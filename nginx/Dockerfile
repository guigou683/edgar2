# EDGAR v2 — image nginx (reverse proxy TLS) avec openssl pour le certificat auto-signé.
FROM nginx:1.27-alpine

# openssl : génération locale du certificat (aucun accès réseau au runtime).
RUN apk add --no-cache openssl

COPY nginx.conf /etc/nginx/conf.d/default.conf
# Script exécuté par l'entrypoint officiel avant le démarrage de nginx.
COPY gen-cert.sh /docker-entrypoint.d/40-gen-cert.sh
# Normalise d'éventuels CRLF (Windows) et rend exécutable.
RUN sed -i 's/\r$//' /docker-entrypoint.d/40-gen-cert.sh \
    && chmod +x /docker-entrypoint.d/40-gen-cert.sh
