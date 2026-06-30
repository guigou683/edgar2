"""Crée une base de démonstration et y indexe un mini-corpus (français, Marine).

Idempotent : ne fait rien si une base nommée « Démonstration » existe déjà.
À lancer dans le conteneur app (Ollama bge-m3 + Qdrant requis) :

    docker compose exec app python scripts/seed_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _cand in (_here.parent / "app", _here.parent, Path("/app"), Path.cwd()):
    if (_cand / "core").is_dir():
        sys.path.insert(0, str(_cand))
        break

from core import bases, ingest  # noqa: E402
from core.config import settings  # noqa: E402

BASE_NAME = "Démonstration"

DOCS = {
    "fremm.md": (
        "# Frégate multi-missions (FREMM)\n\n"
        "La frégate multi-missions FREMM est un navire de combat de la Marine "
        "nationale française. Elle est conçue pour la lutte anti-sous-marine, la "
        "défense aérienne et la frappe vers la terre.\n\n"
        "## Armement\n\n"
        "La FREMM met en œuvre des missiles de croisière navals (MdCN), des "
        "missiles surface-air Aster, ainsi qu'une torpille MU90 pour la lutte "
        "anti-sous-marine.\n"
    ),
    "sonar.md": (
        "# Lutte anti-sous-marine\n\n"
        "La détection des sous-marins repose sur des capteurs acoustiques. Le "
        "sonar remorqué à très basse fréquence permet une détection à grande "
        "distance en analysant les signaux acoustiques sous-marins.\n\n"
        "## Hélicoptère embarqué\n\n"
        "L'hélicoptère NH90 Caïman participe à la lutte anti-sous-marine en "
        "déployant des bouées acoustiques et une torpille légère.\n"
    ),
    "porte-avions.md": (
        "# Porte-avions Charles de Gaulle\n\n"
        "Le porte-avions Charles de Gaulle est le bâtiment amiral de la Marine "
        "nationale. À propulsion nucléaire, il assure la projection de puissance "
        "aérienne depuis la mer.\n\n"
        "## Groupe aérien embarqué\n\n"
        "Le groupe aérien comprend des avions de chasse Rafale Marine, un avion "
        "de guet aérien E-2C Hawkeye et des hélicoptères.\n"
    ),
}


def main() -> int:
    for b in bases.load_bases():
        if b["name"] == BASE_NAME:
            print(f"La base « {BASE_NAME} » existe déjà ({b['id']}). Rien à faire.")
            return 0

    base = bases.create_base(BASE_NAME, "Mini-corpus de démonstration",
                             parse_strategy="fast", llm_enrichment=False)
    docs_dir = settings.DOCUMENTS_DIR / base["id"]
    docs_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    for name, content in DOCS.items():
        path = docs_dir / name
        path.write_text(content, encoding="utf-8")
        rep = ingest.ingest_file(base["id"], str(path), strategy="fast")
        print(f"  {name}: {rep}")
        total += rep.get("indexed", 0)

    print(f"Base « {BASE_NAME} » créée ({base['id']}), {total} chunks indexés.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
