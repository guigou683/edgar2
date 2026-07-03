#!/usr/bin/env bash
# =====================================================================
# EDGAR v2 — Téléchargement de DOCKER + ses dépendances pour une
# installation HORS-LIGNE (Docker uniquement, PAS l'application).
#
# À exécuter sur une machine CONNECTÉE. Demande la plateforme cible et
# télécharge dans ./docker_offline le moteur Docker + Compose (+ NVIDIA
# Container Toolkit si GPU) + un installeur Linux + un guide.
#
# L'application EDGAR (images, modèles, code) s'exporte à part avec
# scripts/export_offline.sh.
#
# Options : --dry-run          (affiche le plan sans télécharger)
#           TARGET=windows|debian|rhel GPU=yes|no   (non interactif)
#
# Tailles : ~650 Mo (Windows) / ~140 Mo (Linux) + ~5 Mo (NVIDIA).
# =====================================================================
set -euo pipefail

DOCKER_STATIC_VERSION="27.3.1"
COMPOSE_VERSION="v2.29.7"
NVCT_VERSION="1.16.2"

DRYRUN=0
[ "${1:-}" = "--dry-run" ] && DRYRUN=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/docker_offline"

c_ok(){ printf '\033[32m%s\033[0m\n' "$*"; }
c_warn(){ printf '\033[33m%s\033[0m\n' "$*"; }
section(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
die(){ printf '\033[31mERREUR: %s\033[0m\n' "$*" >&2; exit 1; }

fetch(){
  local url="$1" dest="$2" opt="${3:-required}"
  if [ "$DRYRUN" = 1 ]; then echo "  [dry-run] $url -> $(basename "$dest")"; return 0; fi
  echo "  -> $(basename "$dest")"
  if ! curl -fSL --retry 3 --retry-delay 2 -o "$dest" "$url"; then
    [ "$opt" = optional ] && { c_warn "  (échec, optionnel)"; return 0; } || die "échec : $url"
  fi
}

TARGET="${TARGET:-}"; GPU="${GPU:-}"
if [ -z "$TARGET" ]; then
  echo "Plateforme cible (machine hors-ligne où installer Docker) ?"
  echo "  1) Windows (Docker Desktop + WSL2)"
  echo "  2) Linux Debian / Ubuntu (amd64)"
  echo "  3) Linux RHEL / Rocky / AlmaLinux / Fedora (amd64)"
  read -rp "Votre choix [1-3] : " ch
  case "$ch" in 1) TARGET=windows;; 2) TARGET=debian;; 3) TARGET=rhel;; *) die "choix invalide";; esac
fi
if [ -z "$GPU" ]; then
  read -rp "GPU NVIDIA à exploiter sur la cible ? [o/N] : " g
  case "$g" in o|O|y|Y|oui|Oui) GPU=yes;; *) GPU=no;; esac
fi
c_ok "Cible : $TARGET | GPU : $GPU | dry-run : $DRYRUN"
mkdir -p "$OUT" "$OUT/nvidia"

# --- Moteur Docker ---
section "Docker + Compose"
case "$TARGET" in
  windows)
    fetch "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe" "$OUT/DockerDesktopInstaller.exe"
    fetch "https://wslstorestorage.blob.core.windows.net/wslblob/wsl_update_x64.msi"    "$OUT/wsl_update_x64.msi"
    ;;
  debian|rhel)
    fetch "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_STATIC_VERSION}.tgz" "$OUT/docker-${DOCKER_STATIC_VERSION}.tgz"
    fetch "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" "$OUT/docker-compose"
    ;;
esac

# --- NVIDIA Container Toolkit (si GPU) ---
if [ "$GPU" = yes ]; then
  section "NVIDIA Container Toolkit ($NVCT_VERSION)"
  case "$TARGET" in
    debian)
      b="https://nvidia.github.io/libnvidia-container/stable/deb/amd64"
      for p in libnvidia-container1_${NVCT_VERSION}-1_amd64.deb libnvidia-container-tools_${NVCT_VERSION}-1_amd64.deb \
               nvidia-container-toolkit-base_${NVCT_VERSION}-1_amd64.deb nvidia-container-toolkit_${NVCT_VERSION}-1_amd64.deb; do
        fetch "$b/$p" "$OUT/nvidia/$p" optional; done ;;
    rhel)
      b="https://nvidia.github.io/libnvidia-container/stable/rpm/x86_64"
      for p in libnvidia-container1-${NVCT_VERSION}-1.x86_64.rpm libnvidia-container-tools-${NVCT_VERSION}-1.x86_64.rpm \
               nvidia-container-toolkit-base-${NVCT_VERSION}-1.x86_64.rpm nvidia-container-toolkit-${NVCT_VERSION}-1.x86_64.rpm; do
        fetch "$b/$p" "$OUT/nvidia/$p" optional; done ;;
    windows)
      c_warn "  Windows : le GPU passe par le pilote NVIDIA + WSL2 (pas de toolkit séparé)." ;;
  esac
  c_warn "  Le PILOTE NVIDIA n'est pas téléchargeable automatiquement (dépend du GPU/OS)."
  c_warn "  À récupérer sur https://www.nvidia.com/Download/index.aspx"
fi

# --- Installeur Linux (binaires statiques + systemd + NVIDIA) ---
if [ "$TARGET" = debian ] || [ "$TARGET" = rhel ]; then
  cat > "$OUT/install_docker.sh" <<'INSTALLEOF'
#!/usr/bin/env bash
# Installe Docker (binaires statiques) + NVIDIA Toolkit (si présent). En root : sudo bash install_docker.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "Exécuter en root (sudo)."; exit 1; }

echo "== Docker (binaires statiques) =="
tar xzf "$HERE"/docker-*.tgz -C /usr/local/bin --strip-components=1
install -d /usr/local/lib/docker/cli-plugins
install -m0755 "$HERE/docker-compose" /usr/local/lib/docker/cli-plugins/docker-compose
getent group docker >/dev/null || groupadd docker

cat > /etc/systemd/system/containerd.service <<'UNIT'
[Unit]
Description=containerd
After=network.target
[Service]
ExecStart=/usr/local/bin/containerd
Restart=always
Delegate=yes
KillMode=process
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/docker.service <<'UNIT'
[Unit]
Description=Docker Application Container Engine
After=network-online.target containerd.service
Wants=network-online.target
Requires=containerd.service
[Service]
ExecStart=/usr/local/bin/dockerd --containerd=/run/containerd/containerd.sock
Restart=always
LimitNOFILE=1048576
Delegate=yes
[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now containerd docker
echo "Docker : $(docker --version)"

if ls "$HERE"/nvidia/*.deb >/dev/null 2>&1; then
  echo "== NVIDIA Container Toolkit (.deb) =="
  dpkg -i "$HERE"/nvidia/*.deb || apt-get -f install -y || true
  nvidia-ctk runtime configure --runtime=docker && systemctl restart docker || echo "Config NVIDIA à finaliser (pilote requis)."
elif ls "$HERE"/nvidia/*.rpm >/dev/null 2>&1; then
  echo "== NVIDIA Container Toolkit (.rpm) =="
  rpm -Uvh --nodeps "$HERE"/nvidia/*.rpm || true
  nvidia-ctk runtime configure --runtime=docker && systemctl restart docker || echo "Config NVIDIA à finaliser (pilote requis)."
fi
echo "Docker installé."
INSTALLEOF
  chmod +x "$OUT/install_docker.sh" 2>/dev/null || true
fi

# --- Guide ---
cat > "$OUT/INSTALL_DOCKER.md" <<EOF
# Installation hors-ligne de Docker — cible : $TARGET (GPU: $GPU)
EOF
if [ "$TARGET" = windows ]; then
cat >> "$OUT/INSTALL_DOCKER.md" <<'EOF'
1. BIOS : activer la virtualisation. Windows : activer « Plateforme de machine virtuelle » et « WSL ».
2. Installer le noyau WSL2 : `wsl_update_x64.msi`.
3. Installer Docker Desktop : `DockerDesktopInstaller.exe` (backend WSL2).
4. (GPU) Installer le pilote NVIDIA Windows adéquat — le support CUDA en WSL2 vient du pilote Windows.
EOF
else
cat >> "$OUT/INSTALL_DOCKER.md" <<'EOF'
Installer via l'installeur fourni (binaires statiques, sans dépendances système) :
```bash
sudo bash install_docker.sh
```
Il pose Docker + Compose (+ NVIDIA Container Toolkit si les paquets sont présents) et configure systemd.
Le PILOTE NVIDIA doit être installé séparément (dépend du GPU/kernel : https://www.nvidia.com/Download/index.aspx).
EOF
fi
cat >> "$OUT/INSTALL_DOCKER.md" <<'EOF'

Ensuite, déployer l'application EDGAR avec le paquet produit par `scripts/export_offline.sh`.
EOF

# --- Sommes de contrôle ---
if [ "$DRYRUN" = 0 ]; then ( cd "$OUT" && find . -type f ! -name SHA256SUMS -exec sha256sum {} \; > SHA256SUMS ); fi

section "Terminé"
c_ok "Docker + dépendances prêts dans : $OUT"
[ "$DRYRUN" = 1 ] && c_warn "(dry-run : rien n'a été téléchargé)"
echo "Transférez ce dossier vers la machine hors-ligne et suivez INSTALL_DOCKER.md."
echo "L'application EDGAR s'exporte séparément : scripts/export_offline.sh"
