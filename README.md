# EDGAR v2 — Exploration de Documentation Guidée par Agent de Raisonnement

Système **RAG auto-hébergé, souverain et 100 % hors-ligne** : interroger de la
documentation technique et obtenir des réponses **en français, synthétiques,
sourcées et vérifiables**. Aucune donnée ne sort du réseau local.

> Version courante : **1.2.0** — voir [CHANGELOG.md](CHANGELOG.md).

---

## Présentation

EDGAR indexe des documents (PDF, DOCX, MD, HTML, PPTX, XLSX, TXT, images) dans des
**bases cloisonnées**, puis répond aux questions à partir de ces documents
uniquement. Chaque réponse cite ses **sources** (fichier · page · section) et,
hors corpus, EDGAR répond explicitement « information non trouvée » plutôt que
d'inventer.

Pipeline d'interrogation : reformulation multi-requêtes → recherche **hybride**
(sémantique *bge-m3* + lexicale *BM25*, fusion RRF) → **reranking** (cross-encoder
ONNX) → **seuil de pertinence** → génération en streaming avec citations.

---

## Architecture

Quatre services conteneurisés (Docker Compose) :

| Service | Rôle |
|---|---|
| **nginx** | Reverse proxy **HTTPS** (terminaison TLS, transmet l'IP réelle du client) |
| **app** | Application EDGAR (FastAPI) : API, SSE, reranking sur CPU |
| **ollama** | Modèles : embeddings (GPU si présent, CPU sinon) et génération |
| **qdrant** | Base vectorielle (vecteurs nommés `dense` + `sparse`) |

**Modèles** :
- Embeddings : **bge-m3** (1024 dim) — via Ollama.
- Reranking : **jinaai/jina-reranker-v2-base-multilingual** (ONNX, CPU, multilingue) — bundlé dans l'image.
- Génération : **mistral:7b** (par défaut) ou **qwen2.5:7b** — via Ollama.

**Ports** (via nginx) : `8443` (HTTPS) · `8800` (HTTP → redirige HTTPS) ·
`7333` (Qdrant) · `11435` (Ollama). **Données sur disque** (bind mounts) :
`./data` (SQLite, config), `./documents` (corpus), `./ollama` (modèles),
`./qdrant` (index).

---

## Fonctionnalités

- **Chat** en streaming token par token, rendu Markdown, coloration de code, bloc
  **Sources** dépliable, **aperçu PDF** positionné sur la page citée, téléchargement.
- **Multi-bases** cloisonnées (un « chatbot » par base), stratégie de parsing par base.
- **Panneau de réglages** (⚙️) par session : préréglages **⚡ Rapide / 🎯 Précis** et
  leviers fins (mode hybride/IA/lexicale, re-prompt, reranking, top-k, seuil, etc.),
  avec info-bulles ; défauts globaux réglables par l'admin.
- **Gestion des documents** : recherche filtrée à la frappe et consultation (tous les
  rôles) ; import (fichiers ou dossier serveur) **en tâche de fond** avec progression
  dynamique, raison précise des échecs et choix de stratégie (rapide / OCR) ;
  suppression, ré-analyse, relance des échecs, statistiques (contributeur / admin).
- **Administration** : santé du système, **supervision GPU/CPU** (VRAM allouée par
  modèle), gestion des bases, des comptes et des rôles, réglages globaux, journaux
  d'audit (export CSV).
- **Sécurité ANSSI** (voir plus bas) et **HTTPS** (certificat auto-signé, sans DNS).
- **100 % hors-ligne** : aucun accès réseau au runtime.

---

## Prérequis

- **Docker** + **Docker Compose**.
- GPU **optionnel** : pilote NVIDIA + NVIDIA Container Toolkit (sinon exécution CPU,
  plus lente). Fonctionne confortablement sur un GPU de 8 Go de VRAM.

---

## Installation & lancement

```bash
cp .env.example .env            # ajuster EDGAR_SECRET_KEY (secret de session)
docker compose up -d --build
```

Puis, une seule fois :

```bash
# Créer le premier administrateur (mot de passe forcé au 1er login)
docker compose exec app python scripts/bootstrap_admin.py

# (Production) certificat aux couleurs du serveur
bash scripts/gen_cert.sh <IP-ou-nom-du-serveur>
```

Accès : **https://<hôte>:8443**

> **HTTPS sans DNS** : un certificat **auto-signé** est généré au premier démarrage
> (avertissement navigateur au premier accès). Installez le certificat (ou la CA
> interne) dans le magasin de confiance des postes clients pour le supprimer.
> Sur un serveur **Linux**, nginx journalise les **vraies IP** des postes clients.

---

## Utilisation

- **Rôles** : *Utilisateur* (chat + consultation des documents), *Contributeur*
  (+ import et gestion des documents), *Administrateur* (+ bases, comptes, système).
  L'inscription est libre mais un administrateur valide chaque compte.
- **Poser une question** : choisir une base, saisir la question ; la réponse arrive en
  streaming avec ses sources. Le **mode recherche** renvoie les extraits sans génération.
- **Importer** : page « Importer des documents » — fichiers depuis le navigateur ou
  scan du dossier serveur de la base ; la progression s'affiche en direct.
- **Gérer** : page « Documents » — rechercher, consulter, supprimer, ré-analyser (OCR).

---

## Fonctionnement hors-ligne

Tous les artefacts (images, modèles, polices, JS/CSS) sont pré-téléchargés puis
transférés. Aucun CDN ; `HF_HUB_OFFLINE=1` au runtime. La préparation se fait en
deux temps sur un **poste connecté** :

```bash
# 1) Docker + ses dépendances pour la machine cible (Windows / Debian / RHEL)
bash scripts/download_docker.sh

# 2) L'application (images + modèles + code)
bash scripts/pull_models.sh          # bge-m3, mistral:7b, qwen2.5:7b
bash scripts/export_offline.sh       # -> ./offline_bundle (images, modèles, code, IMPORT.md)
```

Sur la machine hors-ligne : installer Docker, `docker load` des images, restaurer les
dossiers `./ollama` et `./qdrant`, puis `docker compose up -d`. Le **pilote NVIDIA**
(GPU) reste à récupérer manuellement (il dépend du matériel et de l'OS).

**Vérification** (`scripts/verify_offline.sh`) : chargement des modèles sous
`--network none`, puis pipeline complet (ingestion, recherche, reranking, génération)
et HTTPS sur un réseau **sans aucune sortie Internet**.

---

## Sécurité (ANSSI / PSSI-A)

Hachage **Argon2id**, politique de mot de passe (≥ 12, 4 classes), anti-bruteforce,
sessions serveur **révocables** (révocation immédiate des droits), **CSRF**, en-têtes
de sécurité (CSP stricte), autoescape (anti-XSS), requêtes SQL paramétrées,
assainissement des noms de fichiers et **confinement** des accès (anti path-traversal),
journalisation d'audit horodatée avec **IP**, messages d'erreur génériques
(anti-énumération), **HTTPS** (cookies `Secure`).

---

## Structure du projet

```
edgar2/
  docker-compose.yml          docker-compose.offline.yml
  nginx/                      # reverse proxy HTTPS (Dockerfile, conf, cert auto-signé)
  app/
    main.py                   # FastAPI : routes, SSE, sécurité
    core/                     # config, auth, db, bases, ingest, importer, parsers,
                              #   vectorstore, sparse, keywords, rerank, llm, retrieval, rag
    templates/  static/       # Jinja2 + CSS/JS vanilla (assets vendus localement)
  scripts/                    # bootstrap admin, gen_cert, download_docker, export/verify offline
  CHANGELOG.md
```

---

## Crédits

Développé par le **guigou6** et **CoewZelyr**
— page « À propos » dans l'application.

© Marine nationale 2026 — Tous droits réservés.
