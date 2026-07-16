#!/usr/bin/env python3
"""EDGAR v2 — cœur de la console d'exploitation (installation + synchronisation).

Ce module tourne **côté hôte** (pas dans le conteneur) : il pilote `docker
compose`, manipule les dossiers montés (`documents/`, `ollama/`, `qdrant/`) et
appelle l'aide en base `scripts/_sync.py` *dans* le conteneur `app`.

Aucune dépendance externe (bibliothèque standard uniquement) → universel sur
n'importe quelle distribution Linux disposant de Docker et de Python 3.

Garanties de conception :
  - AUCUN `sudo` : tout passe par le CLI `docker` (droits du groupe docker).
  - Les COMPTES restent locaux : aucun transfert n'écrit `users`/`sessions`/
    `settings`. L'archive de code EXCLUT `.env` (secret propre à chaque poste).

Il est réutilisé par l'interface Tkinter (`ui.py`) et expose une petite CLI
(`python3 tools/edgar_ops/core.py <commande>`) pour tests hors interface.
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

# --------------------------------------------------------------------------
# Emplacements (relatifs à la racine du dépôt : tools/edgar_ops/core.py)
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
OLLAMA_MODELS = ROOT / "ollama" / "models"
QDRANT_COLLECTIONS = ROOT / "qdrant" / "collections"
DOCUMENTS_DIR = ROOT / "documents"

# Images applicatives (construites) vs infrastructure (à puller).
IMAGES_APP = ["edgar2-app:latest", "edgar2-nginx:latest"]
IMAGES_INFRA = ["ollama/ollama:0.30.9", "qdrant/qdrant:v1.12.4"]

# Dossiers/fichiers JAMAIS inclus dans l'archive de code (données + secret local).
CODE_EXCLUDE = {
    ".git", ".env", "data", "documents", "ollama", "qdrant",
    "sync_bundle", "offline_bundle", "docker_offline", "__pycache__",
}

Log = Callable[[str], None]


def _noop(_: str) -> None:
    pass


# --------------------------------------------------------------------------
# Primitives Docker / sous-processus
# --------------------------------------------------------------------------
def _run(cmd: list[str], log: Log = _noop, *, capture: bool = False,
         stdin_text: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
    """Exécute une commande à la racine du dépôt. `capture` renvoie stdout."""
    log("$ " + " ".join(cmd))
    proc = subprocess.run(
        cmd, cwd=ROOT, text=True, input=stdin_text,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and proc.returncode != 0:
        err = (proc.stderr or "").strip() if capture else ""
        raise RuntimeError(f"Échec ({proc.returncode}) : {' '.join(cmd)}\n{err}")
    return proc


def _dc(*args: str, log: Log = _noop, capture: bool = False, check: bool = True):
    return _run(["docker", "compose", *args], log, capture=capture, check=check)


def _sync_py(args: list[str], log: Log = _noop, stdin_text: Optional[str] = None) -> str:
    """Appelle scripts/_sync.py DANS le conteneur app (accès registre + DB)."""
    proc = _run(
        ["docker", "compose", "exec", "-T", "app", "python", "scripts/_sync.py", *args],
        log, capture=True, stdin_text=stdin_text,
    )
    return proc.stdout


def _wait_qdrant(log: Log = _noop, port: str = "7333", tries: int = 30) -> None:
    url = f"http://localhost:{port}/readyz"
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1)
    log("  Qdrant met du temps à répondre…")


# --------------------------------------------------------------------------
# Pré-requis
# --------------------------------------------------------------------------
def check_prereqs(log: Log = _noop) -> dict:
    """Vérifie (sans rien installer) : docker, compose, GPU, stack en marche."""
    res = {"docker": False, "compose": False, "gpu": False,
           "stack_running": False, "docker_version": "", "compose_version": ""}
    try:
        p = _run(["docker", "--version"], log, capture=True, check=False)
        res["docker"] = p.returncode == 0
        res["docker_version"] = (p.stdout or "").strip()
    except FileNotFoundError:
        pass
    try:
        p = _dc("version", capture=True, check=False)
        res["compose"] = p.returncode == 0
        res["compose_version"] = (p.stdout or "").splitlines()[0].strip() if p.stdout else ""
    except FileNotFoundError:
        pass
    try:
        p = _run(["nvidia-smi", "-L"], log, capture=True, check=False)
        res["gpu"] = p.returncode == 0
    except FileNotFoundError:
        pass
    if res["compose"]:
        p = _dc("ps", "--status", "running", capture=True, check=False)
        res["stack_running"] = "qdrant" in (p.stdout or "")
    return res


# --------------------------------------------------------------------------
# Bases documentaires
# --------------------------------------------------------------------------
def list_bases(log: Log = _noop) -> list[tuple[str, str]]:
    """Renvoie [(id, nom), …] des bases présentes (via _sync.py dans le conteneur)."""
    out = _sync_py(["list-bases"], log)
    bases = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        bases.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return bases


def detect_bundle(src: str | Path) -> dict:
    """Inspecte un dossier de bundle et renvoie ce qu'il contient (auto-détection
    à l'import) : {app: bool, models: [noms], bases: [ids]}. Basé sur la présence
    des fichiers (pas de manifeste — prévu plus tard)."""
    s = Path(src)
    app = (s / "app" / "images.tar").exists() or (s / "app" / "code.tar.gz").exists()
    models = sorted(p.name for p in (s / "models").glob("*.tar")) if (s / "models").is_dir() else []
    bases = []
    docs = s / "docs"
    if docs.is_dir():
        for d in sorted(docs.iterdir()):
            if (d / "base.json").exists():
                bases.append(d.name)
    return {"app": app, "models": models, "bases": bases}


def export_base(bid: str, dest_dir: str | Path, with_files: bool = True, log: Log = _noop) -> None:
    """Exporte une base : registre + documents (+ résumés) + fichiers (option) + collection Qdrant.

    N'exporte JAMAIS de comptes. `with_files=False` = « base de données seule »
    (registre + index), sans les fichiers du corpus.
    """
    d = Path(dest_dir)
    d.mkdir(parents=True, exist_ok=True)
    log(f"Base « {bid} » : registre + documents + résumés…")
    (d / "base.json").write_text(_sync_py(["export-base", bid], log), encoding="utf-8")

    if with_files and (DOCUMENTS_DIR / bid).exists():
        log("  fichiers du corpus…")
        with tarfile.open(d / "documents.tar", "w") as tar:
            tar.add(DOCUMENTS_DIR / bid, arcname=bid)
    elif not with_files:
        log("  fichiers NON inclus (base de données seule).")

    coll = QDRANT_COLLECTIONS / f"edgar2_{bid}"
    log("  arrêt momentané de Qdrant pour copier la collection…")
    _dc("stop", "qdrant", log=log, check=False)
    try:
        if coll.exists():
            with tarfile.open(d / "collection.tar", "w") as tar:
                tar.add(coll, arcname=f"edgar2_{bid}")
            log(f"  base « {bid} » exportée.")
        else:
            log(f"  base « {bid} » : collection Qdrant absente.")
    finally:
        _dc("start", "qdrant", log=log, check=False)
        _wait_qdrant(log)


def import_base(dir_path: str | Path, log: Log = _noop) -> None:
    """Importe une base depuis un dossier d'export. Ne touche jamais aux comptes."""
    dirp = Path(dir_path)
    base_json = (dirp / "base.json").read_text(encoding="utf-8")
    bid = json.loads(base_json).get("base", {}).get("id", "?")
    log(f"Base « {bid} »…")

    docs_tar = dirp / "documents.tar"
    if docs_tar.exists():
        log("  restauration des fichiers…")
        _container_untar(docs_tar, DOCUMENTS_DIR, log=log)

    log("  " + _sync_py(["import-base"], log, stdin_text=base_json).strip())

    coll_tar = dirp / "collection.tar"
    if coll_tar.exists():
        log("  arrêt momentané de Qdrant pour installer la collection…")
        _dc("stop", "qdrant", log=log, check=False)
        try:
            _container_untar(coll_tar, QDRANT_COLLECTIONS, rm_name=f"edgar2_{bid}", log=log)
        finally:
            _dc("start", "qdrant", log=log, check=False)
            _wait_qdrant(log)
    log(f"  base « {bid} » importée.")


def _container_untar(tar_path: str | Path, host_dest: str | Path,
                     rm_name: Optional[str] = None, log: Log = _noop) -> None:
    """Extrait un tar dans un dossier hôte VIA un conteneur root jetable (image
    `edgar2-app`, toujours présente). Permet d'écraser des fichiers appartenant à
    root (écrits par les conteneurs qdrant/ollama) — sans aucun `sudo`.

    `rm_name` : sous-dossier de destination à purger d'abord (remplacement propre).
    """
    tar_path = Path(tar_path)
    host_dest = Path(host_dest)
    host_dest.mkdir(parents=True, exist_ok=True)
    rm = f"rm -rf '/dest/{rm_name}'; " if rm_name else ""
    script = f"{rm}tar xf '/bundle/{tar_path.name}' -C /dest"
    _run(["docker", "run", "--rm",
          "-v", f"{tar_path.parent}:/bundle:ro",
          "-v", f"{host_dest}:/dest",
          "--entrypoint", "sh", "edgar2-app", "-c", script], log)


# --------------------------------------------------------------------------
# Modèles Ollama (manifeste + blobs, adressés par contenu)
# --------------------------------------------------------------------------
def _model_files(spec: str) -> Optional[list[str]]:
    name, tag = (spec.split(":") + ["latest"])[:2]
    cands = sorted(p for p in OLLAMA_MODELS.glob(f"manifests/**/{name}/{tag}") if p.is_file())
    if not cands:
        return None
    data = json.loads(cands[0].read_text())
    digs = []
    cfg = data.get("config")
    if isinstance(cfg, dict) and cfg.get("digest"):
        digs.append(cfg["digest"])
    for layer in data.get("layers", []):
        if layer.get("digest"):
            digs.append(layer["digest"])
    files = [cands[0].relative_to(OLLAMA_MODELS).as_posix()]
    files += ["blobs/" + d.replace(":", "-") for d in digs]
    return files


def installed_models(log: Log = _noop) -> list[str]:
    """Modèles chargés dans Ollama (hors reranker), via `ollama list`."""
    p = _dc("exec", "-T", "ollama", "ollama", "list", capture=True, check=False)
    out = p.stdout or ""
    names = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if parts and "reranker" not in parts[0].lower():
            names.append(parts[0])
    return names


def export_models(specs: list[str], dest_dir: str | Path, log: Log = _noop) -> list[str]:
    """Exporte les modèles nommés (tar par modèle). Renvoie ceux réellement exportés."""
    d = Path(dest_dir)
    d.mkdir(parents=True, exist_ok=True)
    done = []
    for spec in specs:
        files = _model_files(spec)
        if not files:
            log(f"  modèle introuvable dans ./ollama : {spec} (ignoré)")
            continue
        out = d / (spec.replace("/", "__").replace(":", "__") + ".tar")
        with tarfile.open(out, "w") as tar:
            for f in files:
                p = OLLAMA_MODELS / f
                if p.exists():
                    tar.add(p, arcname=f)
        log(f"  modèle exporté : {spec}")
        done.append(spec)
    return done


def import_models(dir_path: str | Path, log: Log = _noop) -> int:
    dirp = Path(dir_path)
    OLLAMA_MODELS.mkdir(parents=True, exist_ok=True)
    n = 0
    for tar_path in sorted(dirp.glob("*.tar")):
        log(f"  {tar_path.name}…")
        _container_untar(tar_path, OLLAMA_MODELS, log=log)
        n += 1
    return n


# --------------------------------------------------------------------------
# Mise à jour applicative (code + images) — sans .env ni données
# --------------------------------------------------------------------------
def export_app(dest_dir: str | Path, include_infra: bool = False, log: Log = _noop) -> None:
    """Exporte les images Docker + le code (SANS `.env`, données, modèles, index)."""
    d = Path(dest_dir)
    d.mkdir(parents=True, exist_ok=True)
    imgs = IMAGES_APP + (IMAGES_INFRA if include_infra else [])
    log("  docker save (images)…")
    _run(["docker", "save", *imgs, "-o", str(d / "images.tar")], log)
    log("  archivage du code (sans .env ni données)…")
    _tar_code(d / "code.tar.gz", log)
    log("  application exportée.")


def _tar_code(dest_tgz: str | Path, log: Log = _noop) -> None:
    """Archive le code du dépôt en excluant strictement données et secret."""
    with tarfile.open(dest_tgz, "w:gz") as tar:
        for item in sorted(ROOT.iterdir()):
            if item.name in CODE_EXCLUDE:
                continue
            tar.add(item, arcname=item.name)


def import_app(dir_path: str | Path, replace_code: bool = True, log: Log = _noop) -> None:
    """Charge les images, remplace le code (sans jamais toucher `.env`), relance."""
    dirp = Path(dir_path)
    images = dirp / "images.tar"
    if images.exists():
        log("  docker load (images)…")
        _run(["docker", "load", "-i", str(images)], log)
    code = dirp / "code.tar.gz"
    if replace_code and code.exists():
        log("  remplacement du code (préserve `.env`)…")
        with tarfile.open(code) as tar:
            members = [m for m in tar.getmembers()
                       if m.name != ".env" and not m.name.endswith("/.env")]
            try:
                tar.extractall(ROOT, members=members, filter="data")
            except TypeError:
                tar.extractall(ROOT, members=members)
    log("  démarrage de la stack…")
    _dc("up", "-d", log=log)


# --------------------------------------------------------------------------
# Installation : secret, certificat, démarrage, admin
# --------------------------------------------------------------------------
def ensure_env_secret(log: Log = _noop) -> bool:
    """Garantit un `EDGAR_SECRET_KEY` fort dans `.env`. Renvoie True si généré/modifié."""
    if not ENV_FILE.exists():
        if ENV_EXAMPLE.exists():
            ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
            log("  .env créé depuis .env.example.")
        else:
            ENV_FILE.write_text("", encoding="utf-8")
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    new_key = f"EDGAR_SECRET_KEY={secrets.token_urlsafe(48)}"
    for i, ln in enumerate(lines):
        if ln.startswith("EDGAR_SECRET_KEY="):
            val = ln.split("=", 1)[1].strip()
            if val and "change" not in val.lower() and len(val) >= 32:
                return False  # secret déjà valide : on n'y touche pas
            lines[i] = new_key
            ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log("  EDGAR_SECRET_KEY (re)généré.")
            return True
    lines.append(new_key)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("  EDGAR_SECRET_KEY généré.")
    return True


def gen_cert(host: str, log: Log = _noop) -> None:
    """(Re)génère le certificat auto-signé pour l'IP/nom du serveur."""
    _run(["bash", "scripts/gen_cert.sh", host], log)


def compose_up(log: Log = _noop) -> None:
    _dc("up", "-d", log=log)


def wait_healthy(log: Log = _noop, port: str = "8443", tries: int = 60) -> bool:
    """Attend que la façade HTTPS réponde (certificat auto-signé accepté)."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = f"https://localhost:{port}/login"
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=3, context=ctx) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def bootstrap_admin(username: str, password: str, log: Log = _noop) -> str:
    """Crée le premier administrateur (mot de passe à changer au 1er login)."""
    p = _run(
        ["docker", "compose", "exec", "-T",
         "-e", f"EDGAR_BOOTSTRAP_PASSWORD={password}",
         "app", "python", "scripts/bootstrap_admin.py", username],
        log, capture=True, check=False,
    )
    return (p.stdout or "") + (p.stderr or "")


# --------------------------------------------------------------------------
# CLI de test (avant l'interface Tkinter)
# --------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="EDGAR — cœur d'exploitation (CLI de test)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prereqs")
    sub.add_parser("list-bases")
    eb = sub.add_parser("export-base")
    eb.add_argument("bid")
    eb.add_argument("dest")
    eb.add_argument("--no-files", action="store_true")
    ib = sub.add_parser("import-base")
    ib.add_argument("dir")
    ea = sub.add_parser("export-app")
    ea.add_argument("dest")
    ea.add_argument("--code-only", action="store_true")
    ea.add_argument("--with-infra", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "prereqs":
        print(json.dumps(check_prereqs(print), indent=2, ensure_ascii=False))
    elif args.cmd == "list-bases":
        for bid, name in list_bases(print):
            print(f"{bid}\t{name}")
    elif args.cmd == "export-base":
        export_base(args.bid, args.dest, with_files=not args.no_files, log=print)
    elif args.cmd == "import-base":
        import_base(args.dir, log=print)
    elif args.cmd == "export-app":
        if args.code_only:
            _tar_code(Path(args.dest) / "code.tar.gz", print)
        else:
            export_app(args.dest, include_infra=args.with_infra, log=print)
    return 0


if __name__ == "__main__":
    sys.exit(main())
