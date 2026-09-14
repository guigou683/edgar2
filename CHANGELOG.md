# Journal des versions — EDGAR v2

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/) ;
versionnage [SemVer](https://semver.org/lang/fr/).

Les versions **0.1.0 → 0.9.0** sont des **pré-releases** (développement incrémental,
une par brique). Les versions **1.x** sont **stables** (1.0.0 = application complète
initiale). La **2.0.0** acte le fonctionnement **hors-ligne validé en conditions réelles**
et l'outillage de déploiement/synchronisation associé.

## [À venir]
- Viewer PDF inline plus poussé, optimisations mémoire/VRAM.

## [2.0.1] — 2026-07-23 — nettoyage

### Retiré
- Option **« Enrichissement LLM des mots-clés »** : l'interface (case à la création de
  base, colonne du tableau de bord) et le stockage par base existaient, mais la logique
  n'était **pas branchée** dans l'ingestion — les mots-clés restaient produits par YAKE
  dans tous les cas. Retrait de cette amorce non fonctionnelle pour lever toute ambiguïté ;
  elle pourra être réintroduite le jour où l'enrichissement sera réellement implémenté.

## [2.0.0] — 2026-07-16 — hors-ligne validé, console d'exploitation & durcissement

Première version **stable 2.x** : fonctionnement hors-ligne éprouvé en conditions réelles,
outillage de déploiement et de synchronisation, et correctifs issus du premier test terrain.

### Ajouté
- **Console d'exploitation** (`tools/edgar_ops/`, Tkinter, sans `sudo`) : **installation**
  assistée d'un poste et **synchronisation hors-ligne entre postes** (bases, documents, index Qdrant,
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

## [1.3.14] — 2026-07-13 — correctif Qdrant sur Docker Linux
- Limite de descripteurs de fichiers relevée (`ulimits.nofile`) sur le service Qdrant :
  sur Docker Linux natif, le défaut faisait échouer le démarrage (« Too many open
  files ») avec des collections volumineuses.

## [1.3.13] — 2026-07-13 — synchronisation hors-ligne entre postes
- Script interactif unique : rôle du poste (source = export, cible = import), puis choix
  des paquets — code + images Docker, modèles Ollama (export par manifeste et blobs),
  données par base.
- Transfert des collections Qdrant par **copie du dossier de stockage**, Qdrant arrêté
  le temps de l'opération : remplace les instantanés HTTP, qui provoquaient des
  corruptions à la restauration.

## [1.3.12] — 2026-07-13 — page « Modèles & recherche » allégée
- Explications et recommandations déplacées dans des info-bulles ; seules restent
  visibles les informations d'état (ressources, recommandation, serveur Ollama courant).

## [1.3.11] — 2026-07-13 — limites de charge
- File d'attente des questions : au plus N traitées en parallèle, les suivantes
  patientent (« En file d'attente… »). Protège le GPU de la saturation.
- Plafond de sessions simultanées, contrôlé à la connexion ; les administrateurs
  ne sont jamais bloqués.

## [1.3.10] — 2026-07-13 — suppression de base au choix
- À la suppression d'une base, option pour supprimer **aussi** le corpus sur disque
  (par défaut : fichiers conservés).
- Saisie de la VRAM déplacée dans la carte « Ressources machine ».
- Page « Mon compte » centrée.

## [1.3.9] — 2026-07-12 — coordination indexation ↔ requêtes
- Nouveau module de coordination : sur GPU unique, l'indexation **cède le pas** aux
  questions interactives (politique « priorité », par défaut) ; politique « parallèle »
  pour machine puissante ou Ollama distant.
- L'encart d'import signale « en pause (requête en cours) ».

## [1.3.8] — 2026-07-12 — déchargement des modèles
- Déchargement d'un modèle ou de tous (`keep_alive=0`) depuis le tableau de bord :
  libère la VRAM/RAM, le modèle étant rechargé à la requête suivante.

## [1.3.7] — 2026-07-10 — ressources machine & profil recommandé
- Détection des cœurs CPU et de la RAM, calcul d'un **profil recommandé**
  (parallélisme d'indexation, conseil de modèle selon la VRAM).
- La VRAM n'étant pas détectable depuis le conteneur, elle est saisie par l'administrateur.
- Bouton « Appliquer le profil ».

## [1.3.6] — 2026-07-10 — serveur Ollama configurable
- URL ou IP:port d'un Ollama **distant** du réseau local (vide = Ollama interne), pris
  en compte immédiatement par les embeddings, la génération et la santé.
- Cartes de paramétrage en pleine largeur ; colonnes d'actions alignées dans la
  gestion des comptes.

## [1.3.5] — 2026-07-09 — correction de la synchronisation du dossier
- Comparaison sur le **chemin relatif complet** : un fichier déplacé en sous-dossier
  est bien vu comme nouveau.
- Purge des entrées dont le fichier a disparu du disque, et de leurs points d'index.

## [1.3.4] — 2026-07-09 — page Documents scalable
- Listing paginé et recherche/filtre côté serveur (HTML ~7 Mo → ~150 Ko pour
  5 000 fichiers) ; statistiques agrégées en SQL.
- Réconciliation retirée du chargement, devenue un bouton « Synchroniser le dossier ».
- Présentation en tableau, navigateur de dossiers, actions repliées dans un menu.

## [1.3.3] — 2026-07-08 — indexation accélérée
- **Recouvrement CPU↔GPU** : préparation du fichier suivant pendant l'embedding du courant.
- OCR des pages parallélisé (rendu abaissé à 150 dpi) et extraction de mots-clés
  parallélisée (~×3,5 sur un document de plusieurs centaines de pages).
- Arrêt souple d'un import et progression intra-fichier (étape + compteur).

## [1.3.2] — 2026-07-07 — gestion de compte et inscription contrôlée
- Page « Mon compte » (profil + changement de mot de passe) en libre-service.
- Inscription au format `prenom.nom` avec validation dynamique et checklist du mot
  de passe en direct ; bouton d'envoi désactivé tant que tout n'est pas conforme.

## [1.3.1] — 2026-07-07 — mise en cache des résumés
- Le résumé d'un document est enregistré à la première génération puis réutilisé
  **instantanément** par tous : aucune puissance LLM dépensée deux fois.
- Bouton « Régénérer » ; invalidation automatique à la ré-analyse ou à la suppression.

## [1.3.0] — 2026-07-07 — sous-dossiers, page Documents repensée et résumé LLM
- Documents identifiés par leur **chemin relatif** partout (registre, index, liens,
  suppression) : les fichiers rangés en sous-dossiers deviennent consultables.
- Page Documents : statistiques (fichiers, extraits, pages, taille) et répartitions
  par statut et par stratégie ; arborescence repliable par sous-dossiers.
- **Résumé d'un document** par le LLM à partir de ses extraits indexés.

## [1.2.3] — 2026-07-07 — statistiques de fin d'import
- Durée totale et taille indexée affichées à la fin d'un import.

## [1.2.2] — 2026-07-07 — OCR automatique en repli
- En stratégie « rapide », toute page PDF sans couche texte exploitable bascule
  automatiquement sur l'**OCR** (PDF entièrement scanné ou mixte, page par page) ;
  le registre affiche « fast (OCR auto) » pour distinguer ces documents.

## [1.2.1] — 2026-07-07 — étapes de recherche animées et zone utilisateur
- Affichage des étapes de recherche en direct avec chronométrage ; seul le temps
  total est conservé sous la réponse.
- Suppression groupée des conversations par base, avec confirmation et audit.

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
