// EDGAR v2 — interface de chat (vanilla, aucun CDN).
// Streaming SSE via fetch + ReadableStream, rendu Markdown assaini (marked +
// DOMPurify) et coloration de code (highlight.js). Dégradation propre si une
// lib vendue est absente (repli en texte brut).
"use strict";

function renderMarkdown(el, text) {
  if (window.marked && window.DOMPurify) {
    const html = window.marked.parse(text, { breaks: true });
    el.innerHTML = window.DOMPurify.sanitize(html);
    if (window.hljs) {
      el.querySelectorAll("pre code").forEach((b) => window.hljs.highlightElement(b));
    }
  } else {
    el.textContent = text; // repli sans lib Markdown
  }
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// Construit le bloc Sources (miroir du gabarit serveur).
function buildSources(sources) {
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
    d.appendChild(el("div", "source-cite", s.text || ""));
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
    body.appendChild(el("div", null,
      "Candidats fusionnés : " + diag.counts.candidats_fusionnes +
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

async function streamChat(chat, question, searchMode) {
  const messages = chat.querySelector("[data-messages]");
  const empty = messages.querySelector(".empty-state");
  if (empty) empty.remove();

  // Bulle utilisateur
  const um = el("div", "msg msg-user");
  const ub = el("div", "bubble", question);
  um.appendChild(ub);
  messages.appendChild(um);

  // Bulle assistant (en cours)
  const am = el("div", "msg msg-assistant");
  const abubble = el("div", "bubble");
  const md = el("div", "md-content");
  const cursor = el("span", "typing", "▍");
  abubble.appendChild(md);
  abubble.appendChild(cursor);
  am.appendChild(abubble);
  messages.appendChild(am);
  messages.scrollTop = messages.scrollHeight;

  let meta = null, buffer = "";
  const body = {
    base_id: chat.dataset.baseId,
    conversation_id: chat.dataset.convId || null,
    question: question,
    csrf_token: chat.dataset.csrf,
    params: { search_mode: searchMode },
  };

  try {
    const resp = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
        const raw = acc.slice(0, idx);
        acc = acc.slice(idx + 2);
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
  if (meta && meta.search_mode) {
    md.innerHTML = "<em>Mode recherche : extraits pertinents ci-dessous.</em>";
  } else {
    renderMarkdown(md, buffer);
  }
  if (meta) {
    const src = buildSources(meta.sources);
    if (src) am.appendChild(src);
    const dg = buildDiagnostics(meta.diagnostics);
    if (dg) am.appendChild(dg);
  }
  messages.scrollTop = messages.scrollHeight;
}

document.addEventListener("DOMContentLoaded", () => {
  // Rendu Markdown de l'historique.
  document.querySelectorAll(".md-content[data-md]").forEach((e) => {
    renderMarkdown(e, e.textContent.trim());
  });

  // Sélecteur de base.
  const sel = document.querySelector("[data-base-select]");
  if (sel) sel.addEventListener("change", () => {
    window.location.href = "/chat?base=" + encodeURIComponent(sel.value);
  });

  // Repli de la barre latérale (petit écran).
  const toggle = document.querySelector("[data-sidebar-toggle]");
  const sidebar = document.querySelector("[data-sidebar]");
  if (toggle && sidebar) toggle.addEventListener("click", () => sidebar.classList.toggle("open"));

  // Composer : envoi.
  const chat = document.querySelector(".chat");
  const composer = document.querySelector("[data-composer]");
  if (chat && composer) {
    const input = composer.querySelector("[data-input]");
    const searchMode = composer.querySelector("[data-search-mode]");
    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 180) + "px";
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); composer.requestSubmit(); }
    });
    composer.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (!q || !chat.dataset.baseId) return;
      input.value = "";
      input.style.height = "auto";
      streamChat(chat, q, !!searchMode.checked);
    });
  }
});
