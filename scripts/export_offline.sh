#!/usr/bin/env bash
# EDGAR v2 — export des artefacts de l'APPLICATION pour transfert hors-ligne.
# (Docker moteur : voir scripts/download_docker.sh.)
# Prérequis (poste connecté) : images construites (docker compose build) et
# modèles tirés (scripts/pull_models.sh). Stockage en bind mounts : ./ollama, ./qdrant.
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

echo "[2/3] Archivage des modèles Ollama (bind mount ./ollama)…"
[ -d ./ollama ] || { echo "Dossier ./ollama introuvable — lancer d'abord scripts/pull_models.sh"; exit 1; }
tar czf "$OUT/ollama_models.tar.gz" -C ./ollama .

echo "[3/3] Archivage du code + assets (hors données/modèles/index/.git)…"
tar czf "$OUT/edgar2_src.tar.gz" \
  --exclude='./data' --exclude='./documents' --exclude='./ollama' --exclude='./qdrant' \
  --exclude='./.git' --exclude='./offline_bundle' --exclude='./docker_offline' .

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
8. (option) Offline :     bash scripts/verify_offline.sh

Accès : https://<serveur>:8443
EOF

echo "Bundle prêt dans : $OUT"
ls -la "$OUT"
