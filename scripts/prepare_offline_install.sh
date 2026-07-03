#!/usr/bin/env bash
# =====================================================================
# EDGAR v2 — Préparation d'un PAQUET D'INSTALLATION HORS-LIGNE.
#
# À exécuter sur une machine CONNECTÉE à Internet disposant de Docker.
# Le script demande la plateforme CIBLE (où EDGAR sera installé, hors-ligne)
# et télécharge / prépare TOUT le nécessaire dans ./edgar2_offline_bundle :
#   - Docker lui-même (Docker Desktop pour Windows ; binaires statiques Linux)
#   - NVIDIA Container Toolkit (si GPU) + rappel du pilote NVIDIA (manuel)
#   - Images Docker : ollama, qdrant, edgar2-app, edgar2-nginx (docker save)
#   - Modèles Ollama : bge-m3, mistral:7b, qwen2.5:7b (volume archivé)
#   - Code source + assets front vendus localement (+ modèles bundlés dans l'image)
#   - Guide d'installation + installeur Linux + sommes de contrôle (SHA256)
#
# Options : --dry-run (affiche le plan sans rien télécharger)
#           TARGET=windows|debian|rhel GPU=yes|no (mode non interactif)
#
# Prérequis machine de préparation : Docker, curl, tar, sha256sum.
# Taille approximative du paquet : ~14–16 Go (Windows) / ~14 Go (Linux).
# =====================================================================
set -euo pipefail

# --- Versions épinglées (URLs vérifiées) ---
DOCKER_STATIC_VERSION="27.3.1"
COMPOSE_VERSION="v2.29.7"
NVCT_VERSION="1.16.2"
OLLAMA_IMAGE="ollama/ollama:0.30.9"
QDRANT_IMAGE="qdrant/qdrant:v1.12.4"
OLLAMA_MODELS="${OLLAMA_MODELS:-bge-m3 mistral:7b qwen2.5:7b}"
OLLAMA_VOLUME="edgar2_edgar2_ollama"

DRYRUN=0
[ "${1:-}" = "--dry-run" ] && DRYRUN=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/edgar2_offline_bundle"

c_ok(){ printf '\033[32m%s\033[0m\n' "$*"; }
c_warn(){ printf '\033[33m%s\033[0m\n' "$*"; }
section(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
die(){ printf '\033[31mERREUR: %s\033[0m\n' "$*" >&2; exit 1; }

# fetch <url> <dest> [optional]
fetch(){
  local url="$1" dest="$2" opt="${3:-required}"
  if [ "$DRYRUN" = 1 ]; then echo "  [dry-run] télécharger $url -> $(basename "$dest")"; return 0; fi
  echo "  -> $(basename "$dest")"
  if ! curl -fSL --retry 3 --retry-delay 2 -o "$dest" "$url"; then
    if [ "$opt" = optional ]; then c_warn "  (échec, optionnel) $url"; return 0
    else die "téléchargement échoué : $url"; fi
  fi
}

run(){ if [ "$DRYRUN" = 1 ]; then echo "  [dry-run] $*"; else "$@"; fi; }

# --- Choix de la plateforme cible ---
TARGET="${TARGET:-}"
GPU="${GPU:-}"
if [ -z "$TARGET" ]; then
  echo "Sur quelle plateforme EDGAR sera-t-il installé (machine hors-ligne) ?"
  echo "  1) Windows (Docker Desktop + WSL2)"
  echo "  2) Linux Debian / Ubuntu (amd64)"
  echo "  3) Linux RHEL / Rocky / AlmaLinux / Fedora (amd64)"
  read -rp "Votre choix [1-3] : " ch
  case "$ch" in
    1) TARGET=windows ;; 2) TARGET=debian ;; 3) TARGET=rhel ;;
    *) die "choix invalide" ;;
  esac
fi
if [ -z "$GPU" ]; then
  read -rp "La machine cible a-t-elle un GPU NVIDIA à exploiter ? [o/N] : " g
  case "$g" in o|O|y|Y|oui|Oui) GPU=yes ;; *) GPU=no ;; esac
fi
c_ok "Cible : $TARGET | GPU NVIDIA : $GPU | dry-run : $DRYRUN"

mkdir -p "$OUT" "$OUT/docker" "$OUT/nvidia"

# =====================================================================
# 1) Assets front vendus localement (nécessaires au build de l'image app)
# =====================================================================
section "Assets front (HTMX, marked, DOMPurify, highlight)"
VENDOR="$ROOT/app/static/vendor"
mkdir -p "$VENDOR"
fetch "https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"                              "$VENDOR/htmx.min.js"
fetch "https://cdn.jsdelivr.net/npm/marked@14.1.4/marked.min.js"                       "$VENDOR/marked.min.js"
fetch "https://cdn.jsdelivr.net/npm/dompurify@3.2.3/dist/purify.min.js"                "$VENDOR/dompurify.min.js"
fetch "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/highlight.min.js"   "$VENDOR/highlight.min.js"
fetch "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/styles/github-dark.min.css" "$VENDOR/highlight.css"

# =====================================================================
# 2) Images Docker (build app+nginx, pull ollama+qdrant, docker save)
# =====================================================================
section "Images Docker (build + pull + save)"
[ -f "$ROOT/.env" ] || run cp "$ROOT/.env.example" "$ROOT/.env"
run docker --version >/dev/null 2>&1 || die "Docker est requis sur la machine de préparation."
( cd "$ROOT" && run docker compose build app nginx )
run docker pull "$OLLAMA_IMAGE"
run docker pull "$QDRANT_IMAGE"
run docker save "$OLLAMA_IMAGE" "$QDRANT_IMAGE" edgar2-app:latest edgar2-nginx:latest -o "$OUT/images.tar"

# =====================================================================
# 3) Modèles Ollama (pull dans le volume, puis archivage du volume)
# =====================================================================
section "Modèles Ollama ($OLLAMA_MODELS)"
( cd "$ROOT" && run docker compose up -d ollama )
for m in $OLLAMA_MODELS; do
  echo "  pull $m"
  ( cd "$ROOT" && run docker compose exec -T ollama ollama pull "$m" )
done
run docker run --rm -v "${OLLAMA_VOLUME}:/data:ro" -v "$OUT:/out" alpine \
    tar czf /out/ollama_models.tar.gz -C /data .

# =====================================================================
# 4) Code source + assets (hors données/documents/.git/bundle)
# =====================================================================
section "Code source + assets"
if [ "$DRYRUN" = 1 ]; then echo "  [dry-run] tar edgar2_src.tar.gz"; else
  tar czf "$OUT/edgar2_src.tar.gz" -C "$ROOT" \
    --exclude='./data' --exclude='./documents' --exclude='./.git' \
    --exclude='./edgar2_offline_bundle' --exclude='./offline_bundle' .
fi

# =====================================================================
# 5) Docker (moteur) selon la plateforme cible
# =====================================================================
section "Moteur Docker pour la cible : $TARGET"
case "$TARGET" in
  windows)
    fetch "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe" \
          "$OUT/docker/DockerDesktopInstaller.exe"
    fetch "https://wslstorestorage.blob.core.windows.net/wslblob/wsl_update_x64.msi" \
          "$OUT/docker/wsl_update_x64.msi"
    ;;
  debian|rhel)
    fetch "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_STATIC_VERSION}.tgz" \
          "$OUT/docker/docker-${DOCKER_STATIC_VERSION}.tgz"
    fetch "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
          "$OUT/docker/docker-compose"
    ;;
esac

# =====================================================================
# 6) NVIDIA Container Toolkit (si GPU) — best-effort
# =====================================================================
if [ "$GPU" = yes ]; then
  section "NVIDIA Container Toolkit ($NVCT_VERSION)"
  case "$TARGET" in
    debian)
      base="https://nvidia.github.io/libnvidia-container/stable/deb/amd64"
      for p in libnvidia-container1_${NVCT_VERSION}-1_amd64.deb \
               libnvidia-container-tools_${NVCT_VERSION}-1_amd64.deb \
               nvidia-container-toolkit-base_${NVCT_VERSION}-1_amd64.deb \
               nvidia-container-toolkit_${NVCT_VERSION}-1_amd64.deb; do
        fetch "$base/$p" "$OUT/nvidia/$p" optional
      done ;;
    rhel)
      base="https://nvidia.github.io/libnvidia-container/stable/rpm/x86_64"
      for p in libnvidia-container1-${NVCT_VERSION}-1.x86_64.rpm \
               libnvidia-container-tools-${NVCT_VERSION}-1.x86_64.rpm \
               nvidia-container-toolkit-base-${NVCT_VERSION}-1.x86_64.rpm \
               nvidia-container-toolkit-${NVCT_VERSION}-1.x86_64.rpm; do
        fetch "$base/$p" "$OUT/nvidia/$p" optional
      done ;;
    windows)
      c_warn "  Windows : le support GPU passe par le pilote NVIDIA + WSL2 (pas de toolkit séparé)." ;;
  esac
  c_warn "  IMPORTANT : le PILOTE NVIDIA n'est pas téléchargeable de façon générique"
  c_warn "  (dépend du GPU et de l'OS). À récupérer manuellement sur https://www.nvidia.com/Download/index.aspx"
fi

# =====================================================================
# 7) Installeur Linux (binaires statiques) — généré
# =====================================================================
if [ "$TARGET" = debian ] || [ "$TARGET" = rhel ]; then
  cat > "$OUT/install_linux.sh" <<'INSTALLEOF'
#!/usr/bin/env bash
# Installe Docker (binaires statiques) + NVIDIA Toolkit (si présent) sur la machine HORS-LIGNE.
# À exécuter en root : sudo bash install_linux.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "Exécuter en root (sudo)."; exit 1; }

echo "== Docker (binaires statiques) =="
tar xzf "$HERE"/docker/docker-*.tgz -C /usr/local/bin --strip-components=1
install -d /usr/local/lib/docker/cli-plugins
install -m0755 "$HERE/docker/docker-compose" /usr/local/lib/docker/cli-plugins/docker-compose
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
  nvidia-ctk runtime configure --runtime=docker && systemctl restart docker || echo "Config NVIDIA à finaliser."
elif ls "$HERE"/nvidia/*.rpm >/dev/null 2>&1; then
  echo "== NVIDIA Container Toolkit (.rpm) =="
  rpm -Uvh --nodeps "$HERE"/nvidia/*.rpm || true
  nvidia-ctk runtime configure --runtime=docker && systemctl restart docker || echo "Config NVIDIA à finaliser."
fi
echo "Docker installé. Suivez INSTALL.md pour charger EDGAR."
INSTALLEOF
  chmod +x "$OUT/install_linux.sh" 2>/dev/null || true
fi

# =====================================================================
# 8) Guide d'installation (INSTALL.md)
# =====================================================================
cat > "$OUT/INSTALL.md" <<EOF
# Installation hors-ligne d'EDGAR v2 — cible : $TARGET (GPU: $GPU)

Transférez ce dossier complet vers la machine hors-ligne, puis :

## 1. Installer Docker
EOF
if [ "$TARGET" = windows ]; then
cat >> "$OUT/INSTALL.md" <<'EOF'
1. Activer la virtualisation (BIOS) + « Plateforme de machine virtuelle » et « WSL » (fonctionnalités Windows).
2. Installer le noyau WSL2 : `docker/wsl_update_x64.msi`.
3. Installer Docker Desktop : `docker/DockerDesktopInstaller.exe` (choisir WSL2).
4. (GPU) Installer le pilote NVIDIA Windows adéquat (récupéré manuellement).
   Le support CUDA dans WSL2 est fourni par le pilote Windows (pas de toolkit séparé).
EOF
else
cat >> "$OUT/INSTALL.md" <<'EOF'
Exécuter l'installeur fourni (binaires statiques, sans dépendances système) :
```bash
sudo bash install_linux.sh
```
Il installe Docker + Compose (+ NVIDIA Container Toolkit si les paquets sont présents)
et configure systemd. Le PILOTE NVIDIA doit être installé séparément (voir plus bas).
EOF
fi
cat >> "$OUT/INSTALL.md" <<'EOF'

## 2. Charger les images
```bash
docker load -i images.tar
```

## 3. Restaurer les modèles Ollama
```bash
docker volume create edgar2_edgar2_ollama
docker run --rm -v edgar2_edgar2_ollama:/data -v "$PWD":/in alpine \
  tar xzf /in/ollama_models.tar.gz -C /data
```

## 4. Déployer EDGAR
```bash
mkdir edgar2 && tar xzf edgar2_src.tar.gz -C edgar2 && cd edgar2
cp .env.example .env
# Générer un secret : python -c "import secrets;print(secrets.token_urlsafe(48))"
#   -> renseigner EDGAR_SECRET_KEY dans .env
# (prod) renseigner EDGAR_CERT_CN / EDGAR_CERT_SAN avec l'IP/nom du serveur
docker compose up -d
```

## 5. Premier administrateur + certificat
```bash
docker compose exec app python scripts/bootstrap_admin.py     # mot de passe forcé au 1er login
bash scripts/gen_cert.sh <IP-ou-nom-du-serveur>               # certificat aux couleurs du serveur
```
Accès : `https://<serveur>:8443` (installer le certificat dans le magasin de confiance des postes).

## 6. (option) Vérifier le fonctionnement 100 % hors-ligne
```bash
bash scripts/verify_offline.sh
```

## Prérequis matériel non embarquable (GPU)
- **Pilote NVIDIA** : à installer pour le GPU/OS exact (https://www.nvidia.com/Download/index.aspx).
  Sur Linux, l'installeur configure ensuite le NVIDIA Container Toolkit fourni.
- Sans GPU, EDGAR fonctionne sur CPU (génération plus lente).
EOF

# =====================================================================
# 9) Sommes de contrôle
# =====================================================================
section "Sommes de contrôle (SHA256)"
if [ "$DRYRUN" = 1 ]; then echo "  [dry-run] génération SHA256SUMS"; else
  ( cd "$OUT" && find . -type f ! -name SHA256SUMS -exec sha256sum {} \; > SHA256SUMS )
fi

section "Terminé"
c_ok "Paquet prêt dans : $OUT"
[ "$DRYRUN" = 1 ] && c_warn "(dry-run : rien n'a été téléchargé)"
echo "Contenu : images.tar, ollama_models.tar.gz, edgar2_src.tar.gz, docker/, nvidia/, INSTALL.md, SHA256SUMS"
echo "Transférez ce dossier vers la machine hors-ligne et suivez INSTALL.md."
