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
    sum.appendChild(el("span", "src-file", s.file || ""));
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
    const url = "/doc/" + encodeURIComponent(baseId) + "/" + encodeURIComponent(file);
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
  const cursor = el("span", "typing", "▍");
  abubble.appendChild(md); abubble.appendChild(cursor);
  am.appendChild(abubble);
  messages.appendChild(am);
  messages.scrollTop = messages.scrollHeight;

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
        if (event === "meta") {
          meta = payload;
          if (payload.conversation_id) chat.dataset.convId = payload.conversation_id;
        } else if (event === "token") {
          buffer += payload.t || "";
          md.textContent = buffer;
          messages.scrollTop = messages.scrollHeight;
        } else if (event === "error") {
          buffer += "\n\n_" + (payload.message || "Erreur.") + "_";
          md.textContent = buffer;
        } else if (event === "done") {
          if (payload.conversation_id) chat.dataset.convId = payload.conversation_id;
        }
      }
    }
  } catch (e) {
    md.textContent = "Erreur de communication avec le serveur.";
  }

  cursor.remove();
  if (meta && meta.search_mode) md.innerHTML = "<em>Mode recherche : extraits pertinents ci-dessous.</em>";
  else renderMarkdown(md, buffer);
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
  if (sel) sel.addEventListener("change", () => { window.location.href = "/chat?base=" + encodeURIComponent(sel.value); });

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

  // --- Panneau d'expérimentation ---
  const panel = document.querySelector("[data-panel]");
  const chat = document.querySelector(".chat");
  if (panel) {
    document.querySelector("[data-panel-toggle]").addEventListener("click", () => { panel.hidden = !panel.hidden; });
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
