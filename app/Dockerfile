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

# Dépendances système : OCR Tesseract (fra+eng), conversion PDF->image (poppler),
# détection de type de fichier (libmagic). Stratégies fast + ocr_only uniquement
# (pas de hi_res), donc pas de pile lourde torch/detectron.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng \
        poppler-utils libmagic1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Dépendances Python d'abord (cache de couche)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Modèles & données bundlés dans l'image (offline). On lève temporairement le mode
# hors-ligne UNIQUEMENT pendant le build (poste connecté) ; à l'exécution,
# HF_HUB_OFFLINE=1 reste actif et bloque tout accès réseau.
ENV EDGAR_MODELS_DIR=/app/models \
    NLTK_DATA=/app/models/nltk
RUN mkdir -p /app/models/nltk && \
    HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python -c \
    "from fastembed import SparseTextEmbedding; SparseTextEmbedding(model_name='Qdrant/bm25', cache_dir='/app/models/fastembed')" && \
    python -c "import nltk; [nltk.download(p, download_dir='/app/models/nltk') for p in ['punkt','punkt_tab','averaged_perceptron_tagger','averaged_perceptron_tagger_eng']]"

# Code applicatif
COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
