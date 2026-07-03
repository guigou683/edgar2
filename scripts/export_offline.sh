#!/usr/bin/env bash
# EDGAR v2 — export de tous les artefacts pour transfert vers un poste hors-ligne.
# Prérequis (poste connecté) : stack construit (docker compose build) et modèles
# tirés (scripts/pull_models.sh). Produit un dossier transférable.
set -euo pipefail

OUT="${1:-./offline_bundle}"
mkdir -p "$OUT"

echo "[1/3] Sauvegarde des images Docker…"
docker save \
  ollama/ollama:0.30.9 \
  qdrant/qdrant:v1.12.4 \
  edgar2-app:latest \
  edgar2-nginx:latest \
  -o "$OUT/images.tar"

echo "[2/3] Archivage du volume Ollama (modèles pré-téléchargés)…"
docker run --rm \
  -v edgar2_edgar2_ollama:/data:ro \
  -v "$(pwd)/$OUT:/out" \
  alpine tar czf /out/ollama_models.tar.gz -C /data .

echo "[3/3] Archivage du code + assets vendus (hors données/documents/.git)…"
tar czf "$OUT/edgar2_src.tar.gz" \
  --exclude='./data' --exclude='./documents' --exclude='./.git' \
  --exclude='./offline_bundle' .

cat > "$OUT/IMPORT.md" <<'EOF'
# Import sur le poste hors-ligne
1. Charger les images :        docker load -i images.tar
2. Extraire le code :          mkdir edgar2 && tar xzf edgar2_src.tar.gz -C edgar2 && cd edgar2
3. Recréer le volume modèles :
     docker volume create edgar2_edgar2_ollama
     docker run --rm -v edgar2_edgar2_ollama:/data -v "$PWD/..":/in alpine \
       tar xzf /in/ollama_models.tar.gz -C /data
4. Copier .env.example en .env (ajuster EDGAR_SECRET_KEY).
5. Démarrer :                  docker compose up -d
6. Créer le 1er admin :        docker compose exec app python scripts/bootstrap_admin.py
7. (option) Vérifier offline : bash scripts/verify_offline.sh
EOF

echo "Bundle prêt dans : $OUT"
ls -la "$OUT"
