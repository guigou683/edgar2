# =====================================================================
# EDGAR v2 — image de l'application (brique 1 : socle web).
# Base légère ; les briques suivantes ajouteront Tesseract (OCR),
# les poids du reranker, les données NLTK, etc. (tout bundlé, offline).
# =====================================================================
FROM python:3.11-slim

# Mode hors-ligne forcé pour les bibliothèques ML (effet réel dès brique embeddings)
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dépendances Python d'abord (cache de couche)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code applicatif
COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
