# EDGAR v2 — Exploration de Documentation Guidée par Agent de Raisonnement

Système **RAG auto-hébergé et 100 % hors-ligne** pour interroger de la
documentation technique et obtenir des réponses **synthétiques, en français,
sourcées et vérifiables**. Réécriture *from scratch* de la v1.

> ⚠️ Projet en construction **incrémentale**. État actuel : **brique 1 — socle web**
> (FastAPI démarre, `/static` monté, `/healthz` vérifie Ollama + Qdrant).

## Évolutions v2 (vs v1)
1. Recherche **hybride** dense (bge-m3) + **BM25**, fusion **RRF** native Qdrant.
2. **Re-prompt multi-requêtes** : reformulations LLM avant recherche.
3. **Tagging** des chunks par mots-clés (YAKE + enrichissement LLM optionnel).
4. **Interface web sur-mesure** (FastAPI + Jinja2 + HTMX + JS/CSS vanilla), look OpenWebUI.

## Architecture
Trois services Docker Compose :
- **ollama** — embeddings dense + génération (GPU si présent).
- **qdrant** — base vectorielle (vecteurs nommés `dense` + `sparse`).
- **app** — application EDGAR (FastAPI), reranking sur CPU.

Ports décalés pour cohabiter avec la v1 : app **8800**, qdrant **7333**, ollama **11435**.

## Prérequis hôte (non embarquables)
- Docker + Docker Compose.
- Pour le GPU : pilote NVIDIA + **NVIDIA Container Toolkit**.

## Lancement
```bash
cp .env.example .env          # ajuster EDGAR_SECRET_KEY notamment
docker compose up --build
# Application : http://localhost:8800
# Santé       : http://localhost:8800/healthz
```

## Hors-ligne
Tous les artefacts (images, modèles, polices, JS/CSS) sont pré-téléchargés puis
transférés. Aucun CDN. Vérification cible : exécution sous `--network none`
(`scripts/verify_offline.sh`).

## Feuille de route des briques
- [x] **1. Socle web** — FastAPI, statics, healthcheck, en-têtes de sécurité.
- [ ] 2. Auth/sécurité — Argon2id, sessions, rôles, bootstrap admin, audit+IP.
- [ ] 3. Multi-bases — registre `bases.json`, cloisonnement.
- [ ] 4. Ingestion — parsing/OCR → chunking → tagging → embeddings dense+sparse.
- [ ] 5. Retrieval — multi-query → hybride RRF → fusion → reranking → seuil.
- [ ] 6. Génération streaming + UI chat (sources, diagnostic).
- [ ] 7. Panneau d'expérimentation (toggles/sliders).
- [ ] 8. Administration (bases, comptes, modèles, système, journaux).
- [ ] 9. Chaîne d'export hors-ligne + vérification `--network none`.
