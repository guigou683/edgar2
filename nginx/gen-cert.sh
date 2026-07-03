#!/bin/sh
# Génère un certificat auto-signé s'il n'existe pas encore (100 % local, hors-ligne).
# Exécuté automatiquement par l'entrypoint nginx (dossier /docker-entrypoint.d).
set -e

CRT=/certs/server.crt
KEY=/certs/server.key

if [ ! -f "$CRT" ] || [ ! -f "$KEY" ]; then
    mkdir -p /certs
    CN="${EDGAR_CERT_CN:-localhost}"
    SAN="${EDGAR_CERT_SAN:-DNS:localhost,IP:127.0.0.1}"
    echo "[edgar] Génération d'un certificat auto-signé (CN=$CN, SAN=$SAN)…"
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
        -keyout "$KEY" -out "$CRT" \
        -subj "/C=FR/O=Marine nationale/CN=$CN" \
        -addext "subjectAltName=$SAN"
    chmod 600 "$KEY"
fi
