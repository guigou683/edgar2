#!/usr/bin/env python3
"""EDGAR v2 — console d'exploitation (interface Tkinter).

Interface « presse-bouton » côté hôte : accueil → Installer / Synchroniser.
Toute la logique vit dans core.py ; cette couche ne fait qu'orchestrer et afficher.

Lancement :  python3 tools/edgar_ops/ui.py   (ou via run.sh)
Dépendances : Python 3 + Tkinter (paquet système `python3-tk`).
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    from . import core
except ImportError:  # lancé comme script : core.py est dans le même dossier
    import core  # type: ignore

PAD = {"padx": 8, "pady": 6}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("EDGAR — Console d'exploitation")
        self.geometry("820x620")
        self.minsize(720, 560)

        self._q: queue.Queue = queue.Queue()
        self._busy = False

        self.container = ttk.Frame(self)
        self.container.pack(fill="both", expand=True)
        self._show_home()
        self.after(120, self._drain)

    # ---- navigation ----
    def _clear(self) -> None:
        for w in self.container.winfo_children():
            w.destroy()

    def _show_home(self) -> None:
        self._clear()
        f = ttk.Frame(self.container, padding=24)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="EDGAR — Console d'exploitation",
                  font=("TkDefaultFont", 18, "bold")).pack(anchor="w")
        ttk.Label(f, text="Installation et synchronisation hors-ligne. "
                          "Les comptes restent propres à ce poste.",
                  foreground="#555").pack(anchor="w", pady=(2, 20))
        ttk.Button(f, text="🛠  Installer un poste", width=32,
                   command=self._show_install).pack(anchor="w", pady=6)
        ttk.Button(f, text="🔄  Synchroniser (hors-ligne)", width=32,
                   command=self._show_sync).pack(anchor="w", pady=6)

    def _show_install(self) -> None:
        self._clear()
        top = ttk.Frame(self.container, padding=(12, 10))
        top.pack(fill="x")
        ttk.Button(top, text="← Accueil", command=self._show_home).pack(side="left")
        ttk.Label(top, text="Installer un poste",
                  font=("TkDefaultFont", 13, "bold")).pack(side="left", padx=12)

        self.logbox = scrolledtext.ScrolledText(self.container, height=8, state="disabled",
                                                font=("TkFixedFont", 9))
        self.logbox.pack(side="bottom", fill="x", padx=12, pady=(4, 12))
        self._progress = ttk.Progressbar(self.container, mode="indeterminate")
        self._progress.pack(side="bottom", fill="x", padx=12, pady=(6, 0))

        body = ttk.Frame(self.container, padding=12)
        body.pack(fill="both", expand=True)

        # Pré-requis
        fp = ttk.LabelFrame(body, text="Pré-requis (Docker requis, jamais installé par l'outil)",
                            padding=8)
        fp.pack(fill="x", **PAD)
        self.inst_prereq = ttk.Label(fp, text="non vérifié")
        self.inst_prereq.pack(side="left")
        ttk.Button(fp, text="Vérifier", command=self._check_prereqs_ui).pack(side="right")

        # Bundle
        fb = ttk.LabelFrame(body, text="Bundle d'installation (disque)", padding=8)
        fb.pack(fill="x", **PAD)
        row = ttk.Frame(fb)
        row.pack(fill="x")
        self.inst_src = tk.StringVar()
        ttk.Entry(row, textvariable=self.inst_src).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Parcourir…",
                   command=lambda: self._pick_dir(self.inst_src)).pack(side="left", padx=4)
        ttk.Button(row, text="Analyser", command=self._analyse_install).pack(side="left")
        self.inst_detected = ttk.Label(fb, text="—", foreground="#555")
        self.inst_detected.pack(anchor="w", pady=(6, 0))
        self._inst_info: dict = {"app": False, "models": [], "bases": []}

        # À installer
        fo = ttk.LabelFrame(body, text="À installer", padding=8)
        fo.pack(fill="x", **PAD)
        self.inst_images = tk.BooleanVar(value=True)
        self.inst_code = tk.BooleanVar(value=False)
        self.inst_models = tk.BooleanVar(value=True)
        self.inst_bases = tk.BooleanVar(value=True)
        ttk.Checkbutton(fo, text="Charger les images Docker", variable=self.inst_images).pack(anchor="w")
        ttk.Checkbutton(fo, text="Remplacer aussi le code (préserve .env)",
                        variable=self.inst_code).pack(anchor="w", padx=20)
        ttk.Checkbutton(fo, text="Installer les modèles", variable=self.inst_models).pack(anchor="w")
        ttk.Checkbutton(fo, text="Importer les bases du bundle", variable=self.inst_bases).pack(anchor="w")

        # Configuration
        fc = ttk.LabelFrame(body, text="Configuration", padding=8)
        fc.pack(fill="x", **PAD)
        self.inst_host = tk.StringVar(value="localhost")
        self.inst_admin = tk.StringVar(value="admin")
        self.inst_pwd = tk.StringVar()
        for label, var, kw in (("Nom ou IP du serveur (certificat) :", self.inst_host, {}),
                               ("Identifiant admin :", self.inst_admin, {}),
                               ("Mot de passe admin :", self.inst_pwd, {"show": "•"})):
            r = ttk.Frame(fc)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=label, width=32).pack(side="left")
            ttk.Entry(r, textvariable=var, **kw).pack(side="left", fill="x", expand=True)

        bar = ttk.Frame(body)
        bar.pack(fill="x", **PAD)
        self.inst_run = ttk.Button(bar, text="Lancer l'installation", command=self._do_install)
        self.inst_run.pack(side="right")

        self._check_prereqs_ui()

    def _check_prereqs_ui(self) -> None:
        def work(_log):
            self._q.put(("__prereqs__", core.check_prereqs()))
        self._run_bg(work)

    def _show_prereqs(self, res: dict) -> None:
        if not getattr(self, "inst_prereq", None) or not self.inst_prereq.winfo_exists():
            return
        ok = res["docker"] and res["compose"]
        mark = lambda b: "✔" if b else "✗"
        self.inst_prereq.config(
            text=f"Docker {mark(res['docker'])}   Compose {mark(res['compose'])}   "
                 f"GPU {'✔' if res['gpu'] else '— (mode CPU)'}",
            foreground="#0a6" if ok else "#c00")

    def _analyse_install(self) -> None:
        src = self.inst_src.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showwarning("Installation", "Choisis un dossier de bundle valide.")
            return
        self._inst_info = core.detect_bundle(src)
        self.inst_detected.config(
            text=f"Application : {'oui' if self._inst_info['app'] else 'non'}   ·   "
                 f"modèles : {len(self._inst_info['models'])}   ·   "
                 f"bases : {len(self._inst_info['bases'])}")

    def _do_install(self) -> None:
        src = Path(self.inst_src.get().strip())
        host = self.inst_host.get().strip() or "localhost"
        admin = self.inst_admin.get().strip()
        pwd = self.inst_pwd.get()
        if not admin or not pwd:
            messagebox.showwarning("Installation", "Identifiant et mot de passe admin requis.")
            return
        info = self._inst_info

        def work(log):
            res = core.check_prereqs(log)
            if not res["docker"] or not res["compose"]:
                raise RuntimeError("Docker/compose introuvable — installez Docker puis relancez.")
            log("== Configuration (.env) ==")
            core.ensure_env_secret(log)
            if self.inst_images.get():
                log("== Images / code ==")
                core.import_app(src / "app", replace_code=self.inst_code.get(), log=log)
            else:
                core.compose_up(log)
            if self.inst_models.get():
                log("== Modèles ==")
                core.import_models(src / "models", log=log)
            if self.inst_bases.get():
                for bid in info.get("bases", []):
                    log(f"== Base {bid} ==")
                    core.import_base(src / "docs" / bid, log=log)
            log("== Certificat ==")
            core.gen_cert(host, log=log)
            log("== Attente de la disponibilité ==")
            core.wait_healthy(log)
            log("== Compte administrateur ==")
            log(core.bootstrap_admin(admin, pwd, log).strip())
            log(f"Terminé. Accès : https://{host}:8443")

        msg = (f"Installation terminée.\n\nAccès : https://{host}:8443\n"
               "Le mot de passe admin devra être changé au 1er login.")
        self._run_bg(work, self.inst_run, done_title="Installation", done_message=msg)

    # ---- écran synchronisation ----
    def _show_sync(self) -> None:
        self._clear()
        top = ttk.Frame(self.container, padding=(12, 10))
        top.pack(fill="x")
        ttk.Button(top, text="← Accueil", command=self._show_home).pack(side="left")
        ttk.Label(top, text="Synchronisation hors-ligne",
                  font=("TkDefaultFont", 13, "bold")).pack(side="left", padx=12)

        # Journal + barre de progression (en bas), créés avant les onglets qui s'en servent.
        self.logbox = scrolledtext.ScrolledText(self.container, height=9, state="disabled",
                                                font=("TkFixedFont", 9))
        self.logbox.pack(side="bottom", fill="x", padx=12, pady=(4, 12))
        self._progress = ttk.Progressbar(self.container, mode="indeterminate")
        self._progress.pack(side="bottom", fill="x", padx=12, pady=(6, 0))

        nb = ttk.Notebook(self.container)
        nb.pack(fill="both", expand=True, padx=12)
        self._build_export_tab(nb)
        self._build_import_tab(nb)

    # ---- onglet EXPORT (terre) ----
    def _build_export_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="Exporter (Terre)")

        dest = ttk.Frame(tab)
        dest.pack(fill="x", **PAD)
        ttk.Label(dest, text="Dossier de destination (disque) :").pack(side="left")
        self.exp_dest = tk.StringVar()
        ttk.Entry(dest, textvariable=self.exp_dest).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(dest, text="Parcourir…",
                   command=lambda: self._pick_dir(self.exp_dest)).pack(side="left")

        # ① Application
        self.exp_app = tk.BooleanVar(value=False)
        self.exp_infra = tk.BooleanVar(value=False)
        fa = ttk.LabelFrame(tab, text="① Application (mise à jour logicielle)", padding=8)
        fa.pack(fill="x", **PAD)
        ttk.Checkbutton(fa, text="Inclure le code + les images Docker",
                        variable=self.exp_app).pack(anchor="w")
        ttk.Checkbutton(fa, text="+ infrastructure ollama/qdrant (installation complète, plus lourd)",
                        variable=self.exp_infra).pack(anchor="w", padx=20)

        # ② Documentaire
        fd = ttk.LabelFrame(tab, text="② Documentaire (bases)", padding=8)
        fd.pack(fill="both", expand=True, **PAD)
        self.exp_bases_box = ttk.Frame(fd)
        self.exp_bases_box.pack(fill="x", anchor="w")
        self.exp_bases_vars: dict[str, tk.BooleanVar] = {}
        self.exp_files = tk.StringVar(value="with")
        row = ttk.Frame(fd)
        row.pack(fill="x", pady=(6, 0))
        ttk.Radiobutton(row, text="Avec les fichiers", value="with",
                        variable=self.exp_files).pack(side="left")
        ttk.Radiobutton(row, text="Base de données seule (sans fichiers)", value="db",
                        variable=self.exp_files).pack(side="left", padx=14)

        # ③ Modèles
        fm = ttk.LabelFrame(tab, text="③ Modèles (LLM)", padding=8)
        fm.pack(fill="x", **PAD)
        self.exp_models_box = ttk.Frame(fm)
        self.exp_models_box.pack(fill="x", anchor="w")
        self.exp_models_vars: dict[str, tk.BooleanVar] = {}

        bar = ttk.Frame(tab)
        bar.pack(fill="x", **PAD)
        ttk.Button(bar, text="Rafraîchir les listes", command=self._refresh_lists).pack(side="left")
        self.exp_run = ttk.Button(bar, text="Lancer l'export", command=self._do_export)
        self.exp_run.pack(side="right")

        self._refresh_lists()

    def _refresh_lists(self) -> None:
        for box in (self.exp_bases_box, self.exp_models_box):
            for w in box.winfo_children():
                w.destroy()
        ttk.Label(self.exp_bases_box, text="chargement…").pack(anchor="w")

        def work(log):
            bases = core.list_bases()
            models = core.installed_models()
            self._q.put(("__lists__", bases, models))

        self._run_bg(work)

    def _populate_lists(self, bases, models) -> None:
        if not self.exp_bases_box.winfo_exists():
            return
        for w in self.exp_bases_box.winfo_children():
            w.destroy()
        self.exp_bases_vars.clear()
        if not bases:
            ttk.Label(self.exp_bases_box, text="(aucune base)").pack(anchor="w")
        for bid, name in bases:
            v = tk.BooleanVar(value=False)
            self.exp_bases_vars[bid] = v
            ttk.Checkbutton(self.exp_bases_box, text=f"{name}  ({bid})", variable=v).pack(anchor="w")
        for w in self.exp_models_box.winfo_children():
            w.destroy()
        self.exp_models_vars.clear()
        if not models:
            ttk.Label(self.exp_models_box, text="(aucun modèle)").pack(anchor="w")
        for spec in models:
            v = tk.BooleanVar(value=("bge-m3" in spec))
            self.exp_models_vars[spec] = v
            ttk.Checkbutton(self.exp_models_box, text=spec, variable=v).pack(anchor="w")

    def _do_export(self) -> None:
        dest = self.exp_dest.get().strip()
        if not dest:
            messagebox.showwarning("Export", "Choisis un dossier de destination.")
            return
        do_app = self.exp_app.get()
        infra = self.exp_infra.get()
        bases = [b for b, v in self.exp_bases_vars.items() if v.get()]
        with_files = self.exp_files.get() == "with"
        models = [m for m, v in self.exp_models_vars.items() if v.get()]
        if not (do_app or bases or models):
            messagebox.showwarning("Export", "Rien de sélectionné à exporter.")
            return

        def work(log):
            d = Path(dest)
            if do_app:
                log("== Application ==")
                core.export_app(d / "app", include_infra=infra, log=log)
            if models:
                log("== Modèles ==")
                core.export_models(models, d / "models", log=log)
            for bid in bases:
                log(f"== Base {bid} ==")
                core.export_base(bid, d / "docs" / bid, with_files=with_files, log=log)
            log("Export terminé.")

        self._run_bg(work, self.exp_run, done_title="Export")

    # ---- onglet IMPORT (mer) ----
    def _build_import_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="Importer (Mer)")

        src = ttk.Frame(tab)
        src.pack(fill="x", **PAD)
        ttk.Label(src, text="Dossier du bundle reçu :").pack(side="left")
        self.imp_src = tk.StringVar()
        ttk.Entry(src, textvariable=self.imp_src).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(src, text="Parcourir…",
                   command=lambda: self._pick_dir(self.imp_src)).pack(side="left")
        ttk.Button(src, text="Analyser", command=self._analyse_bundle).pack(side="left", padx=4)

        ttk.Label(tab, text="🔒  Les comptes locaux ne sont pas modifiés.",
                  foreground="#0a6").pack(anchor="w", padx=8)

        self.imp_box = ttk.LabelFrame(tab, text="Contenu détecté", padding=8)
        self.imp_box.pack(fill="both", expand=True, **PAD)
        ttk.Label(self.imp_box, text="Choisis un dossier puis « Analyser ».").pack(anchor="w")
        self.imp_app = tk.BooleanVar(value=False)
        self.imp_models = tk.BooleanVar(value=False)
        self.imp_bases_vars: dict[str, tk.BooleanVar] = {}

        bar = ttk.Frame(tab)
        bar.pack(fill="x", **PAD)
        self.imp_run = ttk.Button(bar, text="Lancer l'import", command=self._do_import, state="disabled")
        self.imp_run.pack(side="right")

    def _analyse_bundle(self) -> None:
        src = self.imp_src.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showwarning("Import", "Choisis un dossier valide.")
            return
        info = core.detect_bundle(src)
        for w in self.imp_box.winfo_children():
            w.destroy()
        self.imp_bases_vars.clear()
        any_found = False
        if info["app"]:
            any_found = True
            self.imp_app.set(True)
            ttk.Checkbutton(self.imp_box, text="Application (code + images)",
                            variable=self.imp_app).pack(anchor="w")
        if info["models"]:
            any_found = True
            self.imp_models.set(True)
            ttk.Checkbutton(self.imp_box,
                            text=f"Modèles ({len(info['models'])})",
                            variable=self.imp_models).pack(anchor="w")
        for bid in info["bases"]:
            any_found = True
            v = tk.BooleanVar(value=True)
            self.imp_bases_vars[bid] = v
            ttk.Checkbutton(self.imp_box, text=f"Base : {bid}", variable=v).pack(anchor="w")
        if not any_found:
            ttk.Label(self.imp_box, text="Aucun contenu reconnu dans ce dossier.").pack(anchor="w")
        self.imp_run.config(state="normal" if any_found else "disabled")

    def _do_import(self) -> None:
        src = Path(self.imp_src.get().strip())
        do_app = self.imp_app.get()
        do_models = self.imp_models.get()
        bases = [b for b, v in self.imp_bases_vars.items() if v.get()]
        if not (do_app or do_models or bases):
            messagebox.showwarning("Import", "Rien de sélectionné à importer.")
            return
        if not messagebox.askyesno("Import",
                                   "Confirmer l'import ?\nLes comptes locaux ne seront pas modifiés."):
            return

        def work(log):
            if do_app:
                log("== Application ==")
                core.import_app(src / "app", log=log)
            if do_models:
                log("== Modèles ==")
                core.import_models(src / "models", log=log)
            for bid in bases:
                log(f"== Base {bid} ==")
                core.import_base(src / "docs" / bid, log=log)
            log("Import terminé.")

        self._run_bg(work, self.imp_run, done_title="Import")

    # ---- utilitaires ----
    def _pick_dir(self, var: tk.StringVar) -> None:
        d = filedialog.askdirectory()
        if d:
            var.set(d)

    def log(self, msg: str) -> None:
        self._q.put(str(msg))

    def _run_bg(self, fn, button: ttk.Button | None = None, done_title: str | None = None,
                done_message: str | None = None) -> None:
        if self._busy:
            messagebox.showinfo("Occupé", "Une opération est déjà en cours.")
            return
        self._busy = True
        if button is not None:
            button.config(state="disabled")
        if hasattr(self, "_progress"):
            self._progress.start(12)

        def wrap():
            ok, err = True, ""
            try:
                fn(self.log)
            except Exception as exc:  # noqa: BLE001
                ok, err = False, f"{type(exc).__name__} : {exc}"
                self.log("ERREUR : " + err)
            finally:
                self._q.put(("__done__", button, done_title, ok, err, done_message))

        threading.Thread(target=wrap, daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__done__":
                    _, btn, title, ok, err, msg = item
                    self._busy = False
                    if hasattr(self, "_progress"):
                        self._progress.stop()
                    if btn is not None:
                        btn.config(state="normal")
                    if title:
                        if ok:
                            messagebox.showinfo(title, msg or f"{title} terminé.")
                        else:
                            messagebox.showerror(title, f"{title} : échec.\n{err}")
                elif isinstance(item, tuple) and item and item[0] == "__lists__":
                    self._populate_lists(item[1], item[2])
                elif isinstance(item, tuple) and item and item[0] == "__prereqs__":
                    self._show_prereqs(item[1])
                elif hasattr(self, "logbox"):
                    self.logbox.config(state="normal")
                    self.logbox.insert("end", str(item) + "\n")
                    self.logbox.see("end")
                    self.logbox.config(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._drain)


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
