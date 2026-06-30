"""Test d'intégration de la brique 3 (multi-bases + Qdrant), contre un Qdrant réel.

Prérequis : Qdrant accessible via QDRANT_URL (par défaut http://localhost:7333
quand on teste depuis l'hôte). Utilise un dossier de données jetable
(EDGAR_DATA_DIR / EDGAR_DOCS_DIR) et nettoie ses collections en fin de test.

    QDRANT_URL=http://localhost:7333 EDGAR_DATA_DIR=<tmp> EDGAR_DOCS_DIR=<tmp/docs> \
        python tests/test_bases_vectorstore.py
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from core import bases, vectorstore  # noqa: E402

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"[{'OK  ' if cond else 'ECHEC'}] {label}")
    if not cond:
        _failures.append(label)


def _dense(idx: int) -> list[float]:
    """Vecteur dense canonique e_idx (1024 dim) pour des tests déterministes."""
    v = [0.0] * vectorstore.DENSE_SIZE
    v[idx] = 1.0
    return v


def main() -> int:
    created_ids: list[str] = []
    try:
        # --- Registre : création de deux bases (cloisonnement) ---
        b1 = bases.create_base("Doctrine Navale", "Test 1", parse_strategy="fast")
        b2 = bases.create_base("Doctrine Navale", "Test 2 (même nom)", parse_strategy="ocr_only")
        created_ids += [b1["id"], b2["id"]]

        check("slug généré", b1["id"] == "doctrine-navale")
        check("collision de nom -> id unique", b2["id"] == "doctrine-navale-2")
        check("collection nommée d'après l'id", b1["collection"] == "edgar2_doctrine-navale")
        check("stratégie de parsing persistée", b2["parse_strategy"] == "ocr_only")
        check("base relue depuis bases.json", bases.get_base(b1["id"]) is not None)
        check("collection Qdrant créée", vectorstore.collection_exists(b1["id"]))

        # --- Indexation de points dense + sparse ---
        pts = [
            {"id": 1, "dense": _dense(0), "sparse_indices": [10], "sparse_values": [1.0],
             "payload": {"file": "a.pdf", "text": "frégate"}},
            {"id": 2, "dense": _dense(1), "sparse_indices": [20], "sparse_values": [1.0],
             "payload": {"file": "b.pdf", "text": "sonar"}},
            {"id": 3, "dense": _dense(2), "sparse_indices": [10, 20], "sparse_values": [1.0, 1.0],
             "payload": {"file": "c.pdf", "text": "frégate sonar"}},
        ]
        vectorstore.upsert_chunks(b1["id"], pts)
        check("comptage des points = 3", vectorstore.count_points(b1["id"]) == 3)

        # --- Recherche dense seule : la requête e0 doit ramener le point 1 en tête ---
        dense_only = vectorstore.hybrid_search(
            b1["id"], dense_vec=_dense(0), use_dense=True, use_sparse=False, limit=10)
        check("dense seul : top1 = point 1", dense_only and dense_only[0]["id"] == 1)

        # --- Recherche BM25 seule : requête sur l'index 20 -> points 2 et 3, pas le 1 ---
        sparse_only = vectorstore.hybrid_search(
            b1["id"], sparse_indices=[20], sparse_values=[1.0],
            use_dense=False, use_sparse=True, limit=10)
        ids_sparse = {r["id"] for r in sparse_only}
        check("BM25 seul : ramène 2 et 3", {2, 3} <= ids_sparse)
        check("BM25 seul : exclut le point 1", 1 not in ids_sparse)

        # --- Recherche hybride RRF : union dense(e0) + sparse(idx20) ---
        hybrid = vectorstore.hybrid_search(
            b1["id"], dense_vec=_dense(0), sparse_indices=[20], sparse_values=[1.0],
            use_dense=True, use_sparse=True, limit=10)
        ids_hybrid = {r["id"] for r in hybrid}
        check("hybride : fusionne dense + BM25 (1, 2 et 3 présents)", {1, 2, 3} <= ids_hybrid)

        # --- Cloisonnement : la base 2 (vide) ne voit rien de la base 1 ---
        check("cloisonnement : base 2 vide", vectorstore.count_points(b2["id"]) == 0)

        # --- Suppression : collection + entrée de registre disparaissent ---
        bases.delete_base(b1["id"])
        created_ids.remove(b1["id"])
        check("suppression : collection Qdrant retirée", not vectorstore.collection_exists(b1["id"]))
        check("suppression : entrée de registre retirée", bases.get_base(b1["id"]) is None)
        check("suppression : autre base intacte", bases.get_base(b2["id"]) is not None)

    finally:
        # Nettoyage des collections restantes.
        for bid in created_ids:
            try:
                vectorstore.delete_collection(bid)
            except Exception:
                pass

    print()
    if _failures:
        print(f"RESULTAT : {len(_failures)} echec(s).")
        return 1
    print("RESULTAT : tous les tests passent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
