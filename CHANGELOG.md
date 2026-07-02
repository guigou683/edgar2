# Journal des versions — EDGAR v2

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/) ;
versionnage [SemVer](https://semver.org/lang/fr/).

> Ces résumés sont un point de départ — à ajuster librement selon les besoins.

## [Non publié]
- Réglages à venir (perfs, prompts, corpus).

## [0.1.0] — 2026-07-02

Première version fonctionnelle : RAG auto-hébergé, souverain et **100 % hors-ligne**
pour interroger de la documentation technique et obtenir des réponses **en français,
sourcées et vérifiables**, via une interface type OpenWebUI. Réécriture *from scratch*
de la v1, sur ports et volumes distincts.

### Évolutions majeures vs v1
- **Recherche hybride** dense (bge-m3) + **BM25** fusionnés par **RRF** (Query API native Qdrant).
- **Re-prompt multi-requêtes** : reformulation de la question par le LLM local avant recherche.
- **Tagging des chunks** par mots-clés (YAKE, injectés dans les vecteurs dense et BM25).
- **Interface web sur-mesure** (FastAPI + Jinja2 + HTMX + JS/CSS vanilla), sans build Node, sans CDN.

### Ajouté
- **Socle** : Docker Compose (ollama / qdrant / app), `/healthz`, en-têtes de sécurité (CSP stricte).
- **Sécurité ANSSI** : Argon2id, politique MDP 12–128, anti-bruteforce, sessions cookie
  révocables (révocation immédiate), CSRF, autoescape, SQL paramétré, audit horodaté + IP,
  messages génériques anti-énumération, bootstrap du premier admin (MDP forcé).
- **Multi-bases** : registre `bases.json`, collections Qdrant `dense`+`sparse`, cloisonnement total.
- **Ingestion** : parsing `unstructured` (fast/OCR Tesseract fra+eng), PDF via pypdf/pdf2image,
  découpage structuré, mots-clés YAKE, embeddings bge-m3 (lots de 32), BM25 FastEmbed,
  déduplication par hash, upsert hybride.
- **Interrogation** : multi-requêtes → hybride RRF par requête → fusion inter-requêtes →
  reranking `bge-reranker-v2-m3` (CPU) → seuil de pertinence (refus d'inventer).
- **Chat** : streaming SSE token par token, rendu Markdown assaini + coloration de code,
  bloc **Sources** (fichier · page · section · %), **aperçu PDF** positionné sur la page citée,
  téléchargement confiné (anti path-traversal), conversations persistées.
- **Panneau d'expérimentation** : leviers (mode hybride/dense/BM25, re-prompt + N, rerank + top-k,
  seuil, k candidats, mode recherche, LLM, mots-clés) ; portée session (prime) vs défauts
  globaux admin ; diagnostic détaillé sous chaque réponse.
- **Administration** : état système, CRUD bases, validation comptes/rôles, réglages globaux,
  journaux d'audit, contribution (upload + ingestion).
- **Hors-ligne** : modèles/ assets bundlés, `HF_HUB_OFFLINE=1`, chaîne d'export
  (`pull_models.sh`, `export_offline.sh`) et vérification (`verify_offline.sh` : `--network none`
  + réseau interne).

### Modèles
- Embeddings **bge-m3** (1024, imposé) · Reranker **bge-reranker-v2-m3** (CPU, bundlé) ·
  Génération **mistral:7b** (défaut) / **qwen2.5:7b** (alternative).

### Vérifié
- Pipeline complet testé en conteneur et **hors-ligne** (parsing/OCR, hybride, re-prompt,
  reranking, seuil, génération). Sécurité éprouvée (rôles, path-traversal, anti-énumération).

### Limites connues
- Enrichissement LLM des mots-clés à l'ingestion : prévu (option par base), non activé par défaut.
- Interface mono-langue (français).
