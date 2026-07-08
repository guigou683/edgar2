#!/usr/bin/env bash
# EDGAR v2 — export des artefacts de l'APPLICATION pour transfert hors-ligne.
# (Docker moteur : voir scripts/download_docker.sh.)
# Prérequis (poste connecté) : images construites (docker compose build) et
# modèles tirés (scripts/pull_models.sh). Stockage en bind mounts : ./ollama, ./qdrant.
#
# Usage : bash scripts/export_offline.sh [dossier_sortie]
set -euo pipefail

OUT="${1:-./offline_bundle}"
IMAGES=(ollama/ollama:0.30.9 qdrant/qdrant:v1.12.4 edgar2-app:latest edgar2-nginx:latest)

# --- Contrôles préalables (échec clair plutôt qu'à mi-parcours) -------------
echo "[0/4] Vérifications…"
missing=0
for img in "${IMAGES[@]}"; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "  ✗ image absente : $img"; missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  echo "→ Construire/tirer d'abord : docker compose build  &&  bash scripts/pull_models.sh"
  exit 1
fi
[ -d ./ollama ] && [ -n "$(ls -A ./ollama 2>/dev/null)" ] || {
  echo "  ✗ ./ollama vide — lancer d'abord scripts/pull_models.sh"; exit 1; }

mkdir -p "$OUT"

echo "[1/4] Sauvegarde des images Docker → images.tar…"
docker save "${IMAGES[@]}" -o "$OUT/images.tar"

echo "[2/4] Archivage des modèles Ollama (bind mount ./ollama)…"
tar czf "$OUT/ollama_models.tar.gz" -C ./ollama .

echo "[3/4] Archivage du code + assets (hors données/modèles/index/.git)…"
tar czf "$OUT/edgar2_src.tar.gz" \
  --exclude='./data' --exclude='./documents' --exclude='./ollama' --exclude='./qdrant' \
  --exclude='./.git' --exclude='./offline_bundle' --exclude='./docker_offline' .

echo "[4/4] Guide d'import…"
cat > "$OUT/IMPORT.md" <<'EOF'
# Import de l'application EDGAR sur le poste hors-ligne
(Docker doit déjà être installé — cf. paquet download_docker.sh / INSTALL_DOCKER.md.)

1. Charger les images :   docker load -i images.tar
2. Extraire le code :     mkdir edgar2 && tar xzf edgar2_src.tar.gz -C edgar2 && cd edgar2
3. Restaurer les modèles (bind mount) :
     mkdir -p ollama && tar xzf ../ollama_models.tar.gz -C ollama
   (Optionnel — préserver les bases déjà indexées : copier les dossiers
    ./qdrant et ./documents depuis le poste source.)
4. cp .env.example .env   (ajuster EDGAR_SECRET_KEY ; en prod EDGAR_CERT_CN/SAN)
5. Démarrer :             docker compose up -d
6. 1er admin :            docker compose exec app python scripts/bootstrap_admin.py
7. Certificat serveur :   bash scripts/gen_cert.sh <IP-ou-nom>
8. Contenu de test :      docker compose exec app python scripts/seed_demo.py
                          (crée la base « demonstration » et l'indexe, 100 % hors-ligne)
9. (option) Offline :     bash scripts/verify_offline.sh

Accès : https://<serveur>:8443
EOF

echo
echo "Bundle prêt dans : $OUT"
du -h "$OUT"/* 2>/dev/null | sort -rh
echo "-------------------------------------------"
echo "Total : $(du -sh "$OUT" | cut -f1)"
