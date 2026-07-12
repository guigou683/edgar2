"""Accès aux modèles Ollama : embeddings dense (bge-m3) et génération.

100 % local : appels HTTP vers le service Ollama interne. Aucune sortie réseau
externe. La génération en streaming (SSE) sera ajoutée à la brique génération ;
ce module fournit déjà l'embedding dense par lots, utilisé à l'indexation et à
la recherche.
"""
from __future__ import annotations

import json
import time
from typing import Iterator

import httpx

from core.config import settings

EMBED_BATCH = 32       # lots d'embeddings (compromis débit / mémoire)
EMBED_TIMEOUT = 300.0  # marge pour un rechargement de modèle (bascule VRAM)
EMBED_RETRIES = 2      # reprises sur timeout/erreur réseau transitoire


def _ollama_url() -> str:
    """URL Ollama effective : réglage admin (table settings) s'il est défini, sinon
    le défaut d'environnement. Import paresseux de db pour éviter un cycle."""
    try:
        from core import db
        url = db.get_setting("ollama_url")
        if url and url.strip():
            return url.strip().rstrip("/")
    except Exception:
        pass
    return settings.OLLAMA_URL


def embed_texts(texts: list[str], model: str | None = None, progress=None) -> list[list[float]]:
    """Calcule les embeddings denses d'une liste de textes, par lots de 32.

    Utilise l'endpoint /api/embed d'Ollama (entrée par lot). Renvoie une liste
    de vecteurs (1024 dim pour bge-m3), dans l'ordre des textes fournis. Un lot
    qui échoue sur un timeout/erreur réseau transitoire est réessayé (le premier
    embedding après une bascule de modèle peut être lent). `progress(done, total)`
    est appelé après chaque lot (suivi intra-fichier)."""
    model = model or settings.EMBED_MODEL
    total = len(texts)
    url = _ollama_url()
    out: list[list[float]] = []
    with httpx.Client(timeout=EMBED_TIMEOUT) as client:
        for i in range(0, total, EMBED_BATCH):
            batch = texts[i:i + EMBED_BATCH]
            last_exc: Exception | None = None
            for attempt in range(EMBED_RETRIES + 1):
                try:
                    r = client.post(f"{url}/api/embed",
                                    json={"model": model, "input": batch})
                    r.raise_for_status()
                    out.extend(r.json()["embeddings"])
                    last_exc = None
                    break
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    time.sleep(2.0 * (attempt + 1))  # petit backoff avant reprise
            if last_exc is not None:
                raise last_exc
            if progress:
                progress(min(i + EMBED_BATCH, total), total)
    return out


def embed_query(text: str, model: str | None = None) -> list[float]:
    """Embedding dense d'une requête unique."""
    return embed_texts([text], model=model)[0]


# --------------------------------------------------------------------------
# Chargement des modèles (pour afficher « Chargement du modèle… » à froid)
# --------------------------------------------------------------------------
def model_loaded(model: str | None = None) -> bool:
    """Vrai si le modèle est déjà résident (Ollama /api/ps). En cas de doute
    (Ollama injoignable), renvoie True pour ne pas afficher un chargement à tort."""
    model = model or settings.LLM_MODEL
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{_ollama_url()}/api/ps")
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            return any(n == model or n.startswith(model) for n in names)
    except Exception:
        return True


def preload(model: str | None = None) -> None:
    """Charge un modèle en mémoire sans générer (prompt vide) — Ollama renvoie une
    fois le modèle prêt. Utilisé pour matérialiser l'étape « Chargement du modèle »."""
    model = model or settings.LLM_MODEL
    with httpx.Client(timeout=180.0) as client:
        r = client.post(f"{_ollama_url()}/api/generate", json={"model": model})
        r.raise_for_status()


def unload(model: str) -> None:
    """Décharge un modèle de la mémoire (VRAM/RAM) : requête keep_alive=0 à Ollama."""
    with httpx.Client(timeout=30.0) as client:
        r = client.post(f"{_ollama_url()}/api/generate",
                        json={"model": model, "keep_alive": 0})
        r.raise_for_status()


def loaded_models() -> list[str]:
    """Noms des modèles actuellement chargés en mémoire (/api/ps)."""
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{_ollama_url()}/api/ps")
            r.raise_for_status()
            return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
    except Exception:
        return []


def unload_all() -> list[str]:
    """Décharge tous les modèles actuellement chargés. Renvoie la liste déchargée."""
    names = loaded_models()
    done = []
    for name in names:
        try:
            unload(name)
            done.append(name)
        except Exception:
            pass
    return done


# --------------------------------------------------------------------------
# Re-prompt multi-requêtes (évolution v2)
# --------------------------------------------------------------------------
_REFORMULATE_SYSTEM = (
    "Tu es un assistant de recherche documentaire en français. À partir d'une "
    "question, tu produis des reformulations utiles pour une recherche : "
    "correction, développement des sigles, synonymes, variantes lexicales. "
    "Réponds UNIQUEMENT par les reformulations, une par ligne, sans numérotation "
    "ni commentaire."
)


def reformulate(question: str, n: int = 3, model: str | None = None) -> list[str]:
    """Génère n reformulations de la question (la question d'origine est gérée à part).

    En cas d'échec du LLM, renvoie une liste vide (le pipeline retombe sur la
    seule question d'origine).
    """
    model = model or settings.LLM_MODEL
    prompt = (f"Question : {question}\n\n"
              f"Donne {n} reformulations différentes, une par ligne.")
    try:
        text = generate(prompt, system=_REFORMULATE_SYSTEM, model=model, temperature=0.4)
    except Exception:
        return []
    variants = []
    for line in text.splitlines():
        line = line.strip(" \t-•*0123456789.").strip()
        if line and line.lower() != question.lower():
            variants.append(line)
    # Dédup en conservant l'ordre, borné à n.
    seen, out = set(), []
    for v in variants:
        if v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
        if len(out) >= n:
            break
    return out


# --------------------------------------------------------------------------
# Génération
# --------------------------------------------------------------------------
def generate(prompt: str, system: str | None = None, model: str | None = None,
             temperature: float = 0.2) -> str:
    """Génération non-streamée (utilisée pour la reformulation)."""
    model = model or settings.LLM_MODEL
    payload = {"model": model, "prompt": prompt, "stream": False,
               "options": {"temperature": temperature}}
    if system:
        payload["system"] = system
    with httpx.Client(timeout=120.0) as client:
        r = client.post(f"{_ollama_url()}/api/generate", json=payload)
        r.raise_for_status()
        return r.json().get("response", "")


def generate_stream(prompt: str, system: str | None = None, model: str | None = None,
                    temperature: float = 0.2) -> Iterator[str]:
    """Génération en streaming : produit les jetons au fil de l'eau (pour SSE)."""
    model = model or settings.LLM_MODEL
    payload = {"model": model, "prompt": prompt, "stream": True,
               "options": {"temperature": temperature}}
    if system:
        payload["system"] = system
    with httpx.Client(timeout=300.0) as client:
        with client.stream("POST", f"{_ollama_url()}/api/generate", json=payload) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("response"):
                    yield obj["response"]
                if obj.get("done"):
                    break
