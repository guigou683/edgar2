# Journal des versions — EDGAR v2

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/) ;
versionnage [SemVer](https://semver.org/lang/fr/).

Les versions **0.1.0 → 0.9.0** sont des **pré-releases** (développement incrémental,
une par brique). Les versions **1.x** sont **stables** (1.0.0 = application complète
initiale). La **2.0.0** acte le fonctionnement **hors-ligne validé en conditions réelles**
et l'outillage de déploiement/synchronisation associé.

> Ces résumés sont un point de départ — à ajuster librement selon les besoins.

## [À venir]
- Enrichissement LLM des mots-clés à l'ingestion (option par base), viewer PDF inline
  plus poussé, optimisations mémoire/VRAM.

## [2.0.0] — 2026-07-16 — hors-ligne validé, console d'exploitation & durcissement

Première version **stable 2.x** : fonctionnement hors-ligne éprouvé en conditions réelles,
outillage de déploiement et de synchronisation, et correctifs issus du premier test terrain.

### Ajouté
- **Console d'exploitation** (`tools/edgar_ops/`, Tkinter, sans `sudo`) : **installation**
  assistée d'un poste et **synchronisation terre↔mer** (bases, documents, index Qdrant,
  modèles) depuis un disque. Les **comptes restent locaux** (jamais transférés) ; l'archive
  de code exclut `.env`. Remplace `scripts/edgar_sync.sh`. Dépendance : `python3-tk`.
- **Mention de classification** : bandeau permanent (barre de gauche, toutes les pages) —
  Non protégé (défaut), Diffusion Restreinte ou Secret, avec option *Special France*.
- **Résumé long** de document (map-reduce, couvre tout le document) en plus du résumé
  court ; les deux sont mis en cache et transférés à la synchro.
- **Renommer une conversation** depuis la barre latérale du chat.
- Réglage **« ne pas décharger les modèles automatiquement »** (keep_alive illimité).

### Corrigé
- **Timeout d'indexation sur très gros fichiers** : upsert Qdrant découpé en lots.
- **Modèles déchargés à tort avec un Ollama distant** : `keep_alive` explicite sur tous les
  appels (le défaut serveur ~5 min ne s'applique plus).
- **Formats non supportés** (mp4, zip…) désormais **ignorés** proprement (badge « ignoré »)
  au lieu d'aboutir à un échec.
- **Qdrant sur Docker Linux** : limite `nofile` relevée (« Too many open files »).

### Modifié
- **Dépôt autonome** : libs JS tierces (htmx, marked, dompurify, highlight) versionnées —
  un clone produit une application complète, toujours 100 % hors-ligne.
- Nettoyage : retrait de `HANDOFF.md` et de `tests/` (vérification hors-ligne rendue
  autonome dans `verify_offline.sh`) ; README et documentation mis à jour.

## [1.2.0] — 2026-07-06 — stabilité, gestion documentaire & performances

### Corrigé
- **Blocage / 504 pendant l'import** : les traitements lourds (ingestion, embeddings,
  reranking, reformulation) s'exécutent hors de la boucle asynchrone ; **import en
  tâche de fond** → l'application reste réactive.

### Ajouté
- **Import repensé** : exécution en tâche de fond avec **progression dynamique** (SSE :
  compteurs réussis/ignorés/échecs, fichier courant, temps restant), **raison précise
  de chaque échec**, **choix de stratégie par import** (rapide *fast* / **OCR forcé**).
- **Gestion des documents** (`/documents`) : recherche **filtrée à la frappe** et
  consultation **ouvertes à tous les rôles** ; contributeur/admin : **supprimer**,
  **ré-analyser** (fast↔OCR), **relancer les échecs**, statistiques par base.
- **Préréglages ⚡ Rapide / 🎯 Précis** dans le panneau de recherche.
- **Supervision GPU/CPU** au tableau de bord admin (modèles chargés, VRAM allouée via `/api/ps`).
- **Page « À propos »** (version, développeurs, copyright) accessible depuis la barre latérale.

### Modifié
- **Reranker ONNX** `jinaai/jina-reranker-v2-base-multilingual` (rapide sur CPU) en
  remplacement de bge-reranker-v2-m3/torch → **image app ~6,3 Go → ~2,9 Go**. Sans
  impact sur l'indexation.
- **Stockage en bind mounts** (`./ollama`, `./qdrant`) ; **templates & assets montés à
  chaud** (édition HTML/CSS/JS sans reconstruction). Export/import adaptés (copie de dossiers).
- **Interface** : panneau ⚙️ en **tiroir latéral** (nom/contrôle sur deux lignes,
  interrupteurs), actions comptes/documents alignées, bouton « ← Conversations » dans la
  barre latérale sur toutes les pages, **anti-cache** des assets (fin des soucis de cache).

## [1.1.0] — 2026-07-03 — améliorations UX, admin & HTTPS

### Ajouté
- **HTTPS via reverse proxy nginx** : terminaison TLS, redirection HTTP→HTTPS,
  **certificat auto-signé** généré au démarrage (sans DNS), cookies `Secure`,
  transmission de l'**IP réelle** du client (X-Forwarded-For). Script
  `scripts/gen_cert.sh <IP>` pour la prod ; nginx ajouté à la chaîne offline.
- **Panneau ⚙️** : sélection du LLM **réservée aux admins** (les autres rôles
  gardent le panneau) ; modèles de génération **filtrés** (embeddings/rerankers
  exclus) ; libellés **Hybride / IA / Lexicale** ; **info-bulles « i »** expliquant
  l'effet de chaque réglage.
- **Suppression de conversation** (icône au survol + confirmation).
- **Ingestion** : import **multi-fichiers** + **scan du dossier serveur** de la base.
- **Comptes** : **création de compte par l'admin** ; lien **« ← Conversations »**
  depuis les pages Admin/Import.
- **Journaux** : **export CSV**.
- Indicateur **« Réflexion en cours.. »** animé pendant la recherche.

### Corrigé
- **Changement de rôle qui déconnectait l'admin** : la révocation ne touche plus
  l'admin qui agit ; auto-suspension / auto-rétrogradation **bloquées** (anti-verrouillage).
- Chevauchement des actions **« Suspendre »** / menu **Rôle** dans la page Comptes.

### Modifié
- Inscription : **courriel obligatoire**.
- Politique de mot de passe : suppression de la limite « 128 max » affichée
  (garde-fou interne discret conservé).

## [1.0.0] — 2026-07-02 — première version stable

Application complète : RAG **hybride, souverain, 100 % hors-ligne**, interface type
OpenWebUI. Finalisée par le **panneau d'expérimentation** et l'**aperçu PDF**.

### Ajouté (1.0.0)
- **Panneau d'expérimentation** (⚙️) : leviers mode hybride/dense/BM25, re-prompt + N,
  reranking + top-k, seuil, k candidats, mode recherche, sélecteur LLM, affichage
  mots-clés ; effet immédiat ; portée session (prime) vs défauts globaux admin ;
  bouton de réinitialisation ; diagnostic détaillé sous chaque réponse.
- **Aperçu PDF** positionné sur la page citée + téléchargement (route confinée,
  anti path-traversal, iframe même-origine).

### Récapitulatif (cumul des pré-releases)
- Recherche **hybride** dense (bge-m3) + **BM25** fusionnés par **RRF**.
- **Re-prompt multi-requêtes**, **tagging mots-clés** (YAKE).
- Reranking **bge-reranker-v2-m3** (CPU), **seuil** de pertinence (refus d'inventer).
- Multi-bases cloisonnées, ingestion (unstructured/OCR/PDF), chat streaming SSE + sources.
- Sécurité ANSSI (Argon2id, sessions révocables, CSRF, audit + IP), administration.
- Chaîne d'export hors-ligne vérifiée (`--network none` + réseau interne).

---

## Pré-releases

## [0.9.0] — 2026-07-02 — Export hors-ligne *(pré-release)*
- `pull_models.sh`, `export_offline.sh`, `docker-compose.offline.yml`,
  `verify_offline.sh` : vérification `--network none` (BM25 + reranker) et réseau
  interne (pipeline complet). Fonctionnement 100 % hors-ligne prouvé.

## [0.8.0] — 2026-07-02 — Administration *(pré-release)*
- État système, CRUD bases, validation comptes/rôles (révocation immédiate),
  réglages de recherche globaux, journaux d'audit, contribution (upload + ingestion).

## [0.7.0] — 2026-07-02 — Chat & génération *(pré-release)*
- Orchestration RAG (contexte numéroté, prompt système FR, citations [n]),
  streaming SSE, interface de chat (Markdown assaini, coloration code, sources),
  conversations persistées.

## [0.6.0] — 2026-07-02 — Interrogation *(pré-release)*
- Multi-requêtes → hybride RRF → fusion inter-requêtes → reranking bge-reranker-v2-m3
  (CPU) → seuil de pertinence (refus d'inventer). Leviers + diagnostic.

## [0.5.0] — 2026-07-02 — Parsing / OCR *(pré-release)*
- `unstructured` (fast/OCR Tesseract fra+eng), PDF via pypdf / pdf2image,
  découpage structuré, images en OCR forcé, `ingest_file`.

## [0.4.0] — 2026-07-02 — Vectorisation & tagging *(pré-release)*
- Embeddings bge-m3 (lots de 32), sparse BM25 FastEmbed (modèle bundlé offline),
  mots-clés YAKE, déduplication par hash, upsert hybride.

## [0.3.0] — 2026-07-02 — Multi-bases *(pré-release)*
- Registre `bases.json`, collections Qdrant `dense`+`sparse`, recherche hybride RRF,
  cloisonnement total.

## [0.2.0] — 2026-07-02 — Authentification & sécurité *(pré-release)*
- Argon2id, politique MDP 12–128, anti-bruteforce, sessions cookie révocables,
  CSRF, audit + IP, messages anti-énumération, bootstrap du premier admin.

## [0.1.0] — 2026-07-02 — Socle web *(pré-release)*
- Docker Compose (ollama / qdrant / app), FastAPI, `/healthz`, statics locaux,
  en-têtes de sécurité (CSP stricte).
