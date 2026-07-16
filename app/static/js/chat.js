// EDGAR v2 — interface de chat (vanilla, aucun CDN).
// Streaming SSE, rendu Markdown assaini, panneau d'expérimentation (leviers),
// affichage des mots-clés et aperçu PDF positionné sur la page citée.
"use strict";

function renderMarkdown(el, text) {
  if (window.marked && window.DOMPurify) {
    const html = window.marked.parse(text, { breaks: true });
    el.innerHTML = window.DOMPurify.sanitize(html);
    if (window.hljs) el.querySelectorAll("pre code").forEach((b) => window.hljs.highlightElement(b));
  } else {
    el.textContent = text;
  }
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// --- Affichage des mots-clés (préférence locale) ---
function keywordsShown() { return localStorage.getItem("edgar_kw") !== "0"; }
function applyKeywordVisibility() {
  const show = keywordsShown();
  document.querySelectorAll(".src-keywords").forEach((k) => { k.style.display = show ? "" : "none"; });
}

// --- Construction du bloc Sources (miroir du gabarit serveur) ---
function buildSources(sources, baseId) {
  if (!sources || !sources.length) return null;
  const wrap = el("div", "sources");
  wrap.appendChild(el("div", "sources-title", "Sources"));
  sources.forEach((s) => {
    const d = el("details", "source");
    const sum = el("summary");
    sum.appendChild(el("span", "src-n", "[" + s.n + "]"));
    const sf = el("span", "src-file", (s.file || "").split("/").pop());
    if (s.file) sf.title = s.file;  // chemin complet (sous-dossier) au survol
    sum.appendChild(sf);
    if (s.page) sum.appendChild(el("span", "src-meta", "· p." + s.page));
    if (s.section) sum.appendChild(el("span", "src-meta", "· " + s.section));
    if (s.score !== null && s.score !== undefined)
      sum.appendChild(el("span", "src-score", Math.round(s.score * 100) + " %"));
    d.appendChild(sum);

    const cite = el("div", "source-cite");
    cite.appendChild(document.createTextNode(s.text || ""));
    if (s.keywords && s.keywords.length) {
      const kw = el("div", "src-keywords", "Mots-clés : " + s.keywords.join(", "));
      cite.appendChild(kw);
    }
    const actions = el("div", "source-actions");
    const file = s.file || "";
    // Encode chaque segment mais conserve les « / » des sous-dossiers.
    const encPath = file.split("/").map(encodeURIComponent).join("/");
    const url = "/doc/" + encodeURIComponent(baseId) + "/" + encPath;
    const page = s.page || 1;
    if (file.toLowerCase().endsWith(".pdf")) {
      const open = el("a", null, "Ouvrir p." + page);
      open.href = url + "#page=" + page; open.target = "_blank"; open.rel = "noopener";
      actions.appendChild(open);
      const prev = el("button", "link-btn", "Aperçu");
      prev.type = "button"; prev.setAttribute("data-doc", url + "#page=" + page);
      actions.appendChild(prev);
    }
    const dl = el("a", null, "Télécharger");
    dl.href = url + "?download=1";
    actions.appendChild(dl);
    cite.appendChild(actions);
    d.appendChild(cite);
    wrap.appendChild(d);
  });
  return wrap;
}

function buildDiagnostics(diag) {
  if (!diag) return null;
  const d = el("details", "diag");
  d.appendChild(el("summary", null, "Diagnostic de recherche"));
  const body = el("div", "diag-body");
  if (diag.queries) body.appendChild(el("div", null, "Variantes : " + diag.queries.join(" · ")));
  if (diag.counts)
    body.appendChild(el("div", null, "Candidats fusionnés : " + diag.counts.candidats_fusionnes +
      " — retenus : " + (diag.counts.retenus || 0)));
  if (diag.scores_avant_rerank)
    body.appendChild(el("div", null, "Scores RRF : " + diag.scores_avant_rerank.join(", ")));
  if (diag.scores_apres_rerank)
    body.appendChild(el("div", null, "Après rerank : " + diag.scores_apres_rerank.join(", ")));
  if (diag.timings_ms) {
    const t = Object.entries(diag.timings_ms).map(([k, v]) => k + "=" + v).join(" ");
    body.appendChild(el("div", null, "Temps (ms) : " + t));
  }
  d.appendChild(body);
  return d;
}

// --- Leviers du panneau -> objet params envoyé à chaque requête ---
function collectParams() {
  const panel = document.querySelector("[data-panel]");
  const p = {};
  if (!panel) return p;
  panel.querySelectorAll("[data-p]").forEach((elx) => {
    const k = elx.getAttribute("data-p");
    if (elx.type === "checkbox") p[k] = elx.checked;
    else if (elx.type === "range") p[k] = (elx.step && elx.step.indexOf(".") >= 0) ? parseFloat(elx.value) : parseInt(elx.value, 10);
    else p[k] = elx.value;
  });
  return p;
}

async function streamChat(chat, question) {
  const params = collectParams();
  const messages = chat.querySelector("[data-messages]");
  const empty = messages.querySelector(".empty-state");
  if (empty) empty.remove();

  const um = el("div", "msg msg-user");
  um.appendChild(el("div", "bubble", question));
  messages.appendChild(um);

  const am = el("div", "msg msg-assistant");
  const abubble = el("div", "bubble");
  const md = el("div", "md-content");
  const thinking = el("span", "thinking-text");
  abubble.appendChild(md); abubble.appendChild(thinking);
  const stepsEl = el("div", "steps");
  am.appendChild(abubble);   // encart de réponse
  am.appendChild(stepsEl);   // étapes EN DESSOUS de l'encart
  messages.appendChild(am);
  messages.scrollTop = messages.scrollHeight;

  // « Réflexion en cours… » DANS l'encart de réponse (comme avant).
  const full = "Réflexion en cours…";
  let ci = 0;
  const typer = setInterval(() => {
    if (ci < full.length) { ci++; thinking.textContent = full.slice(0, ci); }
    else { clearInterval(typer); thinking.innerHTML = full + '<span class="blink">▍</span>'; }
  }, 45);
  let thinkingCleared = false;
  const clearThinking = () => { if (!thinkingCleared) { thinkingCleared = true; clearInterval(typer); thinking.remove(); } };

  // Étapes sous l'encart : chaque ligne est créée UNE fois (le spinner n'est
  // jamais recréé -> animation fluide) ; seul le chrono de l'étape active est mis à jour.
  const steps = [];
  const fmt = (ms) => (ms / 1000).toFixed(1) + " s";
  function finalize(s) {
    if (!s || s.done) return;
    s.done = true; s.elapsed = Date.now() - s.start;
    s.row.classList.add("done");
    s.ic.textContent = "✓";
    s.tm.textContent = fmt(s.elapsed);
  }
  function startStep(label) {
    finalize(steps[steps.length - 1]);
    const row = el("div", "step");
    const ic = el("span", "step-ic"); ic.innerHTML = '<span class="spin"></span>';
    const tm = el("span", "step-time", "0.0 s");
    row.appendChild(ic); row.appendChild(el("span", "step-label", label)); row.appendChild(tm);
    stepsEl.appendChild(row);
    steps.push({ label: label, start: Date.now(), done: false, row: row, ic: ic, tm: tm });
    messages.scrollTop = messages.scrollHeight;
  }
  const ticker = setInterval(() => {
    const s = steps[steps.length - 1];
    if (s && !s.done) s.tm.textContent = fmt(Date.now() - s.start);
  }, 100);

  let meta = null, buffer = "";
  const body = {
    base_id: chat.dataset.baseId,
    conversation_id: chat.dataset.convId || null,
    question: question,
    csrf_token: chat.dataset.csrf,
    params: params,
  };

  try {
    const resp = await fetch("/api/chat/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let acc = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      acc += dec.decode(value, { stream: true });
      let idx;
      while ((idx = acc.indexOf("\n\n")) >= 0) {
        const raw = acc.slice(0, idx); acc = acc.slice(idx + 2);
        let event = "message", data = "";
        raw.split("\n").forEach((line) => {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        });
        let payload = {};
        try { payload = JSON.parse(data); } catch (e) { payload = {}; }
        if (event === "step") {
          startStep(payload.label || "…");
        } else if (event === "meta") {
          meta = payload;
          if (payload.conversation_id) chat.dataset.convId = payload.conversation_id;
        } else if (event === "token") {
          clearThinking();
          buffer += payload.t || "";
          md.textContent = buffer;
          messages.scrollTop = messages.scrollHeight;
        } else if (event === "error") {
          clearThinking();
          buffer += "\n\n_" + (payload.message || "Erreur.") + "_";
          md.textContent = buffer;
        } else if (event === "done") {
          if (payload.conversation_id) chat.dataset.convId = payload.conversation_id;
        }
      }
    }
  } catch (e) {
    clearThinking();
    md.textContent = "Erreur de communication avec le serveur.";
  }

  // Fin de la recherche : on efface le détail des étapes, on ne garde que le temps total.
  clearThinking();
  finalize(steps[steps.length - 1]);
  clearInterval(ticker);
  const total = steps.reduce((a, s) => a + (s.elapsed || 0), 0);
  stepsEl.remove();

  if (meta && meta.search_mode) md.innerHTML = "<em>Mode recherche : extraits pertinents ci-dessous.</em>";
  else renderMarkdown(md, buffer);

  if (steps.length) am.appendChild(el("div", "steps-recap", "Recherche : " + fmt(total)));

  if (meta) {
    const src = buildSources(meta.sources, chat.dataset.baseId);
    if (src) am.appendChild(src);
    const dg = buildDiagnostics(meta.diagnostics);
    if (dg) am.appendChild(dg);
    applyKeywordVisibility();
  }
  messages.scrollTop = messages.scrollHeight;
}

async function postJson(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.ok ? r.json() : null;
}

document.addEventListener("DOMContentLoaded", () => {
  // Confirmation avant soumission (formulaires destructifs) — CSP-clean.
  document.querySelectorAll("form[data-confirm]").forEach((f) => {
    f.addEventListener("submit", (e) => { if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault(); });
  });

  document.querySelectorAll(".md-content[data-md]").forEach((e) => renderMarkdown(e, e.textContent.trim()));
  applyKeywordVisibility();

  const sel = document.querySelector("[data-base-select]");
  if (sel) sel.addEventListener("change", () => {
    const target = sel.getAttribute("data-target") || "/chat";
    window.location.href = target + "?base=" + encodeURIComponent(sel.value);
  });

  const toggle = document.querySelector("[data-sidebar-toggle]");
  const sidebar = document.querySelector("[data-sidebar]");
  if (toggle && sidebar) toggle.addEventListener("click", () => sidebar.classList.toggle("open"));

  // --- Aperçu PDF (délégation : historique + sources dynamiques) ---
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-doc]");
    if (!btn) return;
    const cite = btn.closest(".source-cite");
    let frame = cite.querySelector(".doc-preview");
    if (frame) { frame.remove(); return; }
    frame = el("iframe", "doc-preview");
    frame.src = btn.getAttribute("data-doc");
    cite.appendChild(frame);
  });

  // --- Renommer une conversation (icône crayon -> boîte de saisie) ---
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-rename]");
    if (!btn) return;
    const wrap = btn.closest(".conv-item-wrap");
    const link = wrap && wrap.querySelector(".conv-item");
    if (!link) return;
    const current = link.textContent.trim();
    const name = window.prompt("Renommer la conversation :", current);
    if (name === null) return;
    const title = name.trim();
    if (!title || title === current) return;
    const chatEl = document.querySelector(".chat");
    const body = new URLSearchParams({ title, csrf_token: (chatEl && chatEl.dataset.csrf) || "" });
    try {
      const r = await fetch(btn.getAttribute("data-rename"), {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });
      if (!r.ok) { window.alert("Échec du renommage."); return; }
      const data = await r.json();
      link.textContent = data.title || title;
    } catch (_) {
      window.alert("Échec du renommage.");
    }
  });

  // --- Panneau d'expérimentation ---
  const panel = document.querySelector("[data-panel]");
  const chat = document.querySelector(".chat");
  if (panel) {
    const overlay = document.querySelector(".panel-overlay");
    const togglePanel = () => {
      const open = panel.classList.toggle("open");
      if (overlay) overlay.hidden = !open;
    };
    document.querySelectorAll("[data-panel-toggle]").forEach((b) => b.addEventListener("click", togglePanel));
    panel.querySelectorAll('input[type="range"]').forEach((r) => {
      const out = panel.querySelector('[data-out="' + r.getAttribute("data-p") + '"]');
      if (out) r.addEventListener("input", () => { out.textContent = r.value; });
    });
    const kw = panel.querySelector("[data-show-keywords]");
    if (kw) { kw.checked = keywordsShown();
      kw.addEventListener("change", () => { localStorage.setItem("edgar_kw", kw.checked ? "1" : "0"); applyKeywordVisibility(); }); }
    const status = panel.querySelector("[data-panel-status]");
    const flash = (t) => { if (status) { status.textContent = t; setTimeout(() => { status.textContent = ""; }, 2500); } };
    panel.querySelector("[data-panel-save]").addEventListener("click", async () => {
      const ok = await postJson("/api/settings", { csrf_token: chat.dataset.csrf, params: collectParams() });
      flash(ok ? "Réglages enregistrés pour votre session." : "Échec de l'enregistrement.");
    });
    panel.querySelector("[data-panel-reset]").addEventListener("click", async () => {
      const res = await postJson("/api/settings/reset", { csrf_token: chat.dataset.csrf });
      if (res && res.params) {
        Object.entries(res.params).forEach(([k, v]) => {
          const c = panel.querySelector('[data-p="' + k + '"]');
          if (!c) return;
          if (c.type === "checkbox") c.checked = !!v; else c.value = v;
          const out = panel.querySelector('[data-out="' + k + '"]'); if (out) out.textContent = v;
        });
        flash("Réglages réinitialisés aux défauts globaux.");
      }
    });
  }

  // --- Import / ré-analyse en tâche de fond (progression dynamique) ---
  const jobPanel = document.querySelector("[data-job-panel]");
  const jobDone = (job) => job.status === "done" || job.status === "stopped";
  function fmtETA(job) {
    if (jobDone(job)) return job.status === "stopped" ? "arrêté" : "terminé";
    if (!job.total || !job.done) return "";
    const el = (Date.now() / 1000) - job.started;
    const s = Math.max(0, Math.round(el / job.done * (job.total - job.done)));
    return "≈ " + (s >= 60 ? Math.round(s / 60) + " min" : s + " s") + " restant";
  }
  function fmtBytes(n) {
    n = n || 0;
    if (n < 1024) return n + " o";
    if (n < 1048576) return (n / 1024).toFixed(0) + " Ko";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " Mo";
    return (n / 1073741824).toFixed(2) + " Go";
  }
  function fmtDur(s) {
    s = Math.max(0, s || 0);
    if (s < 60) return (Math.round(s * 10) / 10) + " s";
    return Math.floor(s / 60) + " min " + Math.round(s % 60) + " s";
  }
  function renderJob(job) {
    if (!jobPanel) return;
    jobPanel.hidden = false;
    const q = (s) => jobPanel.querySelector(s);
    const pct = job.total ? Math.round(job.done / job.total * 100) : 0;
    q("[data-job-fill]").style.width = pct + "%";
    q("[data-job-progress]").textContent = job.done + " / " + job.total;
    q("[data-job-ok]").textContent = job.succeeded;
    q("[data-job-skip]").textContent = job.skipped;
    q("[data-job-fail]").textContent = job.failed;
    q("[data-job-eta]").textContent = fmtETA(job);
    const done = jobDone(job);
    q(".job-title").textContent = job.status === "stopped" ? "Import arrêté"
      : (job.status === "done" ? "Import terminé" : "Import en cours…");
    let cur = "";
    if (job.status === "stopped") cur = "Arrêté après le fichier en cours.";
    else if (job.status === "done") cur = "Terminé.";
    else if (job.sub && job.sub.stage) {
      const L = { analyse: "Analyse…", ocr: "OCR", keywords: "Mots-clés", index: "Indexation" };
      const f = (job.sub.file || job.current || "").split("/").pop();
      cur = "En cours : " + f + " — " + (L[job.sub.stage] || job.sub.stage);
      if (job.sub.total) cur += " " + job.sub.done + "/" + job.sub.total;
    } else if (job.current) {
      cur = "En cours : " + job.current.split("/").pop();
    }
    if (!done && job.paused) cur += "  ⏸ en pause (requête en cours)";
    q("[data-job-current]").textContent = cur;
    const stopBtn = q("[data-job-stop]");
    if (stopBtn) {
      stopBtn.hidden = done;
      if (!done) {
        stopBtn.disabled = !!job.stop;
        stopBtn.textContent = job.stop ? "Arrêt en cours…" : "Arrêter l'import";
      }
    }
    const stats = q("[data-job-stats]");
    if (done) {
      const dur = (job.finished && job.started) ? fmtDur(job.finished - job.started) : "—";
      stats.hidden = false;
      stats.innerHTML = "⏱ Durée totale : <b>" + dur + "</b> · 💾 Taille indexée : <b>"
                      + fmtBytes(job.bytes) + "</b>";
    } else {
      stats.hidden = true;
    }
    if (job.failures && job.failures.length) {
      q("[data-job-failures]").hidden = false;
      q("[data-job-failure-list]").innerHTML = job.failures
        .map((f) => "<li><b>" + f.file + "</b> : " + (f.reason || "") + "</li>").join("");
    }
  }
  function trackJob(jobId) {
    if (jobPanel) jobPanel.dataset.jobId = jobId;
    const es = new EventSource("/contribute/jobs/" + jobId + "/stream");
    es.addEventListener("progress", (e) => renderJob(JSON.parse(e.data)));
    es.addEventListener("done", (e) => {
      renderJob(JSON.parse(e.data));
      es.close();
      // Sur la page Documents, rafraîchir pour afficher les nouveaux statuts.
      if (document.querySelector("[data-reload-on-job]")) setTimeout(() => location.reload(), 1500);
    });
    es.addEventListener("error", () => es.close());
  }
  // Bouton « Arrêter l'import » : arrêt souple (le serveur finit le fichier en cours).
  if (jobPanel) {
    const stopBtn = jobPanel.querySelector("[data-job-stop]");
    if (stopBtn) stopBtn.addEventListener("click", async () => {
      const jobId = jobPanel.dataset.jobId;
      if (!jobId) return;
      stopBtn.disabled = true;
      stopBtn.textContent = "Arrêt en cours…";
      try {
        const fd = new FormData();
        fd.append("csrf_token", jobPanel.dataset.csrf || "");
        await fetch("/contribute/jobs/" + jobId + "/stop", { method: "POST", body: fd });
      } catch (err) { /* le flux SSE reflétera l'état */ }
    });
  }
  document.querySelectorAll("[data-import-form]").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = e.submitter || form.querySelector("[data-action]");
      const action = btn ? btn.getAttribute("data-action") : form.getAttribute("action");
      try {
        // FormData(form, btn) inclut le name/value du bouton cliqué (ex. reindex 0/1).
        const r = await fetch(action, { method: "POST", body: new FormData(form, btn) });
        const data = await r.json();
        if (data.busy && data.job_id) {
          alert("Un import est déjà en cours pour cette base. Il s'affiche ci-dessous ; réessayez une fois terminé.");
          trackJob(data.job_id);
        } else if (data.job_id) {
          trackJob(data.job_id);
        } else {
          alert(data.error || "Échec du lancement.");
        }
      } catch (err) { alert("Erreur réseau."); }
    });
  });

  // Au chargement : si un import tourne déjà (en arrière-plan), réafficher l'encart.
  if (jobPanel) {
    const ab = jobPanel.getAttribute("data-active-base") || "";
    fetch("/contribute/jobs/active" + (ab ? "?base=" + encodeURIComponent(ab) : ""))
      .then((r) => r.json())
      .then((d) => { if (d.job && d.job.id) { renderJob(d.job); trackJob(d.job.id); } })
      .catch(() => {});
  }

  // Boutons d'action d'un dossier (dans le <summary>) : ne pas replier/déplier au clic.
  document.querySelectorAll(".tree-actions").forEach((el) => {
    el.addEventListener("click", (e) => e.stopPropagation());
  });

  // --- Recherche de documents : filtre live (nom + statut), arbre replié ---
  const docSearch = document.querySelector("[data-doc-search]");
  const docStatus = document.querySelector("[data-doc-status]");
  if (docSearch || docStatus) {
    const rows = Array.from(document.querySelectorAll("[data-doc-row]"));
    const folders = Array.from(document.querySelectorAll("[data-doc-node]"));
    const empty = document.querySelector("[data-doc-empty]");
    const applyFilter = () => {
      const q = (docSearch ? docSearch.value : "").trim().toLowerCase();
      const st = docStatus ? docStatus.value : "";
      let visible = 0;
      rows.forEach((r) => {
        const okName = !q || (r.getAttribute("data-filename") || "").includes(q);
        const okStatus = !st || r.getAttribute("data-status") === st;
        const show = okName && okStatus;
        r.hidden = !show;
        if (show) visible++;
      });
      // Masque les dossiers sans fichier visible ; ouvre l'arbre pendant un filtre.
      folders.forEach((f) => {
        const anyVisible = !!f.querySelector("[data-doc-row]:not([hidden])");
        f.hidden = !anyVisible;
        if (q || st) f.open = anyVisible;
      });
      if (empty) empty.hidden = visible !== 0 || rows.length === 0;
    };
    if (docSearch) docSearch.addEventListener("input", applyFilter);
    if (docStatus) docStatus.addEventListener("change", applyFilter);
  }

  // --- Arbre documents : tout replier / tout déplier ---
  const treeCollapse = document.querySelector("[data-tree-collapse]");
  const treeExpand = document.querySelector("[data-tree-expand]");
  const setAllDirs = (open) => document.querySelectorAll(".tree-dir").forEach((d) => { d.open = open; });
  if (treeCollapse) treeCollapse.addEventListener("click", () => setAllDirs(false));
  if (treeExpand) treeExpand.addEventListener("click", () => setAllDirs(true));

  // --- Résumé LLM d'un document (modale + streaming SSE, avec cache) ---
  const summaryModal = document.querySelector("[data-summary-modal]");
  if (summaryModal) {
    const title = summaryModal.querySelector("[data-summary-title]");
    const meta = summaryModal.querySelector("[data-summary-meta]");
    const body = summaryModal.querySelector("[data-summary-body]");
    const regenBtn = summaryModal.querySelector("[data-summary-regen]");
    let es = null, current = null;
    const close = () => {
      if (es) { es.close(); es = null; }
      summaryModal.hidden = true;
    };
    const fmtDate = (iso) => {
      if (!iso) return "";
      const d = new Date(iso);
      return isNaN(d) ? "" : d.toLocaleString("fr-FR");
    };
    const run = (file, base, force) => {
      current = { file: file, base: base };
      if (es) es.close();
      title.textContent = "Résumé — " + file.split("/").pop();
      regenBtn.hidden = true;
      regenBtn.disabled = true;
      meta.innerHTML = '<span class="spin"></span> '
        + (force ? "Régénération…" : "Recherche d'un résumé enregistré…");
      body.innerHTML = '<div class="summary-loading"><span class="spin"></span>'
                     + ' Génération de la synthèse en cours…</div>';
      summaryModal.hidden = false;
      let buffer = "", metaText = "", cached = false;
      const url = "/documents/summary?base=" + encodeURIComponent(base)
                + "&file=" + encodeURIComponent(file) + (force ? "&force=1" : "");
      es = new EventSource(url);
      es.addEventListener("meta", (e) => {
        const d = JSON.parse(e.data);
        cached = !!d.cached;
        metaText = cached
          ? "Résumé enregistré" + (d.created_at ? " le " + fmtDate(d.created_at) : "")
            + " · " + d.chunks + " extrait(s)."
          : "Synthèse de " + d.chunks + " extrait(s)"
            + (d.truncated ? " (document volumineux : début synthétisé)." : ".");
        meta.innerHTML = cached ? metaText : ('<span class="spin"></span> ' + metaText);
      });
      es.addEventListener("token", (e) => {
        if (!buffer) { body.textContent = ""; meta.textContent = metaText; }  // 1er jeton : retire le spinner
        buffer += JSON.parse(e.data).t;
        renderMarkdown(body, buffer);
      });
      es.addEventListener("error", (e) => {
        let msg = "Erreur lors de la génération du résumé.";
        try { msg = JSON.parse(e.data).message; } catch (_) {}
        meta.textContent = msg;
        if (!buffer) body.textContent = "";
        regenBtn.hidden = false; regenBtn.disabled = false;
      });
      es.addEventListener("done", () => {
        if (!buffer) body.textContent = "Aucun contenu à synthétiser.";
        regenBtn.hidden = false; regenBtn.disabled = false;
        if (es) { es.close(); es = null; }
      });
    };
    summaryModal.querySelector("[data-summary-close]").addEventListener("click", close);
    summaryModal.addEventListener("click", (e) => { if (e.target === summaryModal) close(); });
    regenBtn.addEventListener("click", () => { if (current) run(current.file, current.base, true); });
    document.querySelectorAll("[data-summary]").forEach((btn) => {
      btn.addEventListener("click", () => run(btn.getAttribute("data-file"),
                                              btn.getAttribute("data-base"), false));
    });
  }

  // --- Validation dynamique des formulaires (inscription / mot de passe) ---
  document.querySelectorAll("[data-validate-form]").forEach((form) => {
    const submit = form.querySelector('[type="submit"]');
    const inputs = Array.from(form.querySelectorAll("[data-rule]"));
    if (!inputs.length) return;
    const pwInput = form.querySelector("[data-pw-input]");
    const pwRulesEl = form.querySelector("[data-pw-rules]");
    const reUser = /^[a-zà-ÿ]+([-'][a-zà-ÿ]+)*\.[a-zà-ÿ]+([-'][a-zà-ÿ]+)*$/;
    const reEmail = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
    const pwChecks = (v) => ({
      len: v.length >= 12, upper: /[A-Z]/.test(v), lower: /[a-z]/.test(v),
      digit: /[0-9]/.test(v), special: /[^A-Za-z0-9]/.test(v),
    });
    const fieldValid = (inp) => {
      const v = inp.value || "";
      switch (inp.getAttribute("data-rule")) {
        case "username": return reUser.test(v.trim());
        case "email": return reEmail.test(v.trim());
        case "password": { const c = pwChecks(v); return c.len && c.upper && c.lower && c.digit && c.special; }
        case "confirm": return v.length > 0 && !!pwInput && v === pwInput.value;
        default: return v.trim().length > 0;  // required
      }
    };
    const update = () => {
      let allOk = true;
      inputs.forEach((inp) => {
        const ok = fieldValid(inp);
        inp.classList.toggle("field-invalid", !ok);
        if (!ok) allOk = false;
      });
      if (pwInput && pwRulesEl) {
        const c = pwChecks(pwInput.value || "");
        pwRulesEl.querySelectorAll("[data-pw]").forEach((li) => {
          const ok = !!c[li.getAttribute("data-pw")];
          li.classList.toggle("ok", ok);
          const mark = li.querySelector(".pw-mark");
          if (mark) mark.textContent = ok ? "✓" : "○";
        });
      }
      if (submit) submit.disabled = !allOk;
    };
    inputs.forEach((inp) => { inp.addEventListener("input", update); inp.addEventListener("blur", update); });
    update();
  });

  // --- Préréglages Rapide / Précis ---
  const PRESETS = {
    fast: { mode: "hybrid", use_reprompt: false, n_reformulations: 1, use_rerank: true, top_k: 5, k_candidates: 10, threshold: 0.3 },
    precise: { mode: "hybrid", use_reprompt: true, n_reformulations: 3, use_rerank: true, top_k: 8, k_candidates: 20, threshold: 0.3 },
  };
  document.querySelectorAll("[data-preset]").forEach((b) => {
    b.addEventListener("click", () => {
      const p = PRESETS[b.getAttribute("data-preset")];
      const panel = document.querySelector("[data-panel]");
      if (!p || !panel) return;
      Object.entries(p).forEach(([k, v]) => {
        const c = panel.querySelector('[data-p="' + k + '"]');
        if (!c) return;
        if (c.type === "checkbox") c.checked = !!v; else c.value = v;
        const out = panel.querySelector('[data-out="' + k + '"]');
        if (out) out.textContent = v;
      });
    });
  });

  // --- Composer ---
  const composer = document.querySelector("[data-composer]");
  if (chat && composer) {
    const input = composer.querySelector("[data-input]");
    input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); composer.requestSubmit(); } });
    composer.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (!q || !chat.dataset.baseId) return;
      input.value = ""; input.style.height = "auto";
      streamChat(chat, q);
    });
  }
});
