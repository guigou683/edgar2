"""Crée une base de démonstration et y indexe un mini-corpus (français).

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
    "onduleur.md": (
        "# Onduleur (alimentation sans interruption)\n\n"
        "Un onduleur protège les équipements sensibles contre les coupures et les "
        "micro-variations du réseau électrique. Il bascule sur batterie en quelques "
        "millisecondes lors d'une perte de secteur.\n\n"
        "## Maintenance\n\n"
        "Les batteries se remplacent en moyenne tous les trois à cinq ans. Un test "
        "de décharge périodique vérifie l'autonomie réelle restante.\n"
    ),
    "climatisation.md": (
        "# Climatisation d'une salle technique\n\n"
        "La climatisation maintient une température et une hygrométrie stables afin "
        "de préserver la fiabilité des équipements. Une redondance N+1 évite l'arrêt "
        "en cas de panne d'une unité.\n\n"
        "## Surveillance\n\n"
        "Des sondes réparties dans la salle déclenchent une alerte lorsqu'un seuil "
        "de température est dépassé, avant que le matériel ne soit affecté.\n"
    ),
    "sauvegarde.md": (
        "# Politique de sauvegarde\n\n"
        "La règle 3-2-1 recommande trois copies des données, sur deux supports "
        "différents, dont une copie hors site. Elle limite le risque de perte en "
        "cas d'incident matériel.\n\n"
        "## Restauration\n\n"
        "Un test de restauration régulier garantit que les sauvegardes sont "
        "exploitables : une sauvegarde jamais restaurée n'a pas de valeur prouvée.\n"
    ),
}


def main() -> int:
    for b in bases.load_bases():
        if b["name"] == BASE_NAME:
            print(f"La base « {BASE_NAME} » existe déjà ({b['id']}). Rien à faire.")
            return 0

    base = bases.create_base(BASE_NAME, "Mini-corpus de démonstration",
                             parse_strategy="fast")
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
