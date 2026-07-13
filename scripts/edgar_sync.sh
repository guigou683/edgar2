#!/usr/bin/env bash
# =====================================================================
# EDGAR v2 — synchronisation TERRE <-> MER (100 % hors-ligne), interactif.
#
#   TERRE  = poste maître : EXPORTE (code/images, modèles, données).
#   MER    = poste embarqué : IMPORTE le bundle reçu.
#
#   Un seul script pour les deux : il demande d'abord le rôle.
#   Usage : bash scripts/edgar_sync.sh
# =====================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="$(command -v python3 || command -v python || true)"
QDRANT_PORT="${QDRANT_PORT:-7333}"
QURL="http://localhost:${QDRANT_PORT}"
IMAGES_ALL=(ollama/ollama:0.30.9 qdrant/qdrant:v1.12.4 edgar2-app:latest edgar2-nginx:latest)
IMAGES_APP=(edgar2-app:latest edgar2-nginx:latest)

c_ok()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_warn(){ printf '\033[33m%s\033[0m\n' "$*"; }
c_err() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
hr()    { printf -- '---------------------------------------------\n'; }

ask()   { local p="$1" d="${2:-}" r; read -rp "$p " r || true; echo "${r:-$d}"; }
ask_yn(){ local p="$1" d="${2:-n}" r; read -rp "$p [$( [ "$d" = y ] && echo O/n || echo o/N )] " r || true
          r="${r:-$d}"; case "$r" in [oOyY]*) return 0;; *) return 1;; esac; }

dc()      { docker compose "$@"; }
# Chemin relatif (cwd conteneur = /app) : évite la conversion de chemin de Git Bash.
sync_py() { docker compose exec -T app python scripts/_sync.py "$@"; }

need_py() { [ -n "$PY" ] || { c_err "python introuvable (requis)."; exit 1; }; }
need_stack() {
  dc ps --status running 2>/dev/null | grep -q edgar2-qdrant \
    || { c_err "La stack doit tourner (docker compose up -d) pour synchroniser les données."; exit 1; }
}

# --------------------------------------------------------------------------
# Modèles Ollama : export/import par manifeste + blobs (adressé par contenu)
# --------------------------------------------------------------------------
model_files() {  # affiche les chemins (manifeste + blobs) relatifs à ollama/models
  need_py
  "$PY" - "$1" <<'PYEOF' | tr -d '\r'
import sys, json, glob, os
name, tag = (sys.argv[1].split(":") + ["latest"])[:2]
base = "ollama/models"
cands = [c for c in dict.fromkeys(
    glob.glob(f"{base}/manifests/**/{name}/{tag}", recursive=True)) if os.path.isfile(c)]
if not cands:
    sys.exit(3)
man = cands[0]
data = json.load(open(man))
digs = []
if isinstance(data.get("config"), dict) and data["config"].get("digest"):
    digs.append(data["config"]["digest"])
for layer in data.get("layers", []):
    if layer.get("digest"):
        digs.append(layer["digest"])
print(os.path.relpath(man, base).replace(os.sep, "/"))   # séparateurs POSIX (tar)
for d in digs:
    print("blobs/" + d.replace(":", "-"))
PYEOF
}

export_model() {  # <model> <outfile.tar>  (tar simple : les poids sont déjà compressés)
  local model="$1" out="$2" files
  if ! files="$(model_files "$model")"; then
    c_warn "  modèle introuvable dans ./ollama : $model (ignoré)"; return 1
  fi
  tar cf "$out" -C ollama/models $files
  c_ok "  modèle exporté : $model  ($(du -h "$out" | cut -f1))"
}

list_installed_models() {
  dc exec -T ollama ollama list 2>/dev/null | awk 'NR>1{print $1}'
}

# --------------------------------------------------------------------------
# Qdrant : transfert d'une collection par copie de son dossier de stockage.
# Méthode déterministe : Qdrant est arrêté le temps de la copie (cohérence des
# fichiers), puis redémarré (il recharge la collection au démarrage).
# --------------------------------------------------------------------------
wait_qdrant() {  # attend que Qdrant réponde (max ~30 s)
  local i; for i in $(seq 1 30); do curl -sf "$QURL/readyz" >/dev/null 2>&1 && return 0; sleep 1; done
  c_warn "  Qdrant met du temps à répondre…"; return 0
}

# =====================================================================
# TERRE — EXPORT
# =====================================================================
run_terre() {
  local OUT; OUT="$(ask 'Dossier de sortie du bundle :' ./sync_bundle)"
  mkdir -p "$OUT"
  hr; c_ok "Export TERRE -> $OUT"; hr

  # --- Code / images ---
  if ask_yn "Exporter le CODE + les IMAGES Docker (mise à jour applicative) ?" n; then
    local imgs=("${IMAGES_APP[@]}")
    ask_yn "  Inclure aussi ollama + qdrant (installation complète, plus lourd) ?" n \
      && imgs=("${IMAGES_ALL[@]}")
    echo "  docker save…"; docker save "${imgs[@]}" -o "$OUT/images.tar"
    echo "  archivage du code…"
    tar czf "$OUT/edgar2_src.tar.gz" \
      --exclude='./data' --exclude='./documents' --exclude='./ollama' --exclude='./qdrant' \
      --exclude='./.git' --exclude='./sync_bundle' --exclude='./offline_bundle' --exclude='./docker_offline' .
    c_ok "  code + images exportés."
  fi

  # --- Modèles ---
  if ask_yn "Exporter des MODÈLES Ollama ?" n; then
    mkdir -p "$OUT/models"
    export_model "bge-m3" "$OUT/models/bge-m3.tar" || true   # embeddings : toujours inclus
    echo "  Modèles de génération installés :"
    mapfile -t MODELS < <(list_installed_models | grep -vi 'bge-m3\|reranker')
    local i=1; for m in "${MODELS[@]}"; do echo "    $i) $m"; i=$((i+1)); done
    local sel; sel="$(ask '  Numéros à exporter (séparés par espace, vide = aucun) :' '')"
    for n in $sel; do
      local m="${MODELS[$((n-1))]:-}"
      [ -n "$m" ] && export_model "$m" "$OUT/models/$(echo "$m" | tr '/:' '__').tar" || true
    done
  fi

  # --- Données (bases + documents) ---
  if ask_yn "Exporter des DONNÉES (bases + documents) ?" n; then
    need_stack
    mkdir -p "$OUT/data"
    echo "  Bases disponibles :"
    mapfile -t BASES < <(sync_py list-bases)
    local i=1; for b in "${BASES[@]}"; do echo "    $i) ${b}"; i=$((i+1)); done
    local sel; sel="$(ask '  Numéros à exporter (espace) ou « all » :' 'all')"
    local ids=()
    if [ "$sel" = "all" ]; then for b in "${BASES[@]}"; do ids+=("${b%%$'\t'*}"); done
    else for n in $sel; do local b="${BASES[$((n-1))]:-}"; [ -n "$b" ] && ids+=("${b%%$'\t'*}"); done; fi
    # Registre + documents (Qdrant encore en marche).
    for bid in "${ids[@]}"; do
      local d="$OUT/data/$bid"; mkdir -p "$d"
      sync_py export-base "$bid" > "$d/base.json"
      [ -d "documents/$bid" ] && tar cf "$d/documents.tar" -C documents "$bid"
    done
    # Collections Qdrant : arrêt le temps de la copie (fichiers cohérents), puis reprise.
    c_warn "  Arrêt momentané de Qdrant pour copier les collections…"
    dc stop qdrant >/dev/null 2>&1 || true
    for bid in "${ids[@]}"; do
      if [ -d "qdrant/collections/edgar2_$bid" ]; then
        tar cf "$OUT/data/$bid/collection.tar" -C qdrant/collections "edgar2_$bid"
        c_ok "  base « $bid » exportée."
      else
        c_warn "  base « $bid » : collection Qdrant absente."
      fi
    done
    dc start qdrant >/dev/null 2>&1 || true; wait_qdrant
  fi

  hr; c_ok "Bundle prêt : $OUT"; du -sh "$OUT" 2>/dev/null | cut -f1 | xargs echo "Taille totale :"
  echo "Transférez ce dossier sur le poste MER, puis relancez ce script en mode MER."
}

# =====================================================================
# MER — IMPORT
# =====================================================================
run_mer() {
  local IN; IN="$(ask 'Dossier du bundle reçu :' ./sync_bundle)"
  [ -d "$IN" ] || { c_err "Dossier introuvable : $IN"; exit 1; }
  hr; c_ok "Import MER <- $IN"; hr

  # --- Code / images ---
  if [ -f "$IN/images.tar" ] && ask_yn "Charger les IMAGES + CODE (mise à jour applicative) ?" y; then
    echo "  docker load…"; docker load -i "$IN/images.tar"
    if [ -f "$IN/edgar2_src.tar.gz" ] && ask_yn "  Remplacer le code (compose/scripts/templates) ?" y; then
      tar xzf "$IN/edgar2_src.tar.gz" -C .
    fi
    ask_yn "  Redémarrer la stack maintenant (docker compose up -d) ?" y && dc up -d
  fi

  # --- Modèles ---
  if [ -d "$IN/models" ] && ask_yn "Installer les MODÈLES du bundle ?" y; then
    mkdir -p ollama/models
    for f in "$IN"/models/*.tar; do
      [ -f "$f" ] || continue
      echo "  $(basename "$f")…"; tar xf "$f" -C ollama/models
    done
    c_ok "  modèles installés (visibles au prochain « ollama list »)."
  fi

  # --- Données ---
  if [ -d "$IN/data" ] && ask_yn "Importer les DONNÉES (bases + documents) ?" y; then
    need_stack
    # Documents + registre (Qdrant en marche).
    for d in "$IN"/data/*/; do
      [ -f "$d/base.json" ] || continue
      local bid; bid="$(basename "$d")"
      echo "  base « $bid »…"
      [ -f "$d/documents.tar" ] && { mkdir -p documents; tar xf "$d/documents.tar" -C documents; }
      sync_py import-base < "$d/base.json"
    done
    # Collections Qdrant : arrêt, dépôt des dossiers, redémarrage (rechargement).
    c_warn "  Arrêt momentané de Qdrant pour installer les collections…"
    dc stop qdrant >/dev/null 2>&1 || true
    for d in "$IN"/data/*/; do
      [ -f "$d/collection.tar" ] || continue
      mkdir -p qdrant/collections
      tar xf "$d/collection.tar" -C qdrant/collections
    done
    dc start qdrant >/dev/null 2>&1 || true; wait_qdrant
    c_ok "  données importées."
  fi

  hr; c_ok "Import terminé."
}

# =====================================================================
# Entrée
# =====================================================================
hr
echo "EDGAR v2 — synchronisation terre <-> mer"
hr
echo "  1) TERRE  (exporter)"
echo "  2) MER    (importer)"
ROLE="$(ask 'Ce poste est :' '')"
case "$ROLE" in
  1|[tT]*) run_terre ;;
  2|[mM]*) run_mer ;;
  *) c_err "Choix invalide."; exit 1 ;;
esac
