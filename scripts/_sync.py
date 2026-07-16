#!/usr/bin/env python3
"""Aide base de données pour scripts/edgar_sync.sh — exécuté DANS le conteneur app
(accès aux modules core + data/bases.json + registre SQLite).

  export-base <id>   : écrit {base, documents, summaries} en JSON sur stdout
  import-base        : lit ce JSON sur stdin, réinjecte la base (id conservé) + registre + résumés
  list-bases         : liste « id<TAB>nom » des bases présentes
"""
import json
import os
import sys

# Rend le paquet « core » importable quel que soit le cwd (racine app = parent de scripts/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import bases as B
from core import db


def export_base(bid: str) -> None:
    b = B.get_base(bid)
    if not b:
        print(f"base introuvable : {bid}", file=sys.stderr)
        sys.exit(1)
    docs = db.list_documents(bid)
    # Résumés de documents (court + long) : transférés avec la base pour ne pas
    # les perdre à la synchro.
    summaries = db.list_document_summaries(bid)
    json.dump({"base": b, "documents": docs, "summaries": summaries},
              sys.stdout, ensure_ascii=False)


def import_base() -> None:
    data = json.load(sys.stdin)
    b = data["base"]
    allb = B.load_bases()
    created = not any(x["id"] == b["id"] for x in allb)
    if created:
        allb.append(b)
        B._save_bases(allb)
    n = 0
    for d in data.get("documents", []):
        db.upsert_document(b["id"], d["filename"], d["status"], d.get("chunks") or 0,
                           d.get("pages") or 0, d.get("strategy"), d.get("size") or 0,
                           d.get("error"))
        n += 1
    # Résumés (rétro-compatible : absents des anciens bundles).
    s_n = 0
    for s in data.get("summaries", []):
        try:
            db.save_document_summary(b["id"], s["filename"], s["summary"],
                                     s.get("model"), s.get("chunks") or 0, s.get("created_by"),
                                     kind=s.get("kind", "short"))
            s_n += 1
        except Exception:
            pass
    print(json.dumps({"base": b["id"], "nom": b.get("name", ""),
                      "cree": created, "documents": n, "resumes": s_n}, ensure_ascii=False))


def list_bases() -> None:
    for b in B.load_bases():
        print(f'{b["id"]}\t{b.get("name", "")}')


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "export-base" and len(sys.argv) > 2:
        export_base(sys.argv[2])
    elif cmd == "import-base":
        import_base()
    elif cmd == "list-bases":
        list_bases()
    else:
        print("usage: _sync.py export-base <id> | import-base | list-bases", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
