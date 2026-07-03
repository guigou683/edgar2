#!/usr/bin/env bash
# EDGAR v2 — (re)génère le certificat auto-signé pour l'IP/nom du serveur.
# Le certificat vit dans le volume nginx ; ce script le régénère et recharge nginx.
#
#   bash scripts/gen_cert.sh 192.168.1.10
#   bash scripts/gen_cert.sh edgar.interne.local
set -euo pipefail

HOST="${1:?Usage: gen_cert.sh <IP-ou-nom-du-serveur>}"
if echo "$HOST" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    SAN="IP:$HOST,DNS:localhost,IP:127.0.0.1"
else
    SAN="DNS:$HOST,DNS:localhost,IP:127.0.0.1"
fi

echo "Régénération du certificat pour $HOST (SAN=$SAN)…"
docker compose exec -T \
    -e EDGAR_CERT_CN="$HOST" -e EDGAR_CERT_SAN="$SAN" \
    nginx sh -c 'rm -f /certs/server.crt /certs/server.key; /docker-entrypoint.d/40-gen-cert.sh; nginx -s reload'
echo "Terminé. Accès : https://$HOST:${HTTPS_PORT:-8443}"
echo "Pensez à installer ce certificat dans le magasin de confiance des postes clients."
