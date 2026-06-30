"""Création du premier administrateur d'EDGAR v2 (bootstrap).

Conforme ANSSI :
  - aucun mot de passe par défaut ;
  - changement de mot de passe **forcé** au premier login (must_change_password) ;
  - compte créé actif uniquement s'il n'existe aucun administrateur.

Usage (dans le conteneur app) :
    EDGAR_BOOTSTRAP_PASSWORD='MotDePasse!Fort12' python scripts/bootstrap_admin.py [username]

Si EDGAR_BOOTSTRAP_PASSWORD n'est pas fourni, un mot de passe fort est généré
et affiché **une seule fois**.
"""
from __future__ import annotations

import os
import secrets
import string
import sys
from pathlib import Path

# Localise le dossier contenant le paquet `core`, que l'on soit lancé depuis la
# racine du dépôt (scripts/ et app/ sont frères) ou dans le conteneur (/app).
_here = Path(__file__).resolve().parent
for _cand in (_here.parent / "app", _here.parent, Path("/app"), Path.cwd()):
    if (_cand / "core").is_dir():
        sys.path.insert(0, str(_cand))
        break

from core import auth, db  # noqa: E402


def _generate_password() -> str:
    """Génère un mot de passe respectant la politique (4 classes, 16 caractères)."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(16))
        if auth.validate_password_policy(pwd)[0]:
            return pwd


def main() -> int:
    db.init_db()
    username = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("EDGAR_ADMIN_USER", "admin")).strip()

    # Refuse si un administrateur existe déjà.
    for u in db.list_users():
        if u["role"] == auth.ROLE_ADMIN:
            print(f"Un administrateur existe déjà ({u['username']}). Abandon.", file=sys.stderr)
            return 1

    if db.get_user_by_username(username):
        print(f"L'utilisateur '{username}' existe déjà. Abandon.", file=sys.stderr)
        return 1

    password = os.environ.get("EDGAR_BOOTSTRAP_PASSWORD", "").strip()
    generated = False
    if not password:
        password = _generate_password()
        generated = True

    ok, msg = auth.validate_password_policy(password)
    if not ok:
        print(f"Mot de passe non conforme : {msg}", file=sys.stderr)
        return 2

    uid = db.create_user(
        username=username,
        password_hash=auth.hash_password(password),
        role=auth.ROLE_ADMIN,
        status=auth.STATUS_ACTIVE,
        must_change_password=True,
    )
    db.insert_audit("bootstrap_admin", uid, username, "localhost", "bootstrap-script",
                    "création du premier administrateur")

    print(f"Administrateur '{username}' créé (id={uid}).")
    print("Changement de mot de passe OBLIGATOIRE à la première connexion.")
    if generated:
        print("\n=== MOT DE PASSE INITIAL (affiché une seule fois) ===")
        print(f"    {password}")
        print("=====================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
