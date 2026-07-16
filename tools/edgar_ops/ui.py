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
        ttk.Button(f, text="🔄  Synchroniser (terre ↔ mer)", width=32,
                   command=self._show_sync).pack(anchor="w", pady=6)

    def _show_install(self) -> None:
        messagebox.showinfo("Installer",
                            "L'assistant d'installation arrive au prochain lot (C).\n"
                            "La synchronisation est déjà disponible.")

    # ---- écran synchronisation ----
    def _show_sync(self) -> None:
        self._clear()
        top = ttk.Frame(self.container, padding=(12, 10))
        top.pack(fill="x")
        ttk.Button(top, text="← Accueil", command=self._show_home).pack(side="left")
        ttk.Label(top, text="Synchronisation terre ↔ mer",
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

    def _run_bg(self, fn, button: ttk.Button | None = None, done_title: str | None = None) -> None:
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
                self._q.put(("__done__", button, done_title, ok, err))

        threading.Thread(target=wrap, daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__done__":
                    _, btn, title, ok, err = item
                    self._busy = False
                    if hasattr(self, "_progress"):
                        self._progress.stop()
                    if btn is not None:
                        btn.config(state="normal")
                    if title:
                        if ok:
                            messagebox.showinfo(title, f"{title} terminé.")
                        else:
                            messagebox.showerror(title, f"{title} : échec.\n{err}")
                elif isinstance(item, tuple) and item and item[0] == "__lists__":
                    self._populate_lists(item[1], item[2])
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
