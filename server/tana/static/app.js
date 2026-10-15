"use strict";

const $ = (s) => document.querySelector(s);
const state = {
  threads: [], current: null, filter: "all", claude: false, claudeCode: false, busy: false, runKey: null,
  speak: true, handsfree: false, recorder: null, audioQueue: [], playing: false,
  rm: { enabled: false, actions: false, chats: [], agents: [], needs: 0, boardNames: {} },
  harnesses: {},   // other agent harnesses with sessions here: {codex: {label, sessions}, ...}
};

try {
  state.speak = localStorage.getItem("tana.speak") !== "0";
  state.handsfree = localStorage.getItem("tana.handsfree") === "1";
} catch (_) { /* storage unavailable */ }

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "X-Tana": "1", ...(opts.json ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) },
    body: opts.json ? JSON.stringify(opts.json) : opts.body,
    credentials: "same-origin",
  });
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("login required"); }
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return opts.raw ? res : res.json();
}

function toast(text, ms = 3500) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), ms);
}

// ------------------------------------------------------------------ safe markdown
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
}
function renderMarkdown(src) {
  const parts = escapeHtml(src).split(/```(?:[\w-]*)\n?([\s\S]*?)(?:```|$)/);
  let html = "";
  parts.forEach((part, i) => {
    if (i % 2 === 1) { html += `<pre><code>${part}</code></pre>`; return; }
    const blocks = part.split(/\n{2,}/);
    for (const block of blocks) {
      const lines = block.split("\n").filter((l) => l.trim() !== "");
      if (!lines.length) continue;
      if (lines.every((l) => /^\s*[-*•]\s+/.test(l))) {
        html += "<ul>" + lines.map((l) => `<li>${inline(l.replace(/^\s*[-*•]\s+/, ""))}</li>`).join("") + "</ul>";
      } else if (lines.every((l) => /^\s*\d+[.)]\s+/.test(l))) {
        html += "<ol>" + lines.map((l) => `<li>${inline(l.replace(/^\s*\d+[.)]\s+/, ""))}</li>`).join("") + "</ol>";
      } else {
        html += `<p>${lines.map((l) => inline(l.replace(/^#+\s*/, ""))).join("<br>")}</p>`;
      }
    }
  });
  return html;
}

// ------------------------------------------------------------------ screens
function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-password").focus();
}

async function boot() {
  let s;
  try { s = await api("/api/state"); } catch (e) { toast("Server unreachable"); return; }
  if (!s.configured) {
    showLogin();
    $("#login-error").textContent = "Not set up yet. Finish setup from your home network first.";
    return;
  }
  if (!s.logged_in) return showLogin();
  state.claude = s.claude;
  state.claudeCode = s.claude_code;
  state.boards = s.boards;
  // only what is set up is shown
  $("#new-tana").hidden = !s.tana;
  $("#new-claude").hidden = !s.claude;
  $("#new-cc").hidden = !s.claude_code;
  $("#open-devices").hidden = !s.boards;
  document.querySelector('#brain-switch [data-brain="claude"]').hidden = !s.claude;
  state.rm.enabled = !!s.rameness;
  state.rm.actions = !!s.rameness_actions;
  document.querySelectorAll('.filters [data-filter^="rameness"]').forEach((b) => (b.hidden = !s.rameness));
  $("#login").hidden = true;
  $("#app").hidden = false;
  applyToggles();
  if (location.pathname === "/mic") { openMicMode(); return; }
  connectDisplay();
  await Promise.all([loadThreads(), loadTeams(), loadHarnesses()]);
  // a tab shows only when it has something: Claude Code sessions, boards' conversations
  const has = (f) => state.threads.some((x) => (x.automated ? "sessions" : x.source) === f);
  for (const f of ["claude-code", "sessions", "device"]) document.querySelector(`.filters [data-filter="${f}"]`).hidden = !has(f);
  const first = state.threads.find((t) => t.source === "web");
  if (first) openThread(first.id); else renderEmpty();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", json: { password: $("#login-password").value, code: $("#login-code").value } });
    $("#login-password").value = "";
    $("#login-code").value = "";
    boot();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

// ------------------------------------------------------------------ threads
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

async function loadThreads() {
  state.threads = await api("/api/threads");
  renderThreads();
}

function listItem(title, badges, updated, active, onClick, extraClass = "") {
  const li = document.createElement("li");
  li.className = [active ? "active" : "", extraClass].filter(Boolean).join(" ");
  const t = document.createElement("div");
  t.className = "t-title";
  t.textContent = title;
  const meta = document.createElement("div");
  meta.className = "t-meta";
  for (const [cls, text] of badges) {
    const b = document.createElement("span");
    b.className = `badge ${cls}`;
    b.textContent = text;
    meta.append(b);
  }
  const when = document.createElement("span");
  when.textContent = ago(updated);
  meta.append(when);
  li.append(t, meta);
  li.addEventListener("click", () => { onClick(); closeSidebar(); });
  return li;
}

function threadItem(t) {
  let badge;
  if (t.source === "device") badge = ["", `Board ${t.device}`];
  else if (t.source === "claude-code") badge = ["claude", t.project ? `Claude Code · ${t.project}` : "Claude Code"];
  else if (t.brain === "claude") badge = ["claude", "Claude"];
  else badge = ["tana", "Tana"];
  return listItem(t.title || "New conversation", [badge], t.updated,
    state.current && state.current.id === t.id && state.current.source !== "rameness", () => openThread(t.id));
}

const isOpen = (kind, id) => state.current && state.current.source === "rameness" &&
  state.current.kind === kind && state.current.id === id;

function chatLabel(c) {
  if (c.id.startsWith("board-")) {
    const name = state.rm.boardNames[c.id];
    return name ? `${name} board` : `Board ${c.id.slice(-5).replace(/-/g, ":")}`;
  }
  return c.title || c.id;
}

function chatItem(c) {
  const badges = [["rm", c.id.startsWith("board-") ? "Board chat" : "Rameness chat"]];
  if (c.mode === "direct") badges.push(["", "direct"]);
  return listItem(chatLabel(c), badges, c.updated, isOpen("chat", c.id), () => openRameness("chat", c.id));
}

function agentItem(a) {
  const badges = [["rm", a.role], [`st-${a.status}`, a.status]];
  return listItem(a.title, badges, a.updated, isOpen("agent", a.id), () => openRameness("agent", a.id),
    a.role === "manager" ? "" : "rm-child");
}

function renderThreads() {
  const ul = $("#threads");
  ul.textContent = "";
  if (state.filter.startsWith("h:")) {
    const h = state.filter.slice(2);
    for (const x of (state.harnesses[h] || { sessions: [] }).sessions) {
      const badge = [["", x.project ? `${state.harnesses[h].label} · ${x.project}` : state.harnesses[h].label]];
      ul.append(listItem(x.title, badge, x.updated, state.current && state.current.source === "harness"
        && state.current.id === x.id, () => openHarness(h, x.id, x.title)));
    }
    return;
  }
  if (state.filter === "rameness") {
    for (const c of state.rm.chats) ul.append(chatItem(c));
  } else if (state.filter === "rameness-agents") {
    for (const a of state.rm.agents) ul.append(agentItem(a));
  } else {
    const entries = state.threads
      .filter((t) => (state.filter === "all" ? !t.automated : (t.automated ? "sessions" : t.source) === state.filter))
      .map((t) => ({ updated: t.updated, li: threadItem(t) }));
    if (state.filter === "all") for (const c of state.rm.chats) entries.push({ updated: c.updated, li: chatItem(c) });
    entries.sort((a, b) => b.updated - a.updated);
    for (const e of entries) ul.append(e.li);
  }
  if (!ul.children.length && state.filter.startsWith("rameness")) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = state.filter === "rameness" ? "No other Rameness chats yet (boards, the command line)." : "No agents yet.";
    ul.append(li);
  }
}

document.querySelectorAll(".filters .chip").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".filters .chip").forEach((x) => x.classList.toggle("active", x === b));
    state.filter = b.dataset.filter;
    renderThreads();
  }));

async function newThread(brain) {
  if (brain === "claude" && !state.claude) return toast("Claude isn't set up on the server yet.");
  const t = await api("/api/threads", { method: "POST", json: { brain } });
  state.threads.unshift(t);
  await openThread(t.id);
  closeSidebar();
  $("#input").focus();
}
$("#new-tana").addEventListener("click", () => newThread("tana"));
$("#new-claude").addEventListener("click", () => newThread("claude"));
$("#new-cc").addEventListener("click", () => {
  // The session is created by the first message; until then this is a local draft thread.
  state.current = { id: null, source: "claude-code", brain: "claude-code", title: "", messages: [] };
  $("#thread-title").textContent = "New Claude Code session";
  renderEmpty(state.current);
  showComposerFor(state.current);
  renderThreads();
  closeSidebar();
  $("#input").focus();
});

function showComposerFor(t) {
  $("#rm-retire").hidden = true;
  input.placeholder = "Message…";
  if (t.source === "rameness") return showRamenessComposer(t);
  if (t.source === "harness") {
    $("#composer").hidden = true;
    $(".handsfree").hidden = true;
    $("#brain-switch").hidden = true;
    const note = $("#readonly-note");
    note.classList.remove("cc-warning");
    note.textContent = `A ${t.label} session on this machine (read-only).`;
    note.hidden = false;
    return;
  }
  const interactive = t.source === "web" || (t.source === "claude-code" && state.claudeCode);
  $("#composer").hidden = !interactive;
  $(".handsfree").hidden = !interactive;
  $("#brain-switch").hidden = t.source !== "web";
  const note = $("#readonly-note");
  note.classList.toggle("cc-warning", t.source === "claude-code" && interactive);
  if (t.source === "claude-code") {
    note.textContent = interactive
      ? (t.id && !t.web_owned
        ? "Claude Code with full access to the server. Your first message continues this session as a copy (the original may still be open in a terminal)."
        : "Claude Code with full access to the server.")
      : "Claude Code session (read-only here: running Claude Code isn't allowed from this network).";
    note.hidden = false;
  } else {
    note.textContent = "Conversation from a Tana board (read-only).";
    note.hidden = interactive;
  }
}

async function openThread(id) {
  const t = await api(`/api/threads/${id}`);
  state.current = t;
  $("#thread-title").textContent = t.title || "New conversation";
  const box = $("#messages");
  box.textContent = "";
  if (!t.messages.length) renderEmpty(t);
  let steps = null;
  for (const m of t.messages) {
    if (m.role === "tool") {
      // fold consecutive tool calls into one expandable "N steps" line
      if (!steps) steps = addSteps();
      const li = document.createElement("li");
      li.textContent = m.content;
      steps.list.append(li);
      steps.summary.textContent = `${steps.list.children.length} step${steps.list.children.length > 1 ? "s" : ""}`;
      continue;
    }
    steps = null;
    addMessage(m.role, m.content, t.source === "claude-code" ? "claude-code" : (m.brain || t.brain));
  }
  showComposerFor(t);
  updateBrainSwitch();
  renderThreads();
  box.scrollTop = box.scrollHeight;
}

function renderEmpty(t) {
  const box = $("#messages");
  box.textContent = "";
  const d = document.createElement("div");
  d.className = "empty";
  const who = t && (t.source === "claude-code" ? "Claude Code" : t.brain === "claude" ? "Claude" : "Tana");
  d.textContent = t
    ? `Say something to ${who}: tap the microphone or type below.`
    : "Start a conversation with Tana or Claude from the list.";
  box.append(d);
  if (!t) { $("#composer").hidden = true; $(".handsfree").hidden = true; $("#brain-switch").hidden = true; }
}

function addMessage(role, text, brain, label = null) {
  const box = $("#messages");
  box.querySelector(".empty")?.remove();
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  if (role === "assistant") {
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = label || (brain === "claude-code" ? "Claude Code" : brain === "claude" ? "Claude"
      : brain === "rameness" ? "Rameness" : "Tana");
    div.append(who);
  }
  const body = document.createElement("div");
  body.className = "body";
  body.innerHTML = renderMarkdown(text);
  div.append(body);
  box.append(div);
  box.scrollTop = box.scrollHeight;
  return body;
}

function addSteps() {
  const details = document.createElement("details");
  details.className = "steps";
  const summary = document.createElement("summary");
  const list = document.createElement("ul");
  details.append(summary, list);
  $("#messages").append(details);
  return { summary, list };
}

// ------------------------------------------------------------------ brain switch & toggles
function updateBrainSwitch() {
  document.querySelectorAll("#brain-switch button").forEach((b) => {
    b.classList.toggle("on", state.current && b.dataset.brain === state.current.brain);
    if (b.dataset.brain === "claude") b.disabled = !state.claude;
  });
}
document.querySelectorAll("#brain-switch button").forEach((b) =>
  b.addEventListener("click", async () => {
    if (!state.current || state.current.source !== "web") return;
    const t = await api(`/api/threads/${state.current.id}`, { method: "PATCH", json: { brain: b.dataset.brain } });
    state.current.brain = t.brain;
    const listed = state.threads.find((x) => x.id === t.id);
    if (listed) listed.brain = t.brain;
    updateBrainSwitch();
    renderThreads();
    toast(`${t.brain === "claude" ? "Claude" : "Tana"} will answer from here on.`);
  }));

function applyToggles() {
  $("#toggle-speak").setAttribute("aria-pressed", String(state.speak));
  $("#handsfree").checked = state.handsfree;
}
$("#toggle-speak").addEventListener("click", () => {
  state.speak = !state.speak;
  try { localStorage.setItem("tana.speak", state.speak ? "1" : "0"); } catch (_) {}
  if (!state.speak) stopAudio();
  applyToggles();
});
$("#handsfree").addEventListener("change", (e) => {
  state.handsfree = e.target.checked;
  try { localStorage.setItem("tana.handsfree", state.handsfree ? "1" : "0"); } catch (_) {}
});

// ------------------------------------------------------------------ sending
const input = $("#input");
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
});
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (text) { input.value = ""; input.style.height = "auto"; send(text); }
});

async function send(text) {
  if (!state.current || state.busy) return;
  if (state.current.source === "claude-code") return sendClaudeCode(text);
  if (state.current.source === "rameness") return sendRameness(text);
  unlockAudio();
  stopAudio();
  state.busy = true;
  $("#send").disabled = true;
  const thread = state.current;
  addMessage("user", text);
  if (!thread.title) { thread.title = text.slice(0, 80); $("#thread-title").textContent = thread.title; }
  const body = addMessage("assistant", "", thread.brain);
  body.classList.add("typing");
  let full = "";
  try {
    const res = await api(`/api/threads/${thread.id}/messages`,
      { method: "POST", json: { text, speak: state.speak }, raw: true });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        if (ev.t === "delta") {
          full += ev.text;
          body.innerHTML = renderMarkdown(full);
          $("#messages").scrollTop = $("#messages").scrollHeight;
        } else if (ev.t === "audio" && state.speak && state.current === thread) {
          queueAudio(ev.url);
        }
      }
    }
  } catch (err) {
    full += `\n\n(${err.message})`;
    body.innerHTML = renderMarkdown(full);
  } finally {
    body.classList.remove("typing");
    state.busy = false;
    $("#send").disabled = false;
    loadThreads();
    if (state.handsfree && !(state.speak && (state.playing || state.audioQueue.length))) startListening();
  }
}

async function readEvents(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      if (chunk.startsWith("data: ")) onEvent(JSON.parse(chunk.slice(6)));
    }
  }
}

async function sendClaudeCode(text) {
  const thread = state.current;
  unlockAudio();
  stopAudio();
  state.busy = true;
  $("#send").hidden = true;
  $("#stop").hidden = false;
  addMessage("user", text);
  let body = null, steps = null, full = "", newSession = null;
  const ensureBody = () => {
    if (!body) { body = addMessage("assistant", "", "claude-code"); body.classList.add("typing"); full = ""; steps = null; }
    return body;
  };
  try {
    const res = await api("/api/claude-code/run",
      { method: "POST", json: { thread: thread.id, text, speak: state.speak }, raw: true });
    await readEvents(res, (ev) => {
      if (ev.t === "run") state.runKey = ev.key;
      else if (ev.t === "session") newSession = "cc-" + ev.id;
      else if (ev.t === "delta") {
        if (!full.trim() && !ev.text.trim()) return;
        const b = ensureBody();
        full += ev.text;
        b.innerHTML = renderMarkdown(full);
        $("#messages").scrollTop = $("#messages").scrollHeight;
      } else if (ev.t === "step") {
        if (body) { body.classList.remove("typing"); body = null; }
        if (!steps) steps = addSteps();
        const li = document.createElement("li");
        li.textContent = ev.text;
        steps.list.append(li);
        steps.summary.textContent = `${steps.list.children.length} step${steps.list.children.length > 1 ? "s" : ""}`;
        $("#messages").scrollTop = $("#messages").scrollHeight;
      } else if (ev.t === "audio" && state.speak) queueAudio(ev.url);
      else if (ev.t === "error") toast("Claude Code: " + ev.text, 6000);
    });
  } catch (err) {
    toast("Claude Code: " + err.message, 6000);
  } finally {
    body?.classList.remove("typing");
    state.busy = false;
    state.runKey = null;
    $("#send").hidden = false;
    $("#stop").hidden = true;
    await loadThreads();
    if (newSession) await openThread(newSession);
    if (state.handsfree && !(state.speak && (state.playing || state.audioQueue.length))) startListening();
  }
}

$("#stop").addEventListener("click", async () => {
  if (state.runKey) await api("/api/claude-code/stop", { method: "POST", json: { key: state.runKey } }).catch(() => {});
});

// ------------------------------------------------------------------ other harnesses' sessions (read-only)
async function loadHarnesses() {
  try { state.harnesses = await api("/api/harnesses"); } catch (_) { state.harnesses = {}; }
  const bar = document.querySelector(".filters");
  bar.querySelectorAll('[data-filter^="h:"]').forEach((b) => b.remove());
  for (const [name, h] of Object.entries(state.harnesses)) {      // a tab only for harnesses with sessions
    const b = document.createElement("button");
    b.className = "chip";
    b.dataset.filter = `h:${name}`;
    b.textContent = h.label;
    b.addEventListener("click", () => {
      bar.querySelectorAll(".chip").forEach((x) => x.classList.toggle("active", x === b));
      state.filter = b.dataset.filter;
      renderThreads();
    });
    bar.append(b);
  }
}

async function openHarness(h, id, title) {
  const v = await api(`/api/harnesses/${encodeURIComponent(h)}/${encodeURIComponent(id)}`);
  state.current = { source: "harness", id, harness: h, label: v.label, title };
  $("#thread-title").textContent = title;
  const box = $("#messages");
  box.textContent = "";
  for (const m of v.messages) addMessage(m.role, m.content, h, m.role === "assistant" ? v.label : null);
  if (!v.messages.length) renderEmpty(state.current);
  showComposerFor(state.current);
  renderThreads();
  box.scrollTop = box.scrollHeight;
}

// ------------------------------------------------------------------ Rameness (its chats and agents)
const enc = encodeURIComponent;

async function loadTeams() {
  if (!state.rm.enabled) return;
  try {
    const r = await api("/api/rameness");
    Object.assign(state.rm, { chats: r.chats, agents: r.agents, needs: r.needs, actions: r.actions });
    if (!Object.keys(state.rm.boardNames).length && r.chats.some((c) => c.id.startsWith("board-"))) {
      try {
        for (const b of await api("/api/devices")) if (b.name) state.rm.boardNames[b.chat] = b.name;
      } catch (_) { /* tana-boards not reachable: boards keep their ids */ }
    }
    renderThreads();
  } catch (_) { /* shown on the next action */ }
}

async function openRameness(kind, id, quiet = false) {
  const v = await api(kind === "agent" ? `/api/rameness/agents/${enc(id)}` : `/api/rameness/chats/${enc(id)}`);
  const sig = JSON.stringify([v.status, v.messages.length, v.messages.at(-1), v.needs, (v.log || "").slice(-400)]);
  if (quiet && (!isOpen(kind, id) || state.current.sig === sig)) return;
  const box = $("#messages");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  state.current = { ...v, kind, id, source: "rameness", sig };
  const chat = state.rm.chats.find((c) => c.id === id);
  $("#thread-title").textContent = kind === "agent" ? v.title : (chat ? chatLabel(chat) : id);
  box.textContent = "";
  if (kind === "agent") {
    const info = document.createElement("div");
    info.className = "rm-info";
    for (const f of [v.status, v.role, v.runtime, [v.slot, v.env].filter(Boolean).join(" @ "),
      v.branch && `branch ${v.branch}`, v.parent_title && `under ${v.parent_title}`].filter(Boolean)) {
      const sp = document.createElement("span"); sp.textContent = f; info.append(sp);
    }
    box.append(info);
  }
  if (!v.messages.length && !v.needs.length) {
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = "Nothing here yet.";
    box.append(d);
  }
  let steps = null;
  const label = kind === "agent" ? v.title : "Rameness";
  for (const m of v.messages) {
    if (m.role === "tool") {
      if (!steps) steps = addSteps();
      const li = document.createElement("li");
      li.textContent = m.content;
      steps.list.append(li);
      steps.summary.textContent = `${steps.list.children.length} step${steps.list.children.length > 1 ? "s" : ""}`;
      continue;
    }
    steps = null;
    addMessage(m.role, m.content, "rameness", m.role === "assistant" ? label : null);
  }
  for (const n of v.needs) addNeed(n);
  if (v.log) {
    const d = document.createElement("details");
    d.className = "steps rm-log";
    const sum = document.createElement("summary");
    sum.textContent = "Session log";
    const pre = document.createElement("pre");
    pre.textContent = v.log;
    d.append(sum, pre);
    box.append(d);
  }
  showComposerFor(state.current);
  renderThreads();
  if (!quiet || atBottom) box.scrollTop = box.scrollHeight;
}

function addNeed(n) {
  const d = document.createElement("div");
  d.className = "need";
  const who = document.createElement("div");
  who.className = "who";
  who.textContent = `${n.who} needs your decision`;
  const q = document.createElement("div");
  q.className = "q";
  q.textContent = n.question;
  d.append(who, q);
  const opts = document.createElement("div");
  opts.className = "opts";
  for (const o of n.options) {
    const b = document.createElement("button");
    b.textContent = o;
    b.disabled = !state.rm.actions;
    b.addEventListener("click", () => answerNeed(n, o));
    opts.append(b);
  }
  if (n.options.length) d.append(opts);
  if (n.free_text) {
    const f = document.createElement("form");
    const i = document.createElement("input");
    i.placeholder = n.options.length ? "…or give instructions" : "Your answer";
    i.disabled = !state.rm.actions;
    const b = document.createElement("button");
    b.className = "primary";
    b.textContent = "Send";
    b.disabled = !state.rm.actions;
    f.append(i, b);
    f.addEventListener("submit", (e) => { e.preventDefault(); if (i.value.trim()) answerNeed(n, i.value.trim()); });
    d.append(f);
  }
  $("#messages").append(d);
}

async function answerNeed(n, answer) {
  const v = state.current;
  try {
    await api(`/api/rameness/needs/${enc(n.id)}`, { method: "POST", json: { answer } });
    toast(`Answered: ${answer}`);
  } catch (err) { toast("Rameness: " + err.message, 6000); }
  await openRameness(v.kind, v.id);
  loadTeams();
}

function showRamenessComposer(t) {
  const retired = t.kind === "agent" && t.status === "retired";
  const can = state.rm.actions && !retired;
  $("#composer").hidden = !can;
  $(".handsfree").hidden = true;
  $("#brain-switch").hidden = true;
  $("#rm-retire").hidden = !(state.rm.actions && t.kind === "agent" && t.role !== "manager" && !retired && !t.closed);
  input.placeholder = t.kind === "agent" ? "Message this agent…" : "Message Rameness…";
  const note = $("#readonly-note");
  note.classList.add("cc-warning");
  if (!state.rm.actions) note.textContent = "Read-only here: directing Rameness isn't allowed from this network.";
  else if (retired) note.textContent = "This agent was retired.";
  else if (t.kind === "agent") note.textContent = ["done", "failed"].includes(t.status)
    ? (t.closed ? "Closed. A message reopens this agent with your follow-up." : "A message reopens this agent with your follow-up.")
    : "Messages reach this agent between its turns.";
  else note.textContent = t.id.startsWith("board-") ? "This is a board's conversation: you can continue it here." : "";
  note.hidden = !note.textContent;
}

async function sendRameness(text) {
  const v = state.current;
  state.busy = true;
  $("#send").disabled = true;
  addMessage("user", text);
  const wait = addMessage("assistant", "…", "rameness", v.kind === "agent" ? v.title : "Rameness");
  wait.classList.add("typing");
  try {
    if (v.kind === "agent") {
      const r = await api(`/api/rameness/agents/${enc(v.id)}/prompt`, { method: "POST", json: { text } });
      toast(r.result ? `Agent: ${r.result}` : "Sent");
    } else {
      await api(`/api/rameness/chats/${enc(v.id)}/turn`, { method: "POST", json: { text } });
    }
  } catch (err) {
    toast("Rameness: " + err.message, 8000);
  } finally {
    state.busy = false;
    $("#send").disabled = false;
    await openRameness(v.kind, v.id).catch(() => {});
    loadTeams();
  }
}

$("#rm-retire").addEventListener("click", async () => {
  const v = state.current;
  if (!v || v.source !== "rameness" || v.kind !== "agent") return;
  if (!confirm(`Retire “${v.title}”? Its session closes and its worktree is removed (its branch is kept).`)) return;
  try {
    await api(`/api/rameness/agents/${enc(v.id)}/retire`, { method: "POST", json: {} });
    toast("Retired");
  } catch (err) { toast("Rameness: " + err.message, 6000); }
  await openRameness("agent", v.id);
  loadTeams();
});

// live view: the open Rameness chat or agent, and the list while a Rameness tab is showing
let rmTicks = 0;
setInterval(() => {
  if (document.hidden || !state.rm.enabled) return;
  rmTicks++;
  const c = state.current;
  const typing = document.activeElement && document.activeElement.closest && document.activeElement.closest(".need");
  if (c && c.source === "rameness" && !state.busy && !typing) openRameness(c.kind, c.id, true).catch(() => {});
  if (state.filter.startsWith("rameness") || rmTicks % 5 === 0) loadTeams();
}, 4000);

// ------------------------------------------------------------------ spoken replies
const player = $("#player");
let audioUnlocked = false;
function unlockAudio() {
  // Browsers only allow playback started from a tap; prime the element during one.
  if (audioUnlocked) return;
  audioUnlocked = true;
  player.src = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";
  player.play().catch(() => {});
}
function queueAudio(url) {
  state.audioQueue.push(url);
  if (!state.playing) playNext();
}
function playNext() {
  const url = state.audioQueue.shift();
  if (!url) {
    state.playing = false;
    if (state.handsfree && !state.busy && !state.recorder) startListening();
    return;
  }
  state.playing = true;
  player.src = url;
  player.play().catch(() => { state.playing = false; toast("Tap anywhere to allow sound."); });
}
player.addEventListener("ended", playNext);
player.addEventListener("error", playNext);
function stopAudio() {
  state.audioQueue = [];
  state.playing = false;
  player.pause();
}

// ------------------------------------------------------------------ voice input
function pickMime() {
  const options = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"];
  return options.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
}

// Record one utterance: stops by itself ~1.3 s after speech ends (or when stop() is called).
// Resolves with a Blob, or null if nothing was said. Uses MediaRecorder when the browser has it,
// otherwise captures raw audio and builds a WAV file (some in-car browsers lack MediaRecorder).
async function recordUtterance(button) {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error(window.isSecureContext
      ? "This browser has no microphone support (in a Tesla this needs software 2026.26+ on an AMD Ryzen car)."
      : "The microphone only works over https.");
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (err) {
    const why = {
      NotAllowedError: "Microphone permission was denied. Allow it for this site in the browser's site settings, then reload.",
      NotFoundError: "No microphone was found.",
      NotReadableError: "The microphone is busy (another app or call may be using it).",
      SecurityError: "The browser blocked microphone access for this page.",
    }[err.name];
    throw new Error(why || `Couldn't open the microphone (${err.name}: ${err.message}).`);
  }
  const rec = window.MediaRecorder ? mediaRecorderCapture(stream) : pcmCapture(stream);
  rec.heardSpeech = false;
  const done = rec.finished.then((blob) => {
    stream.getTracks().forEach((t) => t.stop());
    button.classList.remove("listening");
    return rec.heardSpeech ? blob : null;
  });
  button.classList.add("listening");
  watchSilence(stream, rec);
  return {
    done,
    stop() { if (rec.state !== "inactive") { rec.heardSpeech = true; rec.stop(); } },  // manual stop sends what was said
    cancel() { if (rec.state !== "inactive") { rec.heardSpeech = false; rec.stop(); } },
  };
}

function mediaRecorderCapture(stream) {
  const mime = pickMime();
  const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  const rec = {
    get state() { return mr.state; },
    stop() { if (mr.state !== "inactive") mr.stop(); },
  };
  rec.finished = new Promise((resolve) => {
    mr.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    mr.onstop = () => resolve(new Blob(chunks, { type: mr.mimeType || "audio/webm" }));
  });
  mr.start(250);
  return rec;
}

function pcmCapture(stream) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const ctx = new Ctx();
  const source = ctx.createMediaStreamSource(stream);
  const node = ctx.createScriptProcessor(4096, 1, 1);
  const parts = [];
  node.onaudioprocess = (e) => parts.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  source.connect(node);
  node.connect(ctx.destination);
  let resolve;
  const rec = { state: "recording", finished: new Promise((r) => (resolve = r)) };
  rec.stop = () => {
    if (rec.state === "inactive") return;
    rec.state = "inactive";
    node.disconnect(); source.disconnect();
    const rate = ctx.sampleRate;
    ctx.close();
    resolve(encodeWav(parts, rate));
  };
  return rec;
}

function encodeWav(parts, rate) {
  const n = parts.reduce((a, p) => a + p.length, 0);
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const str = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE"); str(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, n * 2, true);
  let o = 44;
  for (const p of parts) for (const x of p) { v.setInt16(o, Math.max(-1, Math.min(1, x)) * 0x7fff, true); o += 2; }
  return new Blob([buf], { type: "audio/wav" });
}

// Stop automatically after ~1.3 s of quiet once speech was heard (or 8 s of nothing).
function watchSilence(stream, rec) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  const ctx = new Ctx();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  ctx.createMediaStreamSource(stream).connect(analyser);
  const data = new Uint8Array(analyser.fftSize);
  const started = performance.now();
  let lastLoud = started;
  const tick = () => {
    if (rec.state === "inactive") { ctx.close(); return; }
    analyser.getByteTimeDomainData(data);
    let peak = 0;
    for (const v of data) peak = Math.max(peak, Math.abs(v - 128));
    const now = performance.now();
    if (peak > 14) { lastLoud = now; rec.heardSpeech = true; }
    const quietFor = now - lastLoud;
    if ((rec.heardSpeech && quietFor > 1300) || (!rec.heardSpeech && now - started > 8000) || now - started > 60000) {
      rec.stop();
      ctx.close();
      return;
    }
    requestAnimationFrame(tick);
  };
  tick();
}

// The in-app microphone button (works where the browser allows the microphone; not in a Tesla)
const mic = $("#mic");
mic.addEventListener("click", () => {
  unlockAudio();
  if (state.recorder) state.recorder.stop(); else startListening();
});

async function startListening() {
  if (state.recorder || state.busy || !state.current || $("#composer").hidden) return;
  if (state.phoneMic) return;  // a paired phone is doing the listening
  stopAudio();
  let recording;
  try {
    recording = await recordUtterance(mic);
  } catch (err) {
    state.handsfree = false; applyToggles();
    return toast(`${err.message} You can also pair your phone as a microphone.`, 8000);
  }
  state.recorder = recording;
  const blob = await recording.done;
  state.recorder = null;
  if (!blob) return;
  mic.classList.add("busy");
  try {
    const res = await api("/api/transcribe", { method: "POST", body: blob, headers: { "Content-Type": blob.type } });
    if (res.text && /\w/.test(res.text)) send(res.text);
    else toast("Didn't catch that.");
  } catch (err) {
    toast("Transcription failed: " + err.message);
  } finally {
    mic.classList.remove("busy");
  }
}

// ------------------------------------------------------------------ this screen as a display
// Every tab listens for text from a paired phone and tells phones when it is busy (so they
// don't record Tana's own voice).
let clientId = "";
try { clientId = localStorage.getItem("tana.client") || ""; } catch (_) {}
if (!clientId) {
  clientId = Math.random().toString(36).slice(2) + Date.now().toString(36);
  try { localStorage.setItem("tana.client", clientId); } catch (_) {}
}

let screenName = "";
try { screenName = localStorage.getItem("tana.screen.name") || ""; } catch (_) {}
$("#screen-name").value = screenName;
$("#screen-name-form").addEventListener("submit", (e) => {
  e.preventDefault();
  screenName = $("#screen-name").value.trim();
  try { localStorage.setItem("tana.screen.name", screenName); } catch (_) {}
  displayEvents?.close();
  displayEvents = null;
  connectDisplay();  // reconnect so phones see the new name
  toast(screenName ? `This screen is now “${screenName}”.` : "Screen name cleared.");
});

let displayEvents = null;
function connectDisplay() {
  if (displayEvents) return;
  const name = screenName ? `&name=${encodeURIComponent(screenName)}` : "";
  displayEvents = new EventSource(`/api/events?role=display&id=${encodeURIComponent(clientId)}${name}`);
  displayEvents.onmessage = async (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.t !== "remote_text") return;
    state.phoneMic = true;
    $("#phone-mic-note").hidden = false;
    unlockAudio();
    // Shortcut keys may only talk to Tana/Claude, never drive Claude Code
    const unsafe = ev.from === "shortcut" && state.current?.source === "claude-code";
    if (!state.current || $("#composer").hidden || unsafe) await newThread("tana");
    if (state.busy) { toast("Still answering; try again in a moment."); return; }
    send(ev.text);
  };
}

let lastBusy = null;
function reportBusy() {
  const busy = state.busy || state.playing || state.audioQueue.length > 0;
  if (busy === lastBusy) return;
  lastBusy = busy;
  api("/api/remote/status", { method: "POST", json: { id: clientId, busy } }).catch(() => {});
}
setInterval(reportBusy, 400);

// ------------------------------------------------------------------ dictation auto-send
// Keyboard dictation (e.g. the Tesla keyboard's microphone key) inserts whole words at once.
// When that happens and the text then sits unchanged for 2 s, send it.
let dictationTimer = null;
try { state.autosend = localStorage.getItem("tana.autosend") !== "0"; } catch (_) { state.autosend = true; }
$("#autosend").checked = state.autosend;
$("#autosend").addEventListener("change", (e) => {
  state.autosend = e.target.checked;
  try { localStorage.setItem("tana.autosend", state.autosend ? "1" : "0"); } catch (_) {}
});
input.addEventListener("input", (e) => {
  clearTimeout(dictationTimer);
  const bulk = (e.data && e.data.length >= 3) || e.inputType === "insertReplacementText" || e.inputType === "insertFromDictation";
  if (!state.autosend || !bulk) return;
  dictationTimer = setTimeout(() => {
    if (input.value.trim() && document.activeElement === input) $("#composer").requestSubmit();
  }, 2000);
});
input.addEventListener("keydown", () => clearTimeout(dictationTimer));

// ------------------------------------------------------------------ this device as a microphone
const FOLLOW_UP_MS = 10000;  // after a reply, follow-ups don't need the wake word for this long
const micState = { events: null, displays: [], target: null, continuous: false, recording: null,
  awakeUntil: 0, targetWasBusy: false };
const micAwake = () => Date.now() < micState.awakeUntil;
const bigMic = $("#big-mic");

// Keep the phone screen on while it is acting as a microphone (it stops recording when locked)
let wakeLock = null;
async function keepAwake(on) {
  try {
    if (on && "wakeLock" in navigator && !wakeLock) {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => (wakeLock = null));
    } else if (!on && wakeLock) {
      await wakeLock.release();
      wakeLock = null;
    }
  } catch (_) { /* not supported or not allowed right now */ }
}
document.addEventListener("visibilitychange", () => {
  if ($("#micmode").hidden) return;
  if (document.visibilityState === "visible") {
    // iOS kills the microphone while locked/backgrounded; pick hands-free listening back up
    keepAwake(true);
    micState.recording?.cancel();
    micState.recording = null;
    if (micState.continuous) setTimeout(micListen, 300);
  } else {
    micState.recording?.cancel();
  }
});

function openMicMode() {
  $("#app").hidden = true;
  $("#micmode").hidden = false;
  keepAwake(true);
  try { micState.continuous = localStorage.getItem("tana.mic.continuous") === "1"; } catch (_) {}
  $("#mic-continuous").checked = micState.continuous;
  micState.events = new EventSource(`/api/events?role=mic&id=${encodeURIComponent(clientId + "-mic")}`);
  micState.events.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.t === "displays") updateDisplays(ev.displays);
  };
}

function closeMicMode() {
  micState.recording?.cancel();
  micState.events?.close();
  micState.events = null;
  keepAwake(false);
  $("#micmode").hidden = true;
  $("#app").hidden = false;
  if (location.pathname === "/mic") { history.replaceState(null, "", "/"); connectDisplay(); loadThreads().then(renderEmpty); }
}

function updateDisplays(list) {
  micState.displays = list.filter((d) => d.id !== clientId);
  const sel = $("#mic-target");
  let saved = "";
  try { saved = localStorage.getItem("tana.mic.target") || ""; } catch (_) {}
  const keep = micState.target && micState.displays.some((d) => d.id === micState.target) ? micState.target : null;
  const pick = keep || (micState.displays.find((d) => d.id === saved)
    || micState.displays.find((d) => /tesla|car/i.test(d.name)) || micState.displays[0] || {}).id || null;
  sel.textContent = "";
  for (const d of micState.displays) {
    const o = document.createElement("option");
    o.value = d.id;
    o.textContent = `${d.name} (…${d.id.slice(-4)})`;
    sel.append(o);
  }
  micState.target = pick;
  if (pick) sel.value = pick;
  const target = micState.displays.find((d) => d.id === pick);
  $("#mic-status").textContent = !target
    ? "No screen is open. Open Tana on the car's screen."
    : target.busy ? `${target.name} is answering…` : `Connected to ${target.name}. Tap to talk.`;
  bigMic.disabled = !target;
  if (target && target.busy) micState.awakeUntil = Infinity;  // stay awake while it answers
  if (target && micState.targetWasBusy && !target.busy) micState.awakeUntil = Date.now() + FOLLOW_UP_MS;
  micState.targetWasBusy = !!(target && target.busy);
  if (target && !target.busy && micState.continuous) {
    $("#mic-status").textContent = micAwake() ? "Listening…" : "Say “Hey Tana”…";
    if (!micState.recording) micListen();
  }
}

$("#mic-target").addEventListener("change", (e) => {
  micState.target = e.target.value;
  try { localStorage.setItem("tana.mic.target", micState.target); } catch (_) {}
});
$("#mic-continuous").addEventListener("change", (e) => {
  micState.continuous = e.target.checked;
  try { localStorage.setItem("tana.mic.continuous", micState.continuous ? "1" : "0"); } catch (_) {}
  if (micState.continuous && !micState.recording) micListen();
});
bigMic.addEventListener("click", () => {
  // tapping always means "listen to me now", no wake word needed
  micState.awakeUntil = Date.now() + FOLLOW_UP_MS;
  if (micState.recording) micState.recording.stop(); else micListen();
});

// Hands-free status line: dormant → "Say Hey Tana", awake → "Listening…"
setInterval(() => {
  const t = micState.displays.find((d) => d.id === micState.target);
  if ($("#micmode").hidden || !micState.continuous || !t || t.busy) return;
  $("#mic-status").textContent = micAwake() ? "Listening…" : "Say “Hey Tana”…";
}, 500);

async function micListen() {
  const target = micState.displays.find((d) => d.id === micState.target);
  if (!target || micState.recording || $("#micmode").hidden) return;
  if (target.busy) return;  // resumes when the screen finishes speaking
  try {
    micState.recording = await recordUtterance(bigMic);
  } catch (err) {
    micState.continuous = false;
    $("#mic-continuous").checked = false;
    return toast(err.message, 5000);
  }
  const blob = await micState.recording.done;
  micState.recording = null;
  if (!blob) { if (micState.continuous) setTimeout(micListen, 300); return; }
  bigMic.classList.add("busy");
  const needWake = micState.continuous && !micAwake();
  let delivered = false;
  try {
    const res = await api(`/api/remote/audio?display=${encodeURIComponent(micState.target)}${needWake ? "&wake=1" : ""}`,
      { method: "POST", body: blob, headers: { "Content-Type": blob.type } });
    delivered = res.sent;
    if (res.sent) {
      $("#mic-last").textContent = `“${res.text}”`;
      micState.awakeUntil = Date.now() + FOLLOW_UP_MS;
    } else if (res.woke) {
      micState.awakeUntil = Date.now() + FOLLOW_UP_MS;  // bare "Hey Tana": wait for the request
      $("#mic-last").textContent = "Yes?";
      chime();
    } else if (!needWake) {
      $("#mic-last").textContent = "Didn't catch that.";
    }
  } catch (err) {
    toast(err.message);
  } finally {
    bigMic.classList.remove("busy");
  }
  // Continuous mode normally resumes when the screen reports it's no longer busy; if the screen
  // never got busy (nothing understood), resume here instead.
  // (Ignored speech: listen again right away so a "Hey Tana" right after isn't missed.)
  setTimeout(() => {
    const t = micState.displays.find((d) => d.id === micState.target);
    if (micState.continuous && !micState.recording && t && !t.busy) micListen();
  }, delivered ? 2500 : 150);
}

// Short beep so you know Tana heard her name (phone speaker)
function chime() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.15, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.25);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.25);
    osc.onended = () => ctx.close();
  } catch (_) {}
}

$("#mic-type").addEventListener("submit", async (e) => {
  e.preventDefault();
  const box = $("#mic-type-input");
  const text = box.value.trim();
  if (!text || !micState.target) return;
  box.value = "";
  try {
    await api("/api/remote/text", { method: "POST", json: { display: micState.target, text } });
    $("#mic-last").textContent = `“${text}”`;
  } catch (err) { toast(err.message); }
});

$("#open-micmode").addEventListener("click", () => { closeSidebar(); openMicMode(); });
$("#exit-micmode").addEventListener("click", closeMicMode);

// ------------------------------------------------------------------ boards
async function openDevices() {
  $("#devices").hidden = false;
  const list = $("#device-list");
  list.textContent = "Loading…";
  const devices = await api("/api/devices");
  list.textContent = "";
  for (const d of devices) {
    const el = document.createElement("div");
    el.className = "device";
    const name = document.createElement("div");
    name.className = "device-name";
    const dot = document.createElement("span");
    dot.className = "dot" + (d.connected ? " on" : "");
    const label = document.createElement("span");
    label.textContent = `${d.name || "Board " + d.id.slice(-5)}${d.connected ? (d.awake ? " · awake" : " · listening for “Hey Tana”") : " · offline"}`;
    name.append(dot, label);
    el.append(name);
    if (d.connected && d.status) {
      el.append(slider("Volume", d.status.audio_speaker?.volume, (v) => setDevice(d.id, { volume: v })));
      el.append(slider("Brightness", d.status.screen?.brightness, (v) => setDevice(d.id, { brightness: v })));
    }
    list.append(el);
  }
  if (!devices.length) list.textContent = "No boards have checked in yet.";
}

function slider(label, value, onchange) {
  const row = document.createElement("label");
  row.className = "slider";
  const name = document.createElement("span");
  name.textContent = label;
  const input = document.createElement("input");
  input.type = "range"; input.min = "0"; input.max = "100"; input.value = String(value ?? 50);
  const out = document.createElement("span");
  out.textContent = input.value;
  input.addEventListener("input", () => (out.textContent = input.value));
  input.addEventListener("change", () => onchange(Number(input.value)));
  row.append(name, input, out);
  return row;
}

async function setDevice(id, values) {
  try { await api(`/api/devices/${encodeURIComponent(id)}`, { method: "POST", json: values }); }
  catch (err) { toast("Couldn't update the board: " + err.message); }
}

$("#open-devices").addEventListener("click", () => { closeSidebar(); openDevices(); });
$("#close-devices").addEventListener("click", () => ($("#devices").hidden = true));
$("#devices").addEventListener("click", (e) => { if (e.target.id === "devices") $("#devices").hidden = true; });

// ------------------------------------------------------------------ Siri Shortcut keys
async function openKeys() {
  document.querySelectorAll(".site-origin").forEach((e) => (e.textContent = location.origin));
  $("#keys").hidden = false;
  $("#key-new").hidden = true;
  const list = $("#key-list");
  list.textContent = "";
  for (const k of await api("/api/keys")) {
    const row = document.createElement("div");
    row.className = "device";
    const label = document.createElement("span");
    label.textContent = `${k.name} · created ${ago(k.created)}${k.last_used ? " · used " + ago(k.last_used) : " · never used"}`;
    const del = document.createElement("button");
    del.className = "danger";
    del.textContent = "Delete";
    del.addEventListener("click", async () => {
      if (!confirm(`Delete key “${k.name}”? Shortcuts using it will stop working.`)) return;
      await api(`/api/keys/${k.id}`, { method: "DELETE" });
      openKeys();
    });
    row.append(label, del);
    list.append(row);
  }
}
$("#key-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const k = await api("/api/keys", { method: "POST", json: { name: $("#key-name").value.trim() } });
  $("#key-name").value = "";
  await openKeys();
  $("#key-value").value = k.token;
  $("#key-new").hidden = false;
});
$("#key-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#key-value").value); toast("Copied"); }
  catch (_) { $("#key-value").select(); toast("Select and copy the key"); }
});
$("#open-keys").addEventListener("click", () => { closeSidebar(); openKeys(); });
$("#close-keys").addEventListener("click", () => ($("#keys").hidden = true));

// ------------------------------------------------------------------ narrow layout
function closeSidebar() { $("#sidebar").classList.remove("open"); }
$("#open-sidebar").addEventListener("click", () => $("#sidebar").classList.add("open"));
$("#close-sidebar").addEventListener("click", closeSidebar);

boot();
