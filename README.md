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
Images alignées sur la v1 (Ollama `0.30.9`, Qdrant `v1.12.4`).

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
transférés. Aucun CDN. `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1` au runtime.

**Export (poste connecté) :**
```bash
docker compose up -d --build          # construit l'image app
bash scripts/pull_models.sh           # bge-m3, mistral:7b, qwen2.5:7b -> volume
bash scripts/export_offline.sh        # -> ./offline_bundle (images, modèles, code)
```
Transférer `offline_bundle/` puis suivre `offline_bundle/IMPORT.md` sur le poste cible.

**Vérification (`scripts/verify_offline.sh`) — validée :**
- Partie A : BM25 (FastEmbed) et reranker (bge-reranker-v2-m3) chargés sous
  `--network none` — **aucun** réseau ;
- Partie B : stack sur réseau interne (egress Internet coupé, prouvé) →
  ingestion + retrieval (parsing/OCR, bge-m3, hybride RRF, re-prompt mistral,
  reranking, seuil, génération) : **27 assertions vertes**.

**Prérequis hôte non embarquable** (pour le GPU) : pilote NVIDIA + NVIDIA
Container Toolkit.

## Premier administrateur (bootstrap)
Aucun mot de passe par défaut. Après le démarrage des conteneurs :
```bash
# Mot de passe imposé :
docker compose exec app env EDGAR_BOOTSTRAP_PASSWORD='MotDePasse!Fort12' \
  python scripts/bootstrap_admin.py
# …ou laisser le script générer un mot de passe fort (affiché une seule fois).
```
Le changement de mot de passe est **forcé** à la première connexion.

## Feuille de route des briques
- [x] **1. Socle web** — FastAPI, statics, healthcheck, en-têtes de sécurité.
- [x] **2. Auth/sécurité** — Argon2id, sessions révocables, rôles, CSRF,
      politique MDP, anti-bruteforce, audit+IP, bootstrap admin.
- [x] **3. Multi-bases** — registre `bases.json`, `vectorstore.py` (collections
      Qdrant `dense`+`sparse`, recherche hybride RRF, cloisonnement). Testé contre Qdrant réel.
- [x] **4a. Vectorisation + tagging** — embeddings bge-m3 (lots de 32), sparse
      BM25 FastEmbed (modèle **bundlé offline**), mots-clés YAKE, dédup par hash,
      upsert hybride. Testé offline en conteneur (Ollama+Qdrant réels).
- [x] **4b. Parsing/OCR** — `unstructured` (fast=pdfminer, ocr_only=Tesseract
      fra+eng), découpage structuré `chunk_by_title`, images en OCR forcé,
      `ingest_file` (hash→parsing→chunks→index). Testé en conteneur (md, docx, OCR).
- [x] **5. Retrieval** — re-prompt multi-requêtes (mistral), hybride RRF par
      requête, fusion inter-requêtes RRF, reranking bge-reranker-v2-m3 (CPU,
      bundlé), seuil de pertinence (refus d'inventer), leviers + diagnostic.
      Testé en conteneur (13 assertions).
- [x] **6. Génération streaming + UI chat** — `rag.py` (contexte numéroté,
      prompt système FR + citations [n], refus d'inventer), endpoint SSE,
      interface chat (bulles, Markdown assaini, coloration code, bloc Sources
      dépliable, diagnostic), assets front vendus localement, corpus de démo.
      Streaming validé de bout en bout.
- [ ] 7. Panneau d'expérimentation (toggles/sliders) — *reporté* (leviers déjà
      exposés côté moteur + réglages globaux admin ; panneau UI par session à faire).
- [x] **8. Administration** — dashboard santé + points, CRUD bases, validation
      comptes/rôles (révocation immédiate), réglages de recherche globaux,
      journaux d'audit, contribution (upload + ingestion, filename assaini).
      Garde de rôle testée (403). 
- [x] **9. Export hors-ligne** — `pull_models.sh`, `export_offline.sh`,
      `docker-compose.offline.yml` (réseau interne), `verify_offline.sh`
      (--network none + réseau interne). Voir ci-dessous.
