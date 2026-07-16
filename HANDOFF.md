# EDGAR v2 — Passation de développement (reprise sur Linux / Pop!_OS)

Document destiné à **reprendre le développement** de l'application, y compris par une
**nouvelle instance de Claude Code**. Il résume le projet, l'architecture, les
conventions de travail et la **marche à suivre** pour repartir d'un clone GitHub propre.

> État à la passation : **v1.3.13** · dépôt privé `guigou683/edgar2` · branche `master`.

---

## 1. Le projet en deux phrases

EDGAR v2 est une application **RAG documentaire souveraine et 100 % hors-ligne** pour la
**Marine nationale** : bases documentaires cloisonnées, recherche hybride (dense + lexicale),
réponses en français **sourcées** avec refus d'inventer. Aucune sortie réseau externe.

**Scénario cible « terre ↔ mer »** : un **PC terre** (puissant, mis à jour, fait les grosses
indexations) exporte code/modèles/données ; les **PC mer** (embarqués) importent. La
**console d'exploitation** (`tools/edgar_ops/`, Tkinter) gère installation et synchro hors-ligne.

---

## 2. Pile technique & architecture

- **Backend** : FastAPI + Jinja2 + HTMX/JS vanilla/CSS. SSE pour le streaming (chat, imports).
- **Orchestration** : Docker Compose, 4 services :
  - `nginx` — reverse proxy **HTTPS** (certificat auto-signé), seule façade exposée.
  - `app` — l'application FastAPI (uvicorn), reranking sur CPU.
  - `ollama` — modèles (embeddings + génération), **GPU si présent**.
  - `qdrant` — base vectorielle (vecteurs denses + creux, RRF natif).
- **Modèles** :
  - `bge-m3` — embeddings denses (**imposé**, 1024 dim).
  - `mistral:7b` (défaut) et `qwen2.5:7b` — génération.
  - `jinaai/jina-reranker-v2-base-multilingual` — reranker ONNX **bundlé dans l'image** (FastEmbed).
- **Ports hôte** (décalés) : `8443` HTTPS, `8800` HTTP→HTTPS, `7333` Qdrant, `11435` Ollama.
- **Accès** : `https://<hôte>:8443`. Compte admin créé au bootstrap (voir §7).

### Stockage (bind mounts, hors git)
`./data` (SQLite `edgar2.db`, `bases.json`, `settings.json`) · `./documents` (corpus, un
sous-dossier par base) · `./ollama` (modèles) · `./qdrant` (index vectoriel). En dev, `./app/templates`,
`./app/static`, `./scripts` sont aussi montés pour édition à chaud.

---

## 3. Cartographie du code

```
app/
  main.py              # routes FastAPI (chat SSE, admin, documents, auth…)
  core/
    config.py          # settings (VERSION ici), chemins, env
    db.py              # SQLite (users, sessions, conversations, messages, documents, settings, résumés)
    auth.py            # Argon2id, sessions révocables, rôles, politique MDP, anti-bruteforce
    bases.py           # registre des bases (bases.json) ; collection Qdrant = edgar2_<id>
    vectorstore.py     # Qdrant (create/delete, upsert, hybrid_search, chunks_for_file…)
    ingest.py          # pipeline d'indexation : prepare_file (CPU) puis embed_and_upsert (GPU)
    parsers/           # parse_file -> pdf.py (fast + OCR auto), images.py, document.py
    keywords.py        # YAKE (mots-clés)
    llm.py             # Ollama : embed_texts, generate(_stream), preload/unload, _ollama_url()
    rag.py             # assemblage du contexte + prompts + résumé de document
    retrieval.py       # pipeline d'interrogation (reformulation, dense/bm25, rerank, seuil)
    importer.py        # imports en tâche de fond (jobs, pipeline recouvrement CPU/GPU, arrêt souple)
    appsettings.py     # réglages globaux (défauts recherche, workers, ollama_url, vram, limites…)
    coordinator.py     # priorité requêtes vs indexation (pause de l'indexation)
    limits.py          # file d'attente des requêtes + plafond de sessions
    resources.py       # détection CPU/RAM + reco de profil (VRAM saisie manuelle)
    security.py        # CSRF signé, safe_join (anti path-traversal), CSP
  templates/           # Jinja (chat.html, documents.html, admin/*, account.html, about.html…)
  static/css/edgar.css, static/js/chat.js   # front (montés à chaud)
scripts/
  bootstrap_admin.py   # 1er admin
  pull_models.sh       # pull des modèles Ollama
  gen_cert.sh          # certificat serveur
  seed_demo.py         # base « demonstration » de démonstration
  export_offline.sh    # bundle d'installation complet hors-ligne
  _sync.py             # aide registre/DB (dans le conteneur) pour la console de synchro
tools/edgar_ops/       # console Tkinter (hôte) : installation + synchro terre<->mer
  core.py              # primitives (docker/tar/env/cert/modèles/qdrant/_sync), sans sudo
  ui.py                # interface (accueil -> Installer / Synchroniser)
  verify_offline.sh    # contrôle hors-ligne
docker-compose.yml, app/Dockerfile, nginx/, .env.example
```

---

## 4. Boucle de développement (IMPORTANT pour Claude Code)

- **Modifs Python (`app/core`, `app/main.py`)** → nécessitent un **rebuild** :
  `docker compose build app && docker compose up -d app`.
- **Templates / CSS / JS / scripts** → **montés à chaud** (bind mount), pas de rebuild.
  Un `docker compose restart app` bump l'**`asset_v`** (anti-cache) pour que CSS/JS soient
  rechargés au reload normal ; sinon **Ctrl+F5**.
- **Tester la logique** dans le conteneur :
  `docker compose exec -T app python -c "from core import ...; ..."`.
- **Tester une route** en HTTP (cookies **Secure** → passer par HTTPS nginx, `-k`) :
  1. `GET /login` → extraire le `csrf_token` du HTML + garder le cookie jar.
  2. `POST /login` (username/password/csrf_token).
  3. Récupérer un `csrf_token` frais sur une page, puis appeler la route.
  Le CSRF est lié au cookie `edgar_csrf` (voir `security.py`).
- **Logs** : `docker compose logs app --tail 30`.

---

## 5. Conventions (à respecter)

- **Commits sous le seul nom de l'utilisateur** — **jamais** de trailer `Co-Authored-By: Claude`.
- **Langue** : messages de commit, UI et docs **en français**.
- **Versionnage** : `VERSION` dans `app/core/config.py`. Schéma `1.x` = versions **stables**.
  À la passation : **1.3.13**. Chaque incrément est commité **+ tag + release GitHub** (Latest).
- **Release** : le dépôt utilise `gh` (GitHub CLI). Procédure type :
  `git commit` → `git push` → `git tag vX.Y.Z` → `git push origin vX.Y.Z` →
  `gh release create vX.Y.Z --title "vX.Y.Z" --notes-file notes.md --latest`.
  (Sur Linux, `gh auth login` une fois.)
- **Sécurité** : ne jamais versionner `.env`, `data/`, `documents/`, `ollama/`, `qdrant/`
  (déjà dans `.gitignore`). Régénérer `EDGAR_SECRET_KEY` en production.

---

## 6. Fonctionnalités déjà en place (jusqu'à v1.3.13)

Indexation : OCR **automatique** en repli sur PDF scannés (fast → OCR par page) ; pipeline
avec **recouvrement CPU↔GPU**, **OCR parallèle** (workers réglables), **YAKE** hors chemin
critique ; **arrêt souple** d'un import + **progression intra-fichier** ; robustesse pypdf ;
reprise sur timeout d'embeddings ; `OLLAMA_KEEP_ALIVE=30m`.

Page **Documents** scalable (pagination serveur, recherche/filtre serveur, stats SQL,
navigateur de dossiers, bouton **Synchroniser le dossier**) ; **résumé LLM** par document
(mis en cache) ; boutons **analyser les pending / (ré)analyser un dossier** ; reprise de
l'encart d'import + **anti-double import**.

Admin : **serveur Ollama configurable** (URL/IP externe) ; **détection ressources + profil**
recommandé (VRAM saisie manuelle) ; **déchargement des modèles** (libère la VRAM) ;
**coordination requêtes↔indexation** (priorité/parallèle) ; **file d'attente des questions**
+ **plafond de sessions** ; gestion des comptes (modifier/supprimer en double geste) ;
suppression de base au choix (conserver ou non les fichiers) ; page Modèles allégée (info-bulles).

Comptes : page **« Mon compte »** (profil + mot de passe self-service) ; inscription au
format `prenom.nom` avec **validation dynamique** + checklist mot de passe.

Chat : étapes de recherche animées ; étape **« Chargement du modèle »** à froid ; listes
Markdown correctes ; reprise si réponse vide.

Synchro **terre↔mer** + installation assistée : **console d'exploitation** `tools/edgar_ops/`
(Tkinter, `bash tools/edgar_ops/run.sh` ; dépendance `python3-tk`). Voir §8.
Garanties : aucun `sudo`, les **comptes restent locaux** (jamais transférés).

---

## 7. Ce qui N'EST PAS dans git (à recréer/transférer)

Le clone ne contient **que le code**. Il manque :
- **Images Docker** `edgar2-app`, `edgar2-nginx` → se **construisent** (`docker compose build`).
  `ollama/ollama:0.30.9` et `qdrant/qdrant:v1.12.4` → se **pull** (ou via bundle hors-ligne).
- **Modèles Ollama** (`bge-m3`, `mistral:7b`, `qwen2.5:7b`) → `scripts/pull_models.sh`
  (machine connectée) **ou** transfert via la **console** `tools/edgar_ops/` / `export_offline.sh` depuis l'ancien poste.
- **Données** (bases, documents, index Qdrant) → repartir **à vide** + `seed_demo.py`,
  **ou** importer depuis l'ancien poste via la **console** `tools/edgar_ops/`.

---

## 8. MARCHE À SUIVRE — installation propre sur Pop!_OS (Linux)

Pop!_OS est basé sur Ubuntu ; commandes `apt`. Poste **connecté** pour repartir sain.

### 8.1 Prérequis Docker + GPU NVIDIA
```bash
# Docker Engine + plugin compose (dépôt officiel Docker)
sudo apt-get update && sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$UBUNTU_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"   # puis se déconnecter/reconnecter (ou: newgrp docker)

# Pilote NVIDIA : Pop!_OS (ISO NVIDIA) l'a déjà. Vérifier :
nvidia-smi

# NVIDIA Container Toolkit (accès GPU depuis Docker)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
# Test GPU dans Docker :
docker run --rm --gpus all ubuntu nvidia-smi
```

### 8.2 Cloner et configurer
```bash
gh auth login              # ou configurer un token Git ; dépôt privé
git clone https://github.com/guigou683/edgar2.git
cd edgar2
cp .env.example .env
# IMPORTANT : régénérer le secret
python3 -c "import secrets; print('EDGAR_SECRET_KEY='+secrets.token_urlsafe(48))"
# -> coller la valeur dans .env (remplacer EDGAR_SECRET_KEY). En prod, régler EDGAR_CERT_CN/SAN.
```

### 8.3 Construire, démarrer, modèles
```bash
docker compose build            # images app + nginx
docker compose up -d            # démarre les 4 services
bash scripts/pull_models.sh     # bge-m3 + mistral:7b + qwen2.5:7b (connecté)
```

### 8.4 Certificat, premier admin, contenu de démo
```bash
bash scripts/gen_cert.sh localhost   # ou l'IP/nom du serveur en prod
EDGAR_BOOTSTRAP_PASSWORD='MotDePasse!Fort12' docker compose exec -T app python scripts/bootstrap_admin.py admin
docker compose exec -T app python scripts/seed_demo.py   # base « demonstration » interrogeable
```
> Le mot de passe admin doit être **changé au premier login** (imposé). Sans
> `EDGAR_BOOTSTRAP_PASSWORD`, le script en génère un et l'affiche une seule fois.

### 8.5 Accès
Ouvrir **https://localhost:8443** (certificat auto-signé → accepter l'exception).

### 8.6 (Option) Reprendre les données de l'ancien poste (Windows)
Sur l'ancien poste : `bash tools/edgar_ops/run.sh` → **Synchroniser → Exporter (Terre)**.
Copier le dossier d'export sur le Linux, puis : `bash tools/edgar_ops/run.sh` → **Synchroniser → Importer (Mer)**.

---

## 9. Pièges connus / notes Linux

- **Docker Desktop instable = spécifique Windows.** Sur Linux natif (Docker Engine), plus
  de « Starting the Docker Engine » bloqué. (Sur Windows, le remède était : tuer les process
  `docker`, `wsl --shutdown`, relancer.)
- **Permissions `./ollama` / `./qdrant` sur Linux** : les conteneurs écrivent en `root`.
  La console gère ce cas **sans `sudo`** : les extractions à l'import passent par un
  **conteneur root jetable** (`edgar2-app`) au lieu du script hôte (`core._container_untar`).
  L'export lit côté hôte (fichiers world-readable) ; si des blobs de modèles étaient
  `root:root` non lisibles, l'export de modèles devrait passer par le même mécanisme (à surveiller).
- **GPU 6 Go** : `bge-m3` + un 7B ne tiennent pas ensemble → bascule de modèle. Options :
  modèle de génération 3B (coexiste), déchargement des modèles, ou politique de concurrence
  « priorité aux requêtes ». Voir Admin → Modèles & recherche.
- **Snapshots Qdrant HTTP** : donnaient une corruption au restore dans nos tests → la synchro
  utilise la **copie du dossier de collection** (`qdrant/collections/edgar2_<id>`) avec Qdrant
  arrêté. Ne pas revenir aux snapshots sans revalider.
- **CSRF + cookies Secure** : les tests HTTP doivent passer par `https://localhost:8443` (nginx),
  pas par le port interne de l'app en clair.

---

## 10. Reprendre avec Claude Code sur Linux

1. Cloner le dépôt (§8.2), lire ce fichier + `README.md` + `CHANGELOG.md`.
2. Démarrer la stack (§8.3) et vérifier `https://localhost:8443`.
3. Redonner à la nouvelle instance les **conventions du §5** (commits sans co-auteur, français,
   version + tag + release). Idéalement, recréer une mémoire projet équivalente.
4. Point de départ des évolutions : `git log --oneline` (historique en `vX.X.X`), `main.py`
   pour les routes, `importer.py`/`ingest.py` pour l'indexation, `admin/models.html` pour les réglages.
```
