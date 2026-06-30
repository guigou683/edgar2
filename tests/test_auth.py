"""Tests concrets de la brique 2 (auth & sécurité).

Exécution (depuis la racine du dépôt) avec un venv disposant de
argon2-cffi et itsdangerous :

    EDGAR_DATA_DIR=<dossier_temp> python tests/test_auth.py

Le test utilise une base SQLite jetable (EDGAR_DATA_DIR) ; il ne dépend ni
d'Ollama ni de Qdrant.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from core import auth, db  # noqa: E402
from core import security  # noqa: E402

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "OK  " if cond else "ÉCHEC"
    print(f"[{status}] {label}")
    if not cond:
        _failures.append(label)


def main() -> int:
    db.init_db()

    # --- Argon2id ---
    h = auth.hash_password("Motdepasse!Fort12")
    check("hachage Argon2id (préfixe $argon2id$)", h.startswith("$argon2id$"))
    check("sel unique (deux hachages diffèrent)", h != auth.hash_password("Motdepasse!Fort12"))
    check("vérification correcte", auth.verify_password(h, "Motdepasse!Fort12"))
    check("vérification rejet mauvais MDP", not auth.verify_password(h, "mauvais"))
    check("vérification rejet hash invalide", not auth.verify_password("pas-un-hash", "x"))

    # --- Politique de mot de passe ---
    check("politique : trop court rejeté", not auth.validate_password_policy("Aa1!")[0])
    check("politique : sans spécial rejeté", not auth.validate_password_policy("Abcdefghij12")[0])
    check("politique : sans chiffre rejeté", not auth.validate_password_policy("Abcdefghij!!")[0])
    check("politique : conforme accepté", auth.validate_password_policy("Abcdefghij1!")[0])
    check("politique : 128 max respecté", not auth.validate_password_policy("Aa1!" + "x" * 130)[0])

    # --- Cycle de vie utilisateur + anti-bruteforce ---
    uname = "bob_" + h[-6:]
    uid = db.create_user(uname, auth.hash_password("BonMotDePasse!9"),
                         auth.ROLE_USER, auth.STATUS_PENDING)

    # Compte pending -> connexion refusée même avec le bon MDP.
    user, _ = auth.authenticate(uname, "BonMotDePasse!9")
    check("compte pending ne peut pas se connecter", user is None)

    db.set_status(uid, auth.STATUS_ACTIVE)
    user, _ = auth.authenticate(uname, "BonMotDePasse!9")
    check("compte actif se connecte", user is not None)

    # Anti-bruteforce : 5 échecs -> verrouillage.
    for _ in range(auth.MAX_FAILED_ATTEMPTS):
        auth.authenticate(uname, "mauvais")
    locked = db.get_user_by_id(uid)
    check("verrouillage après échecs (locked_until posé)", locked["locked_until"] is not None)
    user, _ = auth.authenticate(uname, "BonMotDePasse!9")
    check("connexion refusée pendant verrouillage", user is None)

    # Déverrouillage manuel (simulate expiration) puis succès -> compteurs remis à zéro.
    db.record_login_failure(uid, 0, None)
    user, _ = auth.authenticate(uname, "BonMotDePasse!9")
    check("connexion OK après déverrouillage", user is not None)
    check("compteur d'échecs remis à zéro", db.get_user_by_id(uid)["failed_attempts"] == 0)

    # --- Sessions + révocation immédiate ---
    sid = auth.create_session(uid, "10.0.0.1", "pytest")
    check("session résolue vers l'utilisateur", auth.resolve_session(sid) is not None)

    db.set_status(uid, auth.STATUS_SUSPENDED)
    check("session invalidée si compte suspendu (révocation immédiate)",
          auth.resolve_session(sid) is None)

    db.set_status(uid, auth.STATUS_ACTIVE)
    sid2 = auth.create_session(uid, "10.0.0.1", "pytest")
    auth.logout(sid2)
    check("session révoquée après logout", auth.resolve_session(sid2) is None)

    # Session expirée.
    sid3 = auth.create_session(uid, "10.0.0.1", "pytest")
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    db.touch_session(sid3, past)
    check("session expirée rejetée", auth.resolve_session(sid3) is None)

    # --- Rôles hiérarchiques ---
    admin = {"role": auth.ROLE_ADMIN}
    simple = {"role": auth.ROLE_USER}
    check("admin >= contributeur", auth.has_role(admin, auth.ROLE_CONTRIBUTOR))
    check("utilisateur < contributeur", not auth.has_role(simple, auth.ROLE_CONTRIBUTOR))
    check("None n'a aucun rôle", not auth.has_role(None, auth.ROLE_USER))

    # --- CSRF ---
    token = security.make_csrf_token("nonce-abc")
    check("CSRF : jeton valide accepté", security.verify_csrf_token(token, "nonce-abc"))
    check("CSRF : nonce différent rejeté", not security.verify_csrf_token(token, "autre"))
    check("CSRF : jeton vide rejeté", not security.verify_csrf_token("", "nonce-abc"))
    check("CSRF : jeton falsifié rejeté", not security.verify_csrf_token(token + "x", "nonce-abc"))

    # --- Assainissement & anti path-traversal ---
    check("nom de fichier : composante de chemin retirée",
          security.sanitize_filename("../../etc/passwd") == "passwd")
    check("nom de fichier : accents normalisés",
          security.sanitize_filename("Rapport éé.pdf") == "Rapport_ee.pdf")
    base = APP_DIR
    try:
        security.safe_join(base, "..", "..", "secret")
        traversal_blocked = False
    except ValueError:
        traversal_blocked = True
    check("safe_join : évasion bloquée", traversal_blocked)
    check("safe_join : chemin interne autorisé",
          str(security.safe_join(base, "core", "auth.py")).endswith("auth.py"))

    print()
    if _failures:
        print(f"RÉSULTAT : {len(_failures)} échec(s).")
        return 1
    print("RÉSULTAT : tous les tests passent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
