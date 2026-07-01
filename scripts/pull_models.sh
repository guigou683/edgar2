#!/usr/bin/env bash
# EDGAR v2 — pré-téléchargement des modèles Ollama dans le volume (poste connecté).
# À lancer une fois, stack démarré, avant l'export hors-ligne.
set -euo pipefail

echo "Téléchargement des modèles Ollama (volume edgar2_ollama)…"
docker compose exec -T ollama ollama pull bge-m3        # embeddings dense (imposé)
docker compose exec -T ollama ollama pull mistral:7b    # génération par défaut
docker compose exec -T ollama ollama pull qwen2.5:7b    # génération alternative
echo "Modèles présents :"
docker compose exec -T ollama ollama list
