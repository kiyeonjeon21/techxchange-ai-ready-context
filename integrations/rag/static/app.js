"use strict";
const $ = (s, r = document) => r.querySelector(s);
const thread = $("#thread"), welcome = $("#welcome"), form = $("#composer"), input = $("#q"), sendBtn = $("#send");
const welcomeHTML = welcome ? welcome.outerHTML : "";   // captured for "새 대화" restore
// stable conversation id (per browser/tab) -> Langflow multi-turn memory; survives refresh
let sessionId = sessionStorage.getItem("ragui_sid") || crypto.randomUUID();
sessionStorage.setItem("ragui_sid", sessionId);

marked.setOptions({ breaks: true });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// CJK markdown fix: CommonMark won't close **bold**/*italic* when the closing delimiter follows
// punctuation and precedes a word char (right-flanking rule) — so Korean like "**…정보)**란" renders
// literally. Insert an invisible zero-width space before such a delimiter to make it a valid closer.
// Code spans/blocks are left untouched.
function fixCJKEmphasis(md) {
  return String(md ?? "").split(/(```[\s\S]*?```|`[^`\n]*`)/g)
    .map((seg, i) => i % 2 ? seg
      : seg.replace(/([^\w\s가-힣*])(\*{1,2})(?=[\w가-힣])/g, "$1​$2"))
    .join("");
}

const SUGGESTIONS = [
  "가명정보란 무엇이고 동의 없이 쓸 수 있는 목적은?",
  "전자금융거래법의 접근매체와 감독기관의 관계는?",
  "위험등급이 high인 고객의 플래그된 거래 총액과 상대국가는?",
  "고객 세그먼트별 의심거래보고(STR) 건수와 신고금액 합계는?",
];

function renderSuggestions() {
  const wrap = $("#suggest");
  wrap.innerHTML = "";
  SUGGESTIONS.forEach(s => {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = s;
    b.onclick = () => { input.value = s; submit(); };
    wrap.appendChild(b);
  });
}
renderSuggestions();

function scrollDown() { window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }); }

function addUser(text) {
  $("#welcome")?.remove();
  const el = document.createElement("div");
  el.className = "msg user";
  el.innerHTML = `<div class="bubble">${esc(text)}</div>`;
  thread.appendChild(el);
  scrollDown();
}

function addLoading() {
  const el = document.createElement("div");
  el.className = "msg bot";
  el.innerHTML = `<div class="answer-card"><div class="loading">
      <span class="orbit"></span>
      <span class="ph">routing · retrieving · synthesizing…</span>
    </div></div>`;
  thread.appendChild(el);
  scrollDown();
  return el;
}

function routeBadges(route) {
  const defs = [["vector", "vector search"], ["graph", "knowledge graph"], ["sql", "sql / iceberg"]];
  const items = defs.map(([k, label]) =>
    `<span class="badge ${k} ${route[k] ? "on" : ""}"><span class="led"></span>${label}</span>`).join("");
  return `<div class="trace"><span class="lab">tools</span>${items}</div>`;
}

function citeChips(cites) {
  if (!cites?.length) return "";
  const seen = new Set();
  const chips = cites.filter(c => { const k = `${c.title}#${c.chunk_no}`; if (seen.has(k)) return false; seen.add(k); return true; })
    .map(c => `<span class="cite">${esc(c.title)}<span style="opacity:.55">#${c.chunk_no}</span></span>`).join("");
  return `<div class="cites">${chips}</div>`;
}

function srcCell(c) {
  const s = c.source || "";
  if (/^https?:\/\//.test(s)) return `<a class="src" href="${esc(s)}" target="_blank" rel="noopener">source ↗</a>`;
  if (s.startsWith("/corpus/")) return `<a class="src" href="${esc(s)}" target="_blank" rel="noopener">문서 ↗</a>`;
  if (s.startsWith("file:")) return `<span class="src src-none">업로드 파일</span>`;
  if (s.startsWith("inline:")) return `<span class="src src-none">붙여넣은 텍스트</span>`;
  return `<span class="src src-none">${esc(c.title)}</span>`;
}

function chunkPanel(chunks) {
  if (!chunks?.length) return "";
  const rows = chunks.map(c => {
    const pct = Math.max(4, Math.min(100, Math.round((c.score || 0) * 100)));
    const src = srcCell(c);
    return `<div class="chunk">
      <div class="meta"><span>${esc(c.title)} <span style="opacity:.5">#${c.chunk_no}</span></span>${src}
        <span class="sc">${(c.score ?? 0).toFixed(3)}</span><span class="scbar"><i style="width:${pct}%"></i></span></div>
      <div class="txt">${esc(c.text)}</div></div>`;
  }).join("");
  return panel("p-vec", `Retrieved passages`, chunks.length, rows);
}

function kgPanel(kg) {
  const ents = kg?.entities || [], edges = kg?.edges || [];
  if (!ents.length && !edges.length) return "";
  const entHtml = ents.length ? `<div class="ents">${ents.map(e => {
    const m = String(e).match(/^(.*?)\s*\((.*)\)\s*$/);
    return m ? `<span class="ent">${esc(m[1])}<em>${esc(m[2])}</em></span>` : `<span class="ent">${esc(e)}</span>`;
  }).join("")}</div>` : "";
  const edgeHtml = edges.length ? `<div class="edges">${edges.map(t => {
    const m = String(t).match(/^(.*?)\s*-\[(.*?)\]->\s*(.*)$/);
    return m ? `<div class="edge"><span class="node">${esc(m[1])}</span><span class="rel">${esc(m[2])}<span>▸</span></span><span class="node">${esc(m[3])}</span></div>`
             : `<div class="edge"><span class="node">${esc(t)}</span></div>`;
  }).join("")}</div>` : "";
  return panel("p-kg", `Knowledge subgraph`, ents.length + edges.length, `<div class="kg">${entHtml}${edgeHtml}</div>`);
}

function sqlPanel(sql) {
  // accepts the new shape {query, columns, rows}; tolerates the old bare-rows array
  const obj = Array.isArray(sql) ? { query: null, columns: [], rows: sql } : (sql || {});
  const rows = obj.rows || [], cols = obj.columns || [];
  if (!rows.length && !obj.query) return "";
  const code = obj.query ? `<pre class="sqlcode"><code>${esc(obj.query)}</code></pre>` : "";
  const head = cols.length ? `<thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead>` : "";
  const body = rows.length
    ? `<table class="sqlt">${head}<tbody>${rows.map(r =>
        `<tr>${r.map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`
    : `<div class="dl-empty">결과 없음</div>`;
  return panel("p-sql", `Generated SQL · results`, rows.length, code + body);
}

function panel(cls, title, n, body) {
  return `<details class="panel ${cls}"><summary><span class="tag"></span>${esc(title)}
      <span class="n">${n}</span><span class="chev">▸</span></summary>
    <div class="panel-body">${body}</div></details>`;
}

function renderAnswer(el, data) {
  const ctx = data.context || {};
  const panels = chunkPanel(ctx.chunks) + kgPanel(ctx.kg) + sqlPanel(ctx.sql);
  el.innerHTML = `<div class="answer-card">
    <div class="ans">${marked.parse(fixCJKEmphasis(data.answer || "_No answer._"))}</div>
    ${citeChips(data.citations)}
    ${routeBadges(data.route || {})}
    ${panels ? `<div class="ctx">${panels}</div>` : ""}
  </div>`;
  scrollDown();
}

async function submit() {
  const q = input.value.trim();
  if (!q) return;
  input.value = ""; sendBtn.disabled = true;
  addUser(q);
  const slot = addLoading();
  try {
    const res = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, session_id: sessionId }),
    });
    const data = await res.json();
    renderAnswer(slot, data);
  } catch (e) {
    slot.innerHTML = `<div class="answer-card"><div class="ans"><b>Error:</b> ${esc(e.message)}</div></div>`;
  } finally {
    sendBtn.disabled = false; input.focus();
  }
}

form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
input.focus();

// "새 대화" — fresh conversation id + reset the thread to the welcome screen
function newChat() {
  sessionId = crypto.randomUUID();
  sessionStorage.setItem("ragui_sid", sessionId);
  thread.innerHTML = welcomeHTML;
  renderSuggestions();
  input.value = ""; input.focus();
}
$("#newChatBtn").onclick = newChat;

/* ---------------- Corpus management drawer (no-code ingest / delete) ---------------- */
const drawer = $("#corpus"), scrim = $("#scrim");
const addTitle = $("#addTitle"), addText = $("#addText"), addUrl = $("#addUrl"), addFile = $("#addFile");
const addBtn = $("#addBtn"), addStatus = $("#addStatus"), docList = $("#docList"), docCount = $("#docCount");
let addMode = "text";

function openDrawer() { drawer.hidden = false; scrim.hidden = false; loadDocs(); }
function closeDrawer() { drawer.hidden = true; scrim.hidden = true; }
$("#corpusBtn").onclick = openDrawer;
$("#corpusClose").onclick = closeDrawer;
scrim.onclick = closeDrawer;

$("#addTabs").addEventListener("click", (e) => {
  const b = e.target.closest(".tab"); if (!b) return;
  addMode = b.dataset.mode;
  [...document.querySelectorAll("#addTabs .tab")].forEach(t => t.classList.toggle("on", t === b));
  addText.hidden = addMode !== "text";
  addUrl.hidden = addMode !== "url";
  addFile.hidden = addMode !== "file";
});

let toastT;
function toast(msg, ok = true) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast " + (ok ? "ok" : "err"); t.hidden = false;
  clearTimeout(toastT); toastT = setTimeout(() => { t.hidden = true; }, 3800);
}

async function loadDocs() {
  docList.innerHTML = `<div class="dl-empty">불러오는 중…</div>`;
  try {
    const data = await (await fetch("/api/docs")).json();
    const docs = data.docs || [];
    docCount.textContent = docs.length;
    docList.innerHTML = docs.length ? docs.map(d => `
      <div class="doc-item">
        <div class="doc">
          <div class="doc-main" data-id="${esc(d.doc_id)}" role="button" tabindex="0" title="클릭하여 내용 보기">
            <div class="doc-title">${esc(d.title)}<span class="doc-chev">▸</span></div>
            <div class="doc-meta">${esc(d.source)}</div>
            <div class="doc-stats">chunks ${d.chunks ?? "–"} · ent ${d.entities ?? "–"} · edge ${d.edges ?? "–"}</div>
          </div>
          <button class="doc-del" data-id="${esc(d.doc_id)}" title="삭제">🗑</button>
        </div>
        <div class="doc-preview" id="prev-${esc(d.doc_id)}" hidden></div>
      </div>`).join("") : `<div class="dl-empty">아직 적재된 문서가 없습니다.</div>`;
  } catch (e) {
    docList.innerHTML = `<div class="dl-empty">목록 오류: ${esc(e.message)}</div>`;
  }
}

docList.addEventListener("click", async (e) => {
  const b = e.target.closest(".doc-del"); if (!b) return;
  if (!confirm("이 문서를 모든 저장소에서 삭제할까요?")) return;
  b.disabled = true; b.textContent = "…";
  try {
    const r = await (await fetch("/api/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doc_id: b.dataset.id }) })).json();
    if (r.ok) { toast("삭제됨"); loadDocs(); } else { toast("삭제 실패: " + r.error, false); b.disabled = false; b.textContent = "🗑"; }
  } catch (err) { toast("삭제 오류: " + err.message, false); b.disabled = false; b.textContent = "🗑"; }
});

function docPreviewHTML(d) {
  const src = (d.source ? `<div class="dp-src">${srcCell({ source: d.source, title: d.title })}</div>` : "");
  const pdf = d.pdf_url ? `<div class="dp-pdf">
      <a class="dp-pdflink" href="${esc(d.pdf_url)}" target="_blank" rel="noopener">📄 원문 PDF 새 탭으로 열기 ↗</a>
      <embed class="dp-embed" src="${esc(d.pdf_url)}#toolbar=0&navpanes=0" type="application/pdf" />
    </div>` : "";
  const ents = (d.entities || []).length
    ? `<div class="dp-ents">${d.entities.slice(0, 30).map(e => `<span class="dp-ent">${esc(e)}</span>`).join("")}</div>` : "";
  const chunks = (d.chunks || []).map(c =>
    `<div class="dp-chunk"><span class="dp-no">#${c.chunk_no}</span><div class="dp-txt">${esc(c.text)}</div></div>`).join("");
  return src + pdf + ents + `<div class="dp-chunks">${chunks || '<div class="dl-empty">내용 없음</div>'}</div>`;
}

// click a doc row -> toggle an inline preview (chunks + KG entities), lazy-loaded once
docList.addEventListener("click", async (e) => {
  const main = e.target.closest(".doc-main"); if (!main) return;
  const id = main.dataset.id;
  const prev = document.getElementById("prev-" + id);
  if (!prev) return;
  const open = prev.hidden;
  main.classList.toggle("open", open);
  prev.hidden = !open;
  if (open && !prev.dataset.loaded) {
    prev.innerHTML = `<div class="dl-empty">불러오는 중…</div>`;
    try {
      const d = await (await fetch("/api/doc/" + encodeURIComponent(id))).json();
      if (d.ok) { prev.innerHTML = docPreviewHTML(d); prev.dataset.loaded = "1"; }
      else prev.innerHTML = `<div class="dl-empty">미리보기 오류: ${esc(d.error)}</div>`;
    } catch (err) { prev.innerHTML = `<div class="dl-empty">미리보기 오류: ${esc(err.message)}</div>`; }
  }
});

addBtn.addEventListener("click", async () => {
  addBtn.disabled = true; const orig = addBtn.textContent; addBtn.textContent = "적재 중…";
  addStatus.textContent = "임베딩 + KG 추출 중 (몇 초 걸립니다)…";
  try {
    let r;
    if (addMode === "file") {
      if (!addFile.files[0]) throw new Error("파일을 선택하세요");
      const fd = new FormData(); fd.append("file", addFile.files[0]);
      if (addTitle.value.trim()) fd.append("title", addTitle.value.trim());
      r = await (await fetch("/api/ingest/file", { method: "POST", body: fd })).json();
    } else {
      const content = addMode === "url" ? addUrl.value.trim() : addText.value;
      if (!content.trim()) throw new Error(addMode === "url" ? "URL을 입력하세요" : "본문을 입력하세요");
      r = await (await fetch("/api/ingest", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: addMode, title: addTitle.value.trim(), content }) })).json();
    }
    if (r.ok) {
      addStatus.textContent = "";
      toast(`적재 완료: "${r.title}" (chunks ${r.chunks} · ent ${r.entities} · edge ${r.edges})`);
      addTitle.value = ""; addText.value = ""; addUrl.value = ""; addFile.value = "";
      loadDocs();
    } else { addStatus.textContent = ""; toast("적재 실패: " + r.error, false); }
  } catch (err) { addStatus.textContent = ""; toast("적재 오류: " + err.message, false); }
  finally { addBtn.disabled = false; addBtn.textContent = orig; }
});
