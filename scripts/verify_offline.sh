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
echo "  -- nginx HTTPS (certificat auto-signé généré hors-ligne) --"
docker compose exec -T app python -c "import httpx; r=httpx.get('https://nginx/healthz', verify=False, timeout=10); print('  nginx HTTPS ->', r.status_code)"
echo "  -- pipeline : ingestion -> embeddings bge-m3 -> recherche hybride Qdrant --"
docker compose exec -T app python - <<'PY'
import sys
sys.path.insert(0, "/app")
from core import bases, ingest, llm, vectorstore
bid = None
try:
    bid = bases.create_base("Verif hors-ligne", "verif", parse_strategy="fast")["id"]
    docs = ["Le sonar remorqué permet la détection des sous-marins.",
            "La frégate FREMM assure la lutte anti-sous-marine de la Marine nationale."]
    chunks = [ingest.Chunk(text=t, file="c.txt", page=1, section=f"§{i}")
              for i, t in enumerate(docs)]
    rep = ingest.index_chunks(bid, ingest.file_hash("\n".join(docs).encode()), chunks)
    assert rep["indexed"] == 2, "indexation KO"
    qvec = llm.embed_query("comment détecter un sous-marin ?")
    res = vectorstore.hybrid_search(bid, dense_vec=qvec, limit=2)
    assert res and "sonar" in " ".join(r["payload"]["text"].lower() for r in res), "recherche KO"
    print("  OK : ingestion + embeddings + recherche hybride, sans egress Internet.")
finally:
    if bid:
        try:
            bases.delete_base(bid, remove_documents=True)
        except Exception:
            pass
PY

echo "Vérification hors-ligne terminée avec succès."
