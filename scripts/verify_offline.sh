#!/usr/bin/env bash
# EDGAR v2 — vérification du fonctionnement 100 % hors-ligne.
#   Partie A : composants à modèles bundlés (BM25, reranker) chargés avec
#              AUCUN réseau (--network none).
#   Partie B : pipeline complet (ingestion, hybride, rerank, génération) sur un
#              réseau interne SANS sortie Internet.
set -euo pipefail

echo "== Partie A : BM25 + reranker sans aucun réseau (--network none) =="
docker run --rm -i --network none edgar2-app:latest python - <<'PY'
import sys
sys.path.insert(0, "/app")
from core import sparse, rerank
idx, val = sparse.encode_query("essai hors ligne")
assert idx and len(idx) == len(val), "BM25 KO"
r = rerank.rerank("question", [{"id": 1, "payload": {"text": "texte pertinent"}}], top_k=1)
assert r and 0.0 <= r[0]["rerank_score"] <= 1.0, "Reranker KO"
print("  OK : BM25 et reranker chargés depuis l'image, sans réseau.")
PY

echo "== Partie B : pipeline complet sur réseau interne (sans egress Internet) =="
# Recréation propre du réseau (les alias de service doivent être réinitialisés).
docker compose -f docker-compose.yml -f docker-compose.offline.yml down
docker compose -f docker-compose.yml -f docker-compose.offline.yml up -d
echo "  (attente de la résolution DNS interne…)"
for i in $(seq 1 30); do
  docker compose exec -T app python -c "import socket;socket.gethostbyname('ollama');socket.gethostbyname('qdrant')" 2>/dev/null && break
  sleep 2
done
echo "  -- test d'ingestion (parsing/OCR -> embeddings -> hybride) --"
docker compose exec -T app python - < tests/test_ingest.py
echo "  -- test de retrieval (multi-query -> rerank -> seuil -> génération) --"
docker compose exec -T app python - < tests/test_retrieval.py

echo "Vérification hors-ligne terminée avec succès."
