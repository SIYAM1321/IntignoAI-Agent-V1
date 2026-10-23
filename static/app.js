const $ = (id) => document.getElementById(id);

const state = {
  sessionId: null,
  title: "New chat",
  pinned: false,
  messages: [],
  queuedMessages: [],
  activities: [],
  pending: null,
  busy: false,
  liveResponse: "",
  stream: null,
  chats: [],
  chatSearch: "",
  openMenuChatId: null,
  attachments: [],
  recoveryAvailable: false,
  recoveryPhase: null,
  selectedModel: localStorage.getItem('intigno-chat-model') || 'z-ai/glm-5.3-flash',
  reasoningEffort: localStorage.getItem('intigno-reasoning-effort') || 'high',
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function configureMarkdown() {
  if (!window.marked) return;
  marked.setOptions({ gfm: true, breaks: true, pedantic: false, headerIds: false, mangle: false });
  const renderer = new marked.Renderer();
  renderer.code = ({ text, lang }) => {
    const language = (lang || "text").toLowerCase().split(/[ :]/)[0].replace(/[^a-z0-9_+#-]/g, "") || "text";
    let html;
    try {
      if (window.hljs && language !== "text" && hljs.getLanguage(language)) {
        html = hljs.highlight(text, { language }).value;
      } else if (window.hljs) {
        html = hljs.highlightAuto(text).value;
      } else {
        html = escapeHtml(text);
      }
    } catch {
      html = escapeHtml(text);
    }
    return `<div class="code-block"><div class="code-toolbar"><span class="code-lang">${escapeHtml(language)}</span><button class="copy-btn" data-copy-kind="code" title="Copy code">Copy</button></div><pre><code class="hljs language-${escapeHtml(language)}">${html}</code></pre></div>`;
  };
  marked.use({ renderer });
}
configureMarkdown();

function renderMarkdown(source) {
  const text = String(source ?? "");
  if (!window.marked) return `<p>${escapeHtml(text).replaceAll("\n", "<br>")}</p>`;
  let html;
  try { html = marked.parse(text); }
  catch { html = `<p>${escapeHtml(text).replaceAll("\n", "<br>")}</p>`; }
  if (window.DOMPurify) {
    html = DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      ADD_ATTR: ["target", "rel", "class"],
      FORBID_ATTR: ["onclick", "onerror", "onload", "style"],
    });
  }
  return html;
}

function renderMath(root) {
  if (!root || !window.renderMathInElement) return;
  try {
    renderMathInElement(root, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "\\(", right: "\\)", display: false },
        { left: "$", right: "$", display: false },
      ],
      throwOnError: false,
      strict: "ignore",
      trust: false,
      ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
    });
  } catch {}
}

async function copyText(text, button) {
  const value = String(text ?? "");
  let copied = false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      copied = true;
    }
  } catch {}
  if (!copied) {
    let ta;
    try {
      ta = document.createElement("textarea");
      ta.value = value;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      ta.style.top = "0";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      ta.setSelectionRange(0, ta.value.length);
      copied = !!document.execCommand("copy");
    } catch { copied = false; }
    finally { if (ta?.parentNode) ta.parentNode.removeChild(ta); }
  }
  if (button) {
    const old = button.dataset.originalLabel || button.textContent || "Copy";
    button.dataset.originalLabel = old;
    button.textContent = copied ? "Copied" : "Copy failed";
    button.classList.toggle("copied", copied);
    button.classList.toggle("copy-failed", !copied);
    if (!copied) button.title = "Clipboard permission was blocked. Select and copy the text manually.";
    setTimeout(() => { button.textContent = old; button.classList.remove("copied", "copy-failed"); }, 1800);
  }
  return copied;
}

function addCopyButtons(root) {
  if (!root) return;
  root.querySelectorAll(".code-block").forEach(block => {
    const btn = block.querySelector('[data-copy-kind="code"]');
    const code = block.querySelector("pre code");
    if (btn && code) btn.dataset.copy = code.textContent;
  });
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function createSession() {
  const data = await api('/api/session', { method: 'POST', body: '{}' });
  applySessionData(data);
  await refreshChatList();
}

async function openChat(chatId) {
  if (!chatId || chatId === state.sessionId) return;
  try {
    const data = await api(`/api/chats/${encodeURIComponent(chatId)}`);
    applySessionData(data);
    await refreshChatList();
  } catch (err) { addLocalError(err.message); }
}

function applySessionData(data) {
  const previousSessionId = state.sessionId;
  state.sessionId = data.session_id;
  if (previousSessionId && previousSessionId !== state.sessionId) {
    state.attachments = [];
    $('messageInput').value = '';
    autoGrow();
    renderAttachmentTray();
  }
  state.title = data.state?.title || "New chat";
  state.pinned = !!data.state?.pinned;
  state.messages = data.state?.messages || [];
  state.queuedMessages = data.state?.queued_messages || [];
  state.activities = data.state?.activities || [];
  state.pending = data.state?.pending_confirmation || null;
  state.liveResponse = data.state?.live_response || "";
  state.busy = !!data.state?.busy;
  state.recoveryAvailable = !!data.state?.recovery_available;
  state.recoveryPhase = data.state?.recovery_phase || null;
  renderAll();
  connectActivityStream();
}

async function refreshChatList() {
  try {
    const data = await api('/api/chats');
    state.chats = data.chats || [];
    renderChatList();
  } catch {}
}

function renderChatList() {
  const root = $('chatList');
  const query = state.chatSearch.trim().toLowerCase();
  const filtered = query
    ? state.chats.filter(c => (c.title || '').toLowerCase().includes(query))
    : state.chats;
  if (!filtered.length) {
    root.innerHTML = `<div class="chat-list-empty">${query ? 'No matching chats.' : 'No chats yet. Start one!'}</div>`;
    return;
  }
  const pinned = filtered.filter(c => c.pinned);
  const others = filtered.filter(c => !c.pinned);
  const renderItem = (c) => {
    const active = c.id === state.sessionId;
    const menuOpen = state.openMenuChatId === c.id;
    return `<div class="chat-item ${active ? 'active' : ''}" data-chat-id="${escapeHtml(c.id)}">
      <div class="chat-item-icon">${c.pinned ? '📌' : '💬'}</div>
      <div class="chat-item-body"><div class="chat-item-title">${escapeHtml(c.title || 'New chat')}</div></div>
      <button class="chat-item-menu-btn ${menuOpen ? 'force-visible' : ''}" data-menu-for="${escapeHtml(c.id)}" title="Chat options">⋯</button>
      ${menuOpen ? `<div class="chat-item-menu" data-menu-panel="${escapeHtml(c.id)}">
        <button data-action="pin" data-chat-id="${escapeHtml(c.id)}">${c.pinned ? '📌 Unpin' : '📌 Pin'}</button>
        <button data-action="rename" data-chat-id="${escapeHtml(c.id)}">✎ Rename</button>
        <button data-action="delete" data-chat-id="${escapeHtml(c.id)}" class="danger-item">🗑 Delete</button>
      </div>` : ''}
    </div>`;
  };
  let html = '';
  if (pinned.length) html += `<div class="chat-list-group-label">Pinned</div>${pinned.map(renderItem).join('')}`;
  if (others.length) html += `${pinned.length ? '<div class="chat-list-group-label">All chats</div>' : ''}${others.map(renderItem).join('')}`;
  root.innerHTML = html;
}

async function renameChatPrompt(chatId) {
  const chat = state.chats.find(c => c.id === chatId);
  $('renameInput').value = chat?.title || '';
  $('renameStatus').textContent = '';
  $('renameModal').dataset.chatId = chatId;
  $('renameModal').classList.remove('hidden');
  setTimeout(() => $('renameInput').focus(), 30);
}

async function saveRename() {
  const chatId = $('renameModal').dataset.chatId;
  const title = $('renameInput').value.trim();
  if (!title) { $('renameStatus').textContent = 'Title cannot be empty.'; return; }
  try {
    await api(`/api/chats/${encodeURIComponent(chatId)}`, { method: 'PATCH', body: JSON.stringify({ title }) });
    if (chatId === state.sessionId) { state.title = title; renderChatHeader(); }
    await refreshChatList();
    $('renameModal').classList.add('hidden');
  } catch (err) { $('renameStatus').textContent = err.message; }
}

async function togglePin(chatId) {
  const chat = state.chats.find(c => c.id === chatId);
  if (!chat) return;
  try {
    await api(`/api/chats/${encodeURIComponent(chatId)}/pin`, { method: 'POST', body: JSON.stringify({ pinned: !chat.pinned }) });
    if (chatId === state.sessionId) state.pinned = !chat.pinned;
    await refreshChatList();
  } catch (err) { addLocalError(err.message); }
}

function deleteChatPrompt(chatId) {
  $('deleteChatModal').dataset.chatId = chatId;
  const chat = state.chats.find(c => c.id === chatId);
  $('deleteChatText').textContent = `This permanently removes "${chat?.title || 'this chat'}", its messages and its activity log. This cannot be undone.`;
  $('deleteChatModal').classList.remove('hidden');
}

async function confirmDeleteChat() {
  const chatId = $('deleteChatModal').dataset.chatId;
  try {
    await api(`/api/chats/${encodeURIComponent(chatId)}`, { method: 'DELETE' });
    $('deleteChatModal').classList.add('hidden');
    if (chatId === state.sessionId) {
      await createSession();
    } else {
      await refreshChatList();
    }
  } catch (err) { addLocalError(err.message); }
}

function renderChatHeader() {
  $('chatTitleInline').textContent = state.title || 'New chat';
}

function toggleSidebar(force) {
  const grid = document.querySelector('.main-grid');
  const collapsed = typeof force === 'boolean' ? !force : !grid.classList.contains('sidebar-collapsed');
  grid.classList.toggle('sidebar-collapsed', collapsed);
  if (window.innerWidth > 920) {
    const sw = collapsed ? 0 : (Number(localStorage.getItem('intigno-sidebar-width')) || 250);
    const aw = Number(localStorage.getItem('intigno-activity-width')) || 480;
    grid.style.gridTemplateColumns = `${sw}px minmax(320px, 1fr) ${aw}px`;
    grid.style.setProperty('--sidebar-resizer-left', `${sw}px`);
    grid.style.setProperty('--activity-resizer-right', `${aw}px`);
  }
}

function connectActivityStream() {
  if (state.stream) state.stream.close();
  if (!state.sessionId) return;
  state.stream = new EventSource(`/api/activity/stream?session_id=${encodeURIComponent(state.sessionId)}`);
  let wasBusy = false;
  state.stream.addEventListener('state', (event) => {
    try {
      const data = JSON.parse(event.data);
      state.messages = data.messages || state.messages;
      state.queuedMessages = data.queued_messages || [];
      state.activities = data.activities || [];
      const browserActivity = [...state.activities].reverse().find(a => {
        const name = String(a.tool || '').toLowerCase();
        const desc = String(a.description || '').toLowerCase();
        return ['browser_action','browser_scrape','browser_test','browser_profiles','linkedin_login','linkedin_create_post','linkedin_scrape_jobs','linkedin_easy_apply','youtube_upload','youtube_like','youtube_comment','youtube_subscribe'].includes(name) && ['running','success'].includes(String(a.status || ''));
      });
      if (browserActivity) autoShowEmbeddedBrowser(browserActivity);
      const lastTool = [...state.activities].reverse().find(a => a.type === 'tool');
      if (lastTool && /^(browser|linkedin|youtube)/i.test(String(lastTool.tool || '')) && findHumanAction(lastTool.output) && state.browserStream.lastHumanId !== lastTool.id) {
        state.browserStream.lastHumanId = lastTool.id;
        focusBrowserPanel();
      }
      state.pending = data.pending_confirmation || null;
      state.busy = !!data.busy;
      state.recoveryAvailable = !!data.recovery_available;
      state.recoveryPhase = data.recovery_phase || null;
      state.liveResponse = data.live_response || "";
      if (data.title) state.title = data.title;
      state.pinned = !!data.pinned;
      renderAll();
      // A task just finished (busy -> idle): the chat may have been auto-titled
      // from its first message, and its position in the list may have moved.
      if (wasBusy && !state.busy) refreshChatList();
      wasBusy = state.busy;
    } catch {}
  });
}

function renderAll() {
  renderChatHeader();
  renderMessages();
  renderActivities();
  renderConfirmation();
  renderRecovery();
  renderAttachmentTray();
  updateComposer();
}

// ---------------------------------------------------------------------------
// v1.12 chat timeline: response -> logs -> response -> logs -> ... -> final response
// ---------------------------------------------------------------------------
const ui = { openGroups: new Set(), closedGroups: new Set(), openSteps: new Set() };
const mdCache = new Map();

function mdCached(text) {
  const key = String(text ?? "");
  if (mdCache.has(key)) return mdCache.get(key);
  const html = renderMarkdown(key);
  mdCache.set(key, html);
  if (mdCache.size > 400) mdCache.delete(mdCache.keys().next().value);
  return html;
}

const ICONS = {
  globe: '<svg class="ticon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.2 3 14.8 0 18M12 3c-3 3.2-3 14.8 0 18"/></svg>',
  terminal: '<svg class="ticon" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="m7 9 3 3-3 3M13 15h4"/></svg>',
  file: '<svg class="ticon" viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
  search: '<svg class="ticon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  bolt: '<svg class="ticon" viewBox="0 0 24 24"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>',
  chev: '<svg class="chev" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>',
};

function toolIcon(tool) {
  const t = String(tool || "").toLowerCase();
  if (/^(browser|linkedin|youtube)/.test(t)) return ICONS.globe;
  if (/terminal|port/.test(t)) return ICONS.terminal;
  if (/file|directory/.test(t)) return ICONS.file;
  if (/search|fetch/.test(t)) return ICONS.search;
  return ICONS.bolt;
}

function stIcon(status) {
  const st = String(status || "running");
  const glyph = st === "success" ? "✓" : st === "error" ? "✕" : st === "pending" ? "!" : st === "stopped" ? "■" : "";
  return `<span class="st st-${escapeHtml(st)}">${glyph}</span>`;
}

function stringify(value) {
  if (value == null || value === "") return "";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function isEmptyObj(v) { return v == null || v === "" || (typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length); }

function findHumanAction(obj, depth = 0) {
  if (!obj || typeof obj !== "object" || depth > 6) return null;
  if (obj.human_action_required) return String(obj.message || "Your input is needed in the embedded browser.");
  for (const v of Object.values(obj)) {
    const r = findHumanAction(v, depth + 1);
    if (r) return r;
  }
  return null;
}

function buildTimeline() {
  const items = [];
  let lastTime = 0;
  state.messages.forEach((m, index) => {
    const t = Number(m.created_at);
    const time = Number.isFinite(t) && t > 0 ? t : lastTime;
    lastTime = Math.max(lastTime, time);
    items.push({ kind: "msg", time, order: index, msg: m, index });
  });
  state.activities.filter(a => a.type === "tool").forEach((a, i) => {
    items.push({ kind: "act", time: Number(a.started_at) || 0, order: 100000 + i, act: a });
  });
  items.sort((x, y) => (x.time - y.time) || (x.order - y.order));
  const out = [];
  let cur = null;
  for (const it of items) {
    if (it.kind === "act") {
      if (!cur) { cur = { kind: "group", acts: [] }; out.push(cur); }
      cur.acts.push(it.act);
    } else { cur = null; out.push(it); }
  }
  return out;
}

function renderStep(a) {
  const status = String(a.status || "running");
  const open = ui.openSteps.has(a.id);
  let detail = "";
  if (open) {
    const input = isEmptyObj(a.input) ? "" : stringify(a.input);
    const rawOut = a.output && typeof a.output === "object" && typeof a.output.output === "string" && Object.keys(a.output).length > 1 ? a.output : a.output;
    const output = isEmptyObj(rawOut) ? "" : stringify(rawOut);
    detail = `<div class="step-detail">${input ? `<div class="dl">Input</div><pre>${escapeHtml(input)}</pre>` : ""}${output ? `<div class="dl">Output</div><pre>${escapeHtml(output)}</pre>` : ""}${a.error ? `<div class="dl">Error</div><pre class="err">${escapeHtml(a.error)}</pre>` : ""}${!input && !output && !a.error ? '<div class="dl">No details yet</div>' : ""}</div>`;
  }
  const dur = a.duration != null ? `${Number(a.duration).toFixed(1)}s` : (status === "running" ? "…" : "");
  return `<div class="step ${open ? "is-open" : ""}"><button class="step-head" data-step="${escapeHtml(a.id)}" type="button">${stIcon(status)}${toolIcon(a.tool)}<span class="step-label">${escapeHtml(a.description || a.tool || "Step")}</span><span class="step-tool">${escapeHtml(a.tool || "tool")}</span><span class="step-dur">${escapeHtml(dur)}</span>${ICONS.chev}</button>${detail}</div>`;
}

function renderGroup(group) {
  const acts = group.acts;
  const key = `g:${acts[0].id}`;
  const running = acts.some(a => a.status === "running");
  const pending = acts.some(a => a.status === "pending");
  const failed = acts.filter(a => a.status === "error").length;
  const live = running || pending;
  const open = ui.openGroups.has(key) || (!ui.closedGroups.has(key) && live);
  const current = acts.find(a => a.status === "running") || acts.find(a => a.status === "pending") || acts[acts.length - 1];
  const n = acts.length;
  const title = running ? (current.description || "Working…") : pending ? "Waiting for your approval" : `Ran ${n} step${n > 1 ? "s" : ""}${failed ? ` · ${failed} failed` : ""}`;
  const preview = acts.slice(0, 3).map(a => a.description || a.tool).filter(Boolean).join("  ·  ");
  const total = acts.reduce((sum, a) => sum + (Number(a.duration) || 0), 0);
  const headIcon = running ? stIcon("running") : pending ? stIcon("pending") : failed === n ? stIcon("error") : failed ? stIcon("pending") : stIcon("success");
  const body = open ? `<div class="steps-body">${acts.map(renderStep).join("")}</div>` : "";
  const html = `<div class="steps ${running ? "running" : ""} ${open ? "is-open" : ""}"><button class="steps-head" data-group="${escapeHtml(key)}" type="button">${headIcon}<span class="steps-title"><b>${escapeHtml(title)}</b>${!open && preview ? `<small>${escapeHtml(preview)}</small>` : ""}</span><span class="steps-meta">${total ? total.toFixed(1) + "s" : ""}</span>${ICONS.chev}</button>${body}</div>`;
  return { key, html };
}

function renderUserMsg(m, index) {
  return { key: `m${index}`, html: `<div class="turn-user"><div class="bubble-user markdown">${mdCached(m.content)}</div></div>`, copy: null };
}

function renderAssistantMsg(m, index) {
  return {
    key: `m${index}`,
    html: `<div class="turn-assistant"><div class="markdown">${mdCached(m.content)}</div><div class="turn-tools"><button class="copy-btn copy-message" title="Copy response" aria-label="Copy response">Copy</button></div></div>`,
    copy: m.content || "",
  };
}

function lastBrowserNotice() {
  const tools = state.activities.filter(a => a.type === "tool");
  const last = tools[tools.length - 1];
  if (!last) return null;
  if (!/^(browser|linkedin|youtube)/i.test(String(last.tool || ""))) return null;
  return findHumanAction(last.output);
}

function postProcessNode(el, copyText) {
  el.querySelectorAll(".markdown").forEach(root => { renderMath(root); addCopyButtons(root); });
  const btn = el.querySelector(".copy-message, .copy-live");
  if (btn) btn.dataset.copy = copyText || "";
}

function reconcileList(container, items) {
  const existing = new Map();
  Array.from(container.children).forEach(c => existing.set(c.dataset.key, c));
  let prev = null;
  items.forEach(({ key, html, copy }) => {
    let el = existing.get(key);
    if (!el) {
      el = document.createElement("div");
      el.dataset.key = key;
      el.innerHTML = html;
      el._html = html;
      postProcessNode(el, copy);
    } else if (el._html !== html) {
      el.innerHTML = html;
      el._html = html;
      postProcessNode(el, copy);
    }
    existing.delete(key);
    const ref = prev ? prev.nextSibling : container.firstChild;
    if (el !== ref) container.insertBefore(el, ref);
    prev = el;
  });
  existing.forEach(el => el.remove());
}

function renderMessages() {
  const scroll = $("chatScroll");
  const list = $("messageList");
  const empty = $("emptyState");
  empty.classList.toggle("hidden", state.messages.length > 0 || !!state.liveResponse || state.activities.some(a => a.type === "tool"));
  const wasNearBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 220;

  const items = [];
  for (const node of buildTimeline()) {
    if (node.kind === "group") items.push(renderGroup(node));
    else if (node.msg.role === "user") items.push(renderUserMsg(node.msg, node.index));
    else items.push(renderAssistantMsg(node.msg, node.index));
  }

  const notice = lastBrowserNotice();
  if (notice) {
    items.push({ key: "human-notice", html: `<div class="human-card"><div class="hc-text"><b>Your input is needed in the embedded browser</b><span>${escapeHtml(notice)}</span></div><button class="primary-button" data-open-browser="1" type="button">Open browser</button></div>` });
    showHumanBanner(notice);
  } else {
    showHumanBanner("");
  }

  const hasRunningTool = state.activities.some(a => a.type === "tool" && ["running", "pending"].includes(a.status));
  if (state.liveResponse && state.busy) {
    items.push({ key: "live", html: `<div class="turn-assistant turn-live"><div class="markdown">${mdCached(state.liveResponse)}</div><span class="cursor-dot"></span></div>`, copy: state.liveResponse });
  } else if (state.busy && !hasRunningTool) {
    items.push({ key: "thinking", html: `<div class="thinking"><i></i><i></i><i></i><span>Thinking…</span></div>` });
  }

  state.queuedMessages.forEach((item, index) => {
    const content = String(item.message || "").trim() || "Please inspect the attached file(s) and help me with them.";
    const files = (item.attachment_names || []).length ? `<div class="queued-attachments">Attachments: ${escapeHtml(item.attachment_names.join(", "))}</div>` : "";
    items.push({ key: `q${index}`, html: `<div class="turn-user queued"><div class="bubble-user markdown"><span class="queued-tag">Queued · ${index + 1}</span>${mdCached(content)}${files}</div></div>` });
  });

  reconcileList(list, items);
  if (wasNearBottom) scroll.scrollTop = scroll.scrollHeight;
}

function showHumanBanner(text) {
  const banner = $("browserHumanBanner");
  if (!banner) return;
  banner.classList.toggle("hidden", !text);
  $("browserHumanText").textContent = text || "";
}

function focusBrowserPanel() {
  setBrowserTabActive("browser");
  if (window.matchMedia("(max-width: 920px)").matches) document.body.classList.add("activity-open");
  const img = $("browserStreamImg");
  if (img && img.style.display !== "none") setTimeout(() => img.focus({ preventScroll: true }), 50);
}

function decodeHtml(value) {
  const ta = document.createElement('textarea');
  ta.innerHTML = String(value ?? '');
  return ta.value;
}

function iconFor(activity) {
  if (activity.status === 'success') return '✓';
  if (activity.status === 'error') return '✕';
  if (activity.status === 'pending') return '⚠';
  if (activity.status === 'stopped') return '■';
  return '→';
}
function statusClass(status) { return `status-${status}`; }
function formatDuration(seconds) { return seconds == null ? '—' : `${Number(seconds).toFixed(2)} sec`; }
function pretty(obj) { try { return JSON.stringify(obj ?? {}, null, 2); } catch { return String(obj ?? ''); } }

function renderActivities() {
  const root = $('activityScroll');
  $('activityMeta').textContent = state.busy ? 'Agent is working…' : (state.activities.length ? `${state.activities.length} events` : 'Waiting for a task');
  if (!state.activities.length) {
    root.innerHTML = `<div class="activity-empty"><div class="pulse-ring"></div><strong>No activity yet</strong><span>Tool calls, processes and results will appear here live.</span></div>`;
    return;
  }
  root.innerHTML = `<div class="activity-list">${state.activities.map((a) => {
    const pid = a.output?.process_id || '';
    const live = a.status === 'running' && pid ? `<span class="process-pill">PID ${escapeHtml(pid)}</span>` : '';
    return `<div class="activity-card" data-id="${escapeHtml(a.id)}">
      <button class="activity-summary" onclick="toggleActivity('${escapeHtml(a.id)}')">
        <div class="activity-icon ${statusClass(a.status)}">${iconFor(a)}</div>
        <div class="activity-main"><div class="activity-desc">${escapeHtml(a.description)}</div><div class="activity-time">${escapeHtml(a.tool || a.type || 'agent')} · ${formatDuration(a.duration)} ${live}</div></div>
        <div class="activity-status ${statusClass(a.status)}">${escapeHtml(a.status)}</div>
        <div class="activity-chevron">›</div>
      </button>
      <div class="activity-details">
        <div class="detail-grid">
          <div class="detail-label">Type</div><div class="detail-value">${escapeHtml(a.type)}</div>
          <div class="detail-label">Tool</div><div class="detail-value">${escapeHtml(a.tool || 'Agent')}</div>
          <div class="detail-label">Started</div><div class="detail-value">${new Date(Number(a.started_at) * 1000).toLocaleTimeString()}</div>
          <div class="detail-label">Duration</div><div class="detail-value live-duration" data-started="${Number(a.started_at)}">${formatDuration(a.duration)}</div>
          <div class="detail-label">Status</div><div class="detail-value ${statusClass(a.status)}">${escapeHtml(a.status)}</div>
          ${a.output?.process_id ? `<div class="detail-label">Process</div><div class="detail-value"><strong>${escapeHtml(a.output.process_id)}</strong> · PID ${escapeHtml(a.output.pid)} · ${escapeHtml(a.output.status || '')}</div>` : ''}
          ${a.output?.working_directory ? `<div class="detail-label">CWD</div><div class="detail-value">${escapeHtml(a.output.working_directory)}</div>` : ''}
          ${a.output?.exit_code !== undefined && a.output?.exit_code !== null ? `<div class="detail-label">Exit Code</div><div class="detail-value">${escapeHtml(a.output.exit_code)}</div>` : ''}
          ${a.output?.output ? `<div class="detail-label">Logs</div><div class="detail-value copyable-pre"><pre>${escapeHtml(a.output.output)}</pre></div>` : ''}
          ${a.error ? `<div class="detail-label">Error</div><div class="detail-value error-text">${escapeHtml(a.error)}</div>` : ''}
          <div class="detail-label">Input</div><div class="detail-value copyable-pre"><pre>${escapeHtml(pretty(a.input))}</pre></div>
          <div class="detail-label">Output</div><div class="detail-value copyable-pre"><pre>${escapeHtml(pretty(a.output))}</pre></div>
        </div>
      </div>
    </div>`;
  }).join('')}</div>`;
  const cards = [...root.querySelectorAll('.activity-card')];
  if (state.busy && cards.length) cards[cards.length - 1].classList.add('open');
  root.scrollTop = root.scrollHeight;
}

window.toggleActivity = (id) => {
  const card = document.querySelector(`.activity-card[data-id="${CSS.escape(id)}"]`);
  if (card) card.classList.toggle('open');
};

function renderConfirmation() { /* Approvals are handled through normal chat replies. */ }

function renderRecovery() {
  const bar = $('recoveryBar');
  if (!state.recoveryAvailable || state.busy || state.pending) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  const phase = state.recoveryPhase || 'failed turn';
  $('recoveryText').textContent = phase === 'function_results_pending'
    ? 'The tool outputs were saved. Resume will reuse them without rerunning those tools.'
    : phase === 'fallback_pending'
      ? 'The tool-result response failed. Resume can try a fresh conversation using the saved result snapshot.'
      : 'A failed model turn was checkpointed. Resume will continue from the saved request.';
}

function renderAttachmentTray() {
  const tray = $('attachmentTray');
  if (!state.attachments.length) { tray.classList.add('hidden'); tray.innerHTML = ''; return; }
  tray.classList.remove('hidden');
  tray.innerHTML = state.attachments.map(item => `<div class="attachment-chip"><span title="${escapeHtml(item.name)}">📎 ${escapeHtml(item.name)} · ${formatBytes(item.size)}</span><button type="button" data-remove-attachment="${escapeHtml(item.id)}" title="Remove attachment" aria-label="Remove ${escapeHtml(item.name)}">×</button></div>`).join('');
}

function formatBytes(size) {
  const n = Number(size || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function updateComposer() {
  // A running turn has a recovery checkpoint by design; that must not disable
  // follow-up messages or turn the Stop icon into a disabled control.
  const blocked = state.recoveryAvailable && !state.busy;
  const draft = !!$('messageInput').value.trim() || state.attachments.length > 0;
  const showStop = state.busy && !draft;
  $('messageInput').disabled = blocked;
  $('attachBtn').disabled = blocked || state.busy;
  $('fileInput').disabled = blocked || state.busy;
  $('sendBtn').innerHTML = showStop
    ? '<svg class="stop-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="6.5" y="6.5" width="11" height="11" rx="2"/></svg>'
    : '<svg class="send-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 12 20 4.5l-4.2 15-3.1-6.2L4.5 12Z"/><path d="m12.7 13.3 3.1-3.1"/></svg>';
  $('sendBtn').title = showStop ? 'Stop current task' : (state.busy ? 'Queue message' : 'Send message');
  $('sendBtn').setAttribute('aria-label', showStop ? 'Stop current task' : (state.busy ? 'Queue message' : 'Send message'));
  $('sendBtn').classList.toggle('stop-mode', showStop);
  $('sendBtn').disabled = blocked || (!state.busy && !draft);
  $('newChatBtn').disabled = false;
}

async function uploadFiles(fileList) {
  if (!state.sessionId || state.busy || state.pending || state.recoveryAvailable) return;
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const remaining = Math.max(0, 8 - state.attachments.length);
  if (files.length > remaining) { addLocalError(`You can attach up to 8 files per message; ${remaining} slot(s) remain.`); return; }
  const tooLarge = files.find(file => file.size > 10 * 1024 * 1024);
  if (tooLarge) { addLocalError(`${tooLarge.name} exceeds the 10 MB per-file limit.`); return; }
  const alreadyAttachedBytes = state.attachments.reduce((sum, item) => sum + Number(item.size || 0), 0);
  const incomingBytes = files.reduce((sum, item) => sum + item.size, 0);
  if (alreadyAttachedBytes + incomingBytes > 30 * 1024 * 1024) {
    addLocalError('All attachments for one message must total 30 MB or less.'); return;
  }
  const form = new FormData();
  form.append('session_id', state.sessionId);
  files.forEach(file => form.append('files', file, file.name));
  $('attachBtn').disabled = true;
  try {
    const response = await fetch('/api/upload', { method: 'POST', body: form });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Upload failed (HTTP ${response.status})`);
    state.attachments.push(...(data.attachments || []));
    renderAttachmentTray();
  } catch (err) { addLocalError(err.message); }
  finally { $('attachBtn').disabled = false; $('fileInput').value = ''; updateComposer(); }
}

async function sendMessage(message) {
  const text = String(message ?? $('messageInput').value).trim();
  if ((!text && !state.attachments.length) || (state.recoveryAvailable && !state.busy)) return;
  const targetSessionId = state.sessionId;
  const chosenAttachments = state.attachments.slice();
  $('messageInput').value = '';
  autoGrow();
  updateComposer();
  try {
    const accepted = await api('/api/chat', { method: 'POST', body: JSON.stringify({ session_id: targetSessionId, message: text, attachments: chosenAttachments.map(item => item.id), model: $('chatModelSelect').value || state.selectedModel, reasoning_effort: $('reasoningEffort').value }) });
    if (state.sessionId === targetSessionId) {
      state.attachments = [];
      if (accepted.queued) {
        const snapshot = await api(`/api/chats/${encodeURIComponent(targetSessionId)}`);
        if (state.sessionId === targetSessionId) {
          state.queuedMessages = snapshot.state?.queued_messages || [];
          state.messages = snapshot.state?.messages || state.messages;
        }
      }
      renderAttachmentTray();
      renderMessages();
      updateComposer();
    }
  } catch (err) {
    if (state.sessionId === targetSessionId) $('messageInput').value = text;
    addLocalError(err.message);
    updateComposer();
  }
}

function addLocalNotice(message) { state.messages.push({ role: 'assistant', content: `ℹ️ ${message}`, created_at: Date.now() / 1000 }); renderMessages(); }
function addLocalError(message) { state.messages.push({ role: 'assistant', content: `Error: ${message}`, created_at: Date.now() / 1000 }); renderMessages(); }
function autoGrow() { const input = $('messageInput'); input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 160)}px`; }

async function refreshStatus() {
  try {
    const data = await api('/api/status');
    $('modelText').textContent = `${data.model}`;
    $('osText').textContent = `${data.os_name} • ${data.shell}`;
    $('workspaceInline').textContent = data.workspace;
    $('rateChip').textContent = `AI ${data.rate_limit.remaining}/${data.rate_limit.limit} left`;
    $('rateChip').title = `${data.rate_limit.used} requests in rolling ${data.rate_limit.window_seconds}s; hard cap ${data.rate_limit.hard_cap}/min. Other apps sharing this API key are not counted.`;
    $('serverStatus').innerHTML = `<span class="dot"></span> Local • ${escapeHtml(data.os_name)}`;
  } catch {
    $('serverStatus').innerHTML = `<span class="dot offline-dot"></span> Offline`;
    $('osText').textContent = 'Operating system unavailable';
  }
}

async function openSettings() {
  try {
    const data = await api('/api/settings');
    $('workspaceInput').value = data.workspace || '';
    $('modelInput').value = data.model || 'z-ai/glm-5.3-flash';
    $('nvidiaApiKeyInput').value = '';
    $('nvidiaApiKeyHelp').textContent = data.nvidia_api_key_configured
      ? `NVIDIA key configured (ending ${data.nvidia_api_key_suffix}). Leave blank to keep it, or paste a new key to replace it.`
      : 'No NVIDIA API key is saved yet. Paste your NVIDIA NIM key to use GLM-5.3-Flash.';
    $('rpmInput').value = data.requests_per_minute || 10;
    $('retriesInput').value = data.retries ?? 1;
    $('apiKeyInput').value = '';
    $('apiKeyHelp').textContent = data.api_key_configured
      ? `API key configured (ending ${data.api_key_suffix}). Leave blank to keep it, or paste a new key to replace it. The key is not returned to the browser.`
      : 'No API key is saved yet. Paste your Google AI Studio API key here. The key is stored locally in .env and never echoed back.';
    $('settingsStatus').textContent = '';
    await refreshKnowledge();
  } catch (err) { $('settingsStatus').textContent = err.message; }
  $('settingsModal').classList.remove('hidden');
}

async function saveWorkspace() {
  const payload = {
    workspace: $('workspaceInput').value.trim(),
    model: $('modelInput').value.trim(),
    api_key: $('apiKeyInput').value.trim(),
    nvidia_api_key: $('nvidiaApiKeyInput').value.trim(),
    requests_per_minute: $('rpmInput').value,
    retries: $('retriesInput').value,
  };
  $('saveWorkspaceBtn').disabled = true;
  try {
    const data = await api('/api/settings', { method: 'POST', body: JSON.stringify(payload) });
    $('workspaceInline').textContent = data.workspace;
    $('modelText').textContent = `${data.model} • Local tools`;
    $('apiKeyInput').value = '';
    $('nvidiaApiKeyInput').value = '';
    $('apiKeyHelp').textContent = data.api_key_configured ? 'Google API key saved locally. It will not be shown again.' : 'No Google API key configured.';
    $('nvidiaApiKeyHelp').textContent = data.nvidia_api_key_configured ? 'NVIDIA API key saved locally. It will not be shown again.' : 'No NVIDIA API key configured.';
    $('settingsStatus').textContent = `Settings saved. App cap: ${data.requests_per_minute} request(s) per rolling minute (hard maximum 10).`;
    await refreshStatus();
    setTimeout(() => $('settingsModal').classList.add('hidden'), 650);
  } catch (err) { $('settingsStatus').textContent = err.message; }
  finally { $('saveWorkspaceBtn').disabled = false; }
}

async function refreshKnowledge() {
  try {
    const data = await api('/api/knowledge?kind=both');
    const memory = data.memory || {};
    const skills = data.skills || {};
    $('knowledgeSummary').textContent = `Append-only files · MEMORY.MD: ${memory.characters || 0} characters · SKILLS.MD: ${skills.characters || 0} characters. New entries are available to the next model turn.`;
  } catch (err) { $('knowledgeSummary').textContent = err.message; }
}

async function addKnowledgeEntry() {
  const kind = $('knowledgeKind').value;
  const content = $('knowledgeContent').value.trim();
  const title = $('knowledgeTitle').value.trim();
  if (!content) { $('settingsStatus').textContent = 'Write the memory or skill text first.'; return; }
  $('addKnowledgeBtn').disabled = true;
  try {
    const data = await api('/api/knowledge', { method: 'POST', body: JSON.stringify({ kind, content, title }) });
    $('settingsStatus').textContent = data.duplicate ? 'That entry already exists; nothing was duplicated.' : `Appended safely to ${kind === 'memory' ? 'MEMORY.MD' : 'SKILLS.MD'}.`;
    $('knowledgeContent').value = '';
    $('knowledgeTitle').value = '';
    await refreshKnowledge();
  } catch (err) { $('settingsStatus').textContent = err.message; }
  finally { $('addKnowledgeBtn').disabled = false; }
}

function showActivityModal() {
  $('modalBody').innerHTML = state.activities.map((a) => `
    <div class="activity-card open modal-activity-card">
      <button class="activity-summary"><div class="activity-icon ${statusClass(a.status)}">${iconFor(a)}</div><div class="activity-main"><div class="activity-desc">${escapeHtml(a.description)}</div><div class="activity-time">${escapeHtml(a.tool || a.type || 'agent')} · ${formatDuration(a.duration)}</div></div><div class="activity-status ${statusClass(a.status)}">${escapeHtml(a.status)}</div></button>
      <div class="activity-details always-open"><div class="detail-grid">
        <div class="detail-label">Type</div><div class="detail-value">${escapeHtml(a.type)}</div>
        <div class="detail-label">Tool</div><div class="detail-value">${escapeHtml(a.tool || 'Agent')}</div>
        <div class="detail-label">Started</div><div class="detail-value">${new Date(Number(a.started_at) * 1000).toLocaleString()}</div>
        <div class="detail-label">Duration</div><div class="detail-value">${formatDuration(a.duration)}</div>
        ${a.output?.process_id ? `<div class="detail-label">Process</div><div class="detail-value">${escapeHtml(a.output.process_id)} · PID ${escapeHtml(a.output.pid)} · ${escapeHtml(a.output.status || '')}</div>` : ''}
        <div class="detail-label">Input</div><div class="detail-value copyable-pre"><pre>${escapeHtml(pretty(a.input))}</pre></div>
        <div class="detail-label">Output</div><div class="detail-value copyable-pre"><pre>${escapeHtml(pretty(a.output))}</pre></div>
        ${a.error ? `<div class="detail-label">Error</div><div class="detail-value error-text">${escapeHtml(a.error)}</div>` : ''}
      </div></div>
    </div>`).join('') || '<div class="activity-empty">No activity.</div>';
  $('activityModal').classList.remove('hidden');
}

function copyAllActivity() {
  const text = `Activity Details\nFull execution timeline\n\n` + state.activities.map(a => {
    const lines = [
      `${a.status || ''} ${a.description || ''}`,
      `Tool: ${a.tool || a.type || 'agent'}`,
      `Started: ${a.started_at ? new Date(Number(a.started_at) * 1000).toLocaleString() : ''}`,
      `Duration: ${formatDuration(a.duration)}`,
      a.output?.process_id ? `Process: ${a.output.process_id} PID ${a.output.pid} Status ${a.output.status || ''}` : '',
      a.output?.output ? `Logs:
${a.output.output}` : '',
      a.error ? `Error: ${a.error}` : '',
      `Input:
${pretty(a.input)}`,
      `Output:
${pretty(a.output)}`
    ];
    return lines.filter(Boolean).join('\n');
  }).join('\n\n────────────────────────\n\n');
  copyText(text, $('copyActivityBtn'));
}



const MODEL_CATALOG = [
  {
    id: 'z-ai/glm-5.3-flash', name: 'GLM-5.3-Flash', provider: 'NVIDIA NIM', icon: 'G', family: 'glm',
    desc: 'Fast agentic model with always-on reasoning. Best default for tool use and coding.',
    badges: [['Reasoning', true], ['Tool use', true], ['Fast', false]], reasoning: true,
  },
  {
    id: 'gemini-3.5-flash-lite', name: 'Gemini 3.5 Flash Lite', provider: 'Google', icon: 'G', family: 'gemini',
    desc: 'Lightweight and very quick. Good for simple tasks, search and quick edits.',
    badges: [['Lightning fast', true], ['Tool use', true], ['Vision', false]], reasoning: false,
  },
];
const EFFORT_INFO = {
  low: 'Quick answers. Less deliberation, lowest latency.',
  high: 'Balanced depth and speed. Recommended for most tasks.',
  max: 'Deepest reasoning. Slower, best for hard debugging and planning.',
};

function modelMeta(id) { return MODEL_CATALOG.find(m => m.id === id) || MODEL_CATALOG[0]; }

function renderModelPicker() {
  const meta = modelMeta(state.selectedModel);
  $('modelPickerLabel').textContent = meta.name;
  $('modelPickerDot').classList.toggle('gemini', meta.family === 'gemini');
  const effortEl = $('modelPickerEffort');
  effortEl.classList.toggle('hidden', !meta.reasoning);
  effortEl.textContent = ({ low: 'Low', high: 'High', max: 'Max' })[state.reasoningEffort] || 'High';
  $('modelMenuItems').innerHTML = MODEL_CATALOG.map(m => `<button type="button" class="mm-item ${m.id === state.selectedModel ? 'selected' : ''}" data-model="${escapeHtml(m.id)}" role="option" aria-selected="${m.id === state.selectedModel}">
    <span class="mm-ico ${m.family === 'gemini' ? 'gemini' : ''}">${escapeHtml(m.icon)}</span>
    <span class="mm-body"><span class="mm-name">${escapeHtml(m.name)}<span class="mm-badge">${escapeHtml(m.provider)}</span></span><span class="mm-desc">${escapeHtml(m.desc)}</span><span class="mm-badges">${m.badges.map(([t, good]) => `<span class="mm-badge ${good ? 'good' : ''}">${escapeHtml(t)}</span>`).join('')}</span></span>
    <svg class="mm-check" viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg></button>`).join('');
  $('mmReasoning').classList.toggle('hidden', !meta.reasoning);
  $('mmNoReasoning').classList.toggle('hidden', meta.reasoning);
  document.querySelectorAll('.mm-seg-btn').forEach(btn => {
    const active = btn.dataset.effort === state.reasoningEffort;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-checked', String(active));
  });
  $('mmEffortHint').textContent = EFFORT_INFO[state.reasoningEffort] || '';
  $('chatModelSelect').value = state.selectedModel;
  $('reasoningEffort').value = state.reasoningEffort;
}

function setModelMenu(open) {
  const menu = $('modelMenu');
  menu.classList.toggle('hidden', !open);
  $('modelPickerBtn').setAttribute('aria-expanded', String(open));
  if (open) setTimeout(() => (menu.querySelector('.mm-item.selected') || menu.querySelector('.mm-item'))?.focus(), 0);
}

function initModelControls() {
  if (!MODEL_CATALOG.some(m => m.id === state.selectedModel)) state.selectedModel = MODEL_CATALOG[0].id;
  if (!['low', 'high', 'max'].includes(state.reasoningEffort)) state.reasoningEffort = 'high';
  renderModelPicker();
  $('modelPickerBtn').addEventListener('click', (e) => { e.stopPropagation(); setModelMenu($('modelMenu').classList.contains('hidden')); });
  $('modelMenu').addEventListener('click', (e) => {
    e.stopPropagation();
    const item = e.target.closest('.mm-item');
    if (item) {
      state.selectedModel = item.dataset.model;
      localStorage.setItem('intigno-chat-model', state.selectedModel);
      renderModelPicker();
      if (!modelMeta(state.selectedModel).reasoning) setModelMenu(false);
      return;
    }
    const seg = e.target.closest('.mm-seg-btn');
    if (seg) {
      state.reasoningEffort = seg.dataset.effort;
      localStorage.setItem('intigno-reasoning-effort', state.reasoningEffort);
      renderModelPicker();
    }
  });
  $('modelMenu').addEventListener('keydown', (e) => {
    const items = Array.from($('modelMenu').querySelectorAll('.mm-item'));
    const i = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length]?.focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length]?.focus(); }
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('#modelPicker')) setModelMenu(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('modelMenu').classList.contains('hidden')) { setModelMenu(false); $('modelPickerBtn').focus(); } });
}
initModelControls();

$('messageList').addEventListener('click', (event) => {
  const groupBtn = event.target.closest('[data-group]');
  if (groupBtn) {
    const key = groupBtn.dataset.group;
    const isOpen = groupBtn.parentElement.classList.contains('is-open');
    if (isOpen) { ui.openGroups.delete(key); ui.closedGroups.add(key); }
    else { ui.closedGroups.delete(key); ui.openGroups.add(key); }
    renderMessages();
    return;
  }
  const stepBtn = event.target.closest('[data-step]');
  if (stepBtn) {
    const id = stepBtn.dataset.step;
    if (ui.openSteps.has(id)) ui.openSteps.delete(id); else ui.openSteps.add(id);
    renderMessages();
    return;
  }
  if (event.target.closest('[data-open-browser]')) focusBrowserPanel();
});

$('sendBtn').onclick = async () => {
  const draft = !!$('messageInput').value.trim() || state.attachments.length > 0;
  if (state.busy && !draft && !state.pending) {
    try { await api('/api/cancel', { method: 'POST', body: JSON.stringify({ session_id: state.sessionId }) }); }
    catch (err) { addLocalError(err.message); }
    return;
  }
  await sendMessage();
};
$('messageInput').addEventListener('input', () => { autoGrow(); updateComposer(); });
$('messageInput').addEventListener('paste', (event) => {
  const items = Array.from(event.clipboardData?.items || []);
  const images = items.filter(item => item.kind === 'file' && /^image\//i.test(item.type)).map(item => item.getAsFile()).filter(Boolean);
  if (!images.length) return;
  event.preventDefault();
  const named = images.map((file, i) => {
    try { Object.defineProperty(file, 'name', { value: file.name && file.name !== 'image.png' ? file.name : `clipboard-image-${Date.now()}-${i + 1}.png` }); } catch {}
    return file;
  });
  uploadFiles(named);
});

$('messageInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
$('newChatBtn').onclick = async () => { await createSession(); if (window.matchMedia('(max-width: 920px)').matches) toggleSidebar(false); };
$('settingsBtn').onclick = openSettings;
$('closeSettingsBtn').onclick = () => $('settingsModal').classList.add('hidden');
$('settingsCancelBtn').onclick = () => $('settingsModal').classList.add('hidden');
$('saveWorkspaceBtn').onclick = saveWorkspace;
$('attachBtn').onclick = () => $('fileInput').click();
$('fileInput').addEventListener('change', event => uploadFiles(event.target.files));
$('attachmentTray').addEventListener('click', event => {
  const button = event.target.closest('[data-remove-attachment]');
  if (!button) return;
  state.attachments = state.attachments.filter(item => item.id !== button.dataset.removeAttachment);
  renderAttachmentTray();
});
$('resumeBtn').onclick = async () => {
  $('resumeBtn').disabled = true;
  try { await api('/api/resume', { method: 'POST', body: JSON.stringify({ session_id: state.sessionId }) }); }
  catch (err) { addLocalError(err.message); }
  finally { $('resumeBtn').disabled = false; }
};
$('discardRecoveryBtn').onclick = async () => {
  try {
    await api('/api/recovery', { method: 'DELETE', body: JSON.stringify({ session_id: state.sessionId }) });
    state.recoveryAvailable = false; state.recoveryPhase = null; renderAll();
  } catch (err) { addLocalError(err.message); }
};
$('refreshKnowledgeBtn').onclick = refreshKnowledge;
$('addKnowledgeBtn').onclick = addKnowledgeEntry;
$('maximizeBtn').onclick = showActivityModal;
$('copyActivityBtn').onclick = copyAllActivity;
$('closeModalBtn').onclick = () => $('activityModal').classList.add('hidden');

// --- v1.4 sidebar wiring -----------------------------------------------
$('sidebarToggleBtn').onclick = () => toggleSidebar();
$('activityToggleBtn').onclick = () => document.body.classList.toggle('activity-open');


$('chatSearchInput').addEventListener('input', (e) => {
  state.chatSearch = e.target.value;
  renderChatList();
});

$('chatList').addEventListener('click', (event) => {
  const menuBtn = event.target.closest('.chat-item-menu-btn');
  if (menuBtn) {
    event.stopPropagation();
    const id = menuBtn.dataset.menuFor;
    state.openMenuChatId = state.openMenuChatId === id ? null : id;
    renderChatList();
    return;
  }
  const actionBtn = event.target.closest('[data-action]');
  if (actionBtn) {
    event.stopPropagation();
    const action = actionBtn.dataset.action;
    const chatId = actionBtn.dataset.chatId;
    state.openMenuChatId = null;
    if (action === 'pin') togglePin(chatId);
    else if (action === 'rename') renameChatPrompt(chatId);
    else if (action === 'delete') deleteChatPrompt(chatId);
    renderChatList();
    return;
  }
  const item = event.target.closest('.chat-item');
  if (item) {
    state.openMenuChatId = null;
    openChat(item.dataset.chatId);
    // On mobile, opening a chat should close the drawer.
    if (window.matchMedia('(max-width: 920px)').matches) toggleSidebar(false);
  }
});

document.addEventListener('click', (event) => {
  // Close any open chat-item menu when clicking elsewhere.
  if (!event.target.closest('.chat-item-menu') && !event.target.closest('.chat-item-menu-btn') && state.openMenuChatId) {
    state.openMenuChatId = null;
    renderChatList();
  }
});

$('renameCancelBtn').onclick = () => $('renameModal').classList.add('hidden');
$('closeRenameBtn').onclick = () => $('renameModal').classList.add('hidden');
$('renameSaveBtn').onclick = saveRename;
$('renameInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); saveRename(); } });

$('deleteChatCancelBtn').onclick = () => $('deleteChatModal').classList.add('hidden');
$('closeDeleteChatBtn').onclick = () => $('deleteChatModal').classList.add('hidden');
$('deleteChatConfirmBtn').onclick = confirmDeleteChat;

document.addEventListener('click', (event) => {
  const button = event.target.closest('.copy-btn');
  // Copy-all already builds a complete timeline from state; don't let this generic
  // delegated handler run afterward and overwrite the clipboard with an empty string.
  if (button && button.id !== 'copyActivityBtn' && Object.prototype.hasOwnProperty.call(button.dataset, 'copy')) {
    copyText(button.dataset.copy || '', button);
  }
  if (event.target === $('activityModal')) $('activityModal').classList.add('hidden');
  if (event.target === $('settingsModal')) $('settingsModal').classList.add('hidden');
  if (event.target === $('renameModal')) $('renameModal').classList.add('hidden');
  if (event.target === $('deleteChatModal')) $('deleteChatModal').classList.add('hidden');
});

setInterval(() => {
  document.querySelectorAll('.live-duration').forEach(el => {
    const started = Number(el.dataset.started || 0);
    if (started && el.closest('.activity-card')?.querySelector('.status-running')) {
      el.textContent = formatDuration(Date.now() / 1000 - started);
    }
  });
}, 1000);

// --- v1.9 embedded browser stream ---------------------------------------
state.browserStream = { tab: "browser", feed: null, closingFeed: false, starting: false, autoStarting: false, lastAutoActivity: null, status: { state: "idle", capturing: false, fps: 0, tab: null } };

function setBrowserTabActive(tab) {
  state.browserStream.tab = tab;
  const browser = tab === "browser";
  $("activityScroll").classList.toggle("hidden", browser);
  $("browserPanel").classList.toggle("hidden", !browser);
  $("activityMeta").classList.toggle("hidden", browser);
  $("browserMeta").classList.toggle("hidden", !browser);
  $("maximizeBtn").classList.toggle("hidden", browser);
  $("panelTabActivity").classList.toggle("active", !browser);
  $("panelTabBrowser").classList.toggle("active", browser);
  if (browser) connectBrowserFeed(); else closeBrowserFeed();
}

function connectBrowserFeed() {
  const bs = state.browserStream;
  // The manager returns state="starting" immediately; subscribe now so the first
  // status/frame events arrive when capture actually becomes active.
  if (bs.feed || !(bs.status.capturing || bs.status.state === "starting")) return;
  const source = new EventSource("/api/browser/stream/feed");
  bs.feed = source;
  source.addEventListener("status", (event) => {
    try { bs.status = JSON.parse(event.data); renderBrowserStream(); } catch {}
  });
  source.addEventListener("frame", (event) => {
    try {
      const frame = JSON.parse(event.data);
      state.browserStream.lastMeta = frame.metadata || {};
      const img = $("browserStreamImg");
      img.src = `data:image/jpeg;base64,${frame.image}`;
      img.style.display = "block";
      $("browserStreamEmpty").classList.add("hidden");
      $("browserStreamError").classList.add("hidden");
    } catch {}
  });
  source.addEventListener("ended", (event) => {
    closeBrowserFeed();
    try { bs.status = JSON.parse(event.data); } catch { bs.status = { state: "stopped", capturing: false, fps: 0, tab: null }; }
    renderBrowserStream();
    $("browserStreamImg").style.display = "none";
    $("browserStreamEmpty").classList.remove("hidden");
    if (bs.status.error) {
      $("browserStreamError").textContent = bs.status.error;
      $("browserStreamError").classList.remove("hidden");
    }
  });
  source.onerror = () => {
    // EventSource retries automatically; only report when we did not close on purpose.
    if (state.browserStream.feed && !state.browserStream.closingFeed) {
      $("browserStreamState").textContent = "Reconnecting…";
    }
  };
}

function closeBrowserFeed() {
  const bs = state.browserStream;
  bs.closingFeed = true;
  if (bs.feed) { bs.feed.close(); bs.feed = null; }
  bs.closingFeed = false;
}

function renderBrowserStream() {
  const bs = state.browserStream;
  const s = bs.status || {};
  const tab = s.tab || {};
  $("browserStreamState").textContent = bs.starting
    ? "Starting browser capture…"
    : s.capturing
      ? `Live · ${(Number(s.fps) || 0).toFixed(1)} fps${s.viewers ? ` · ${s.viewers} viewer(s)` : ""}`
      : (s.state === "error" ? "Stopped" : "Not streaming");
  $("browserStreamBtn").textContent = bs.starting ? "Starting…" : (s.capturing ? "Stop stream" : "Start stream");
  $("browserStreamBtn").disabled = bs.starting;

  $("browserNewTabBtn").classList.toggle("hidden", !s.capturing);
  const urlInput = $("browserUrlInput");
  if (tab.url && document.activeElement !== urlInput) urlInput.value = tab.url;
  $("browserStreamUrl").textContent = tab.url || "";
  $("browserMeta").textContent = tab.title || tab.url || "Browser stream";
}

async function autoShowEmbeddedBrowser(activity) {
  const bs = state.browserStream;
  if (!activity || bs.lastAutoActivity === activity.id) return;
  bs.lastAutoActivity = activity.id;
  setBrowserTabActive("browser");
  if (bs.status.capturing || bs.starting || bs.autoStarting) return;
  bs.autoStarting = true;
  bs.starting = true;
  renderBrowserStream();
  let lastError = "Could not start embedded browser.";
  try {
    // Browser activity is announced before the tool finishes launching Chrome. Retry
    // briefly if the stream endpoint races the first browser action instead of failing
    // permanently for this activity ID.
    for (let attempt = 0; attempt < 12; attempt++) {
      try {
        const result = await api("/api/browser/stream/start", { method: "POST", body: JSON.stringify({}) });
        bs.status = result;
        if (result.ok !== false) {
          $("browserStreamError").classList.add("hidden");
          $("browserStreamEmpty").classList.add("hidden");
          connectBrowserFeed();
          return;
        }
        lastError = result.error || lastError;
        if (!/currently running a browser task|not yet ready|endpoint.*ready/i.test(lastError)) break;
      } catch (err) {
        lastError = err.message || lastError;
        if (attempt >= 11) break;
      }
      await new Promise(resolve => setTimeout(resolve, Math.min(1100, 300 + attempt * 100)));
    }
    $("browserStreamError").textContent = lastError;
    $("browserStreamError").classList.remove("hidden");
  } finally {
    bs.starting = false;
    bs.autoStarting = false;
    renderBrowserStream();
  }
}

async function refreshBrowserStreamStatus() {
  try {
    const data = await api("/api/browser/stream/status");
    state.browserStream.status = data;
    if (data.capturing) setBrowserTabActive("browser");
    renderBrowserStream();
  } catch {}
}

function setBrowserExpanded(on) {
  document.body.classList.toggle("browser-expanded", on);
  const btn = $("browserExpandBtn");
  if (btn) { btn.title = on ? "Collapse browser view" : "Expand browser view"; btn.setAttribute("aria-label", btn.title); }
}
$("browserExpandBtn").onclick = () => setBrowserExpanded(!document.body.classList.contains("browser-expanded"));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && document.body.classList.contains("browser-expanded")) setBrowserExpanded(false); });
document.addEventListener("click", (e) => { if (document.body.classList.contains("browser-expanded") && e.target === document.body) setBrowserExpanded(false); });
$("panelTabActivity").onclick = () => setBrowserTabActive("activity");
$("panelTabBrowser").onclick = () => setBrowserTabActive("browser");
$("browserStreamBtn").onclick = async () => {
  const bs = state.browserStream;
  if (bs.status.capturing) {
    closeBrowserFeed();
    try { bs.status = await api("/api/browser/stream/stop", { method: "POST", body: "{}" }); }
    catch (err) { addLocalError(err.message); }
    bs.status = { state: "stopped", capturing: false, fps: 0, tab: bs.status.tab };
    renderBrowserStream();
    $("browserStreamImg").style.display = "none";
    $("browserStreamEmpty").classList.remove("hidden");
    return;
  }
  bs.starting = true;
  $("browserStreamError").classList.add("hidden");
  renderBrowserStream();
  try {
    bs.status = await api("/api/browser/stream/start", { method: "POST", body: "{}" });
    if (bs.status.ok === false) {
      $("browserStreamError").textContent = bs.status.error || "Could not start the browser stream.";
      $("browserStreamError").classList.remove("hidden");
    } else if (bs.status.capturing || bs.status.state === "starting" || bs.status.state === "capturing") {
      $("browserStreamEmpty").classList.add("hidden");
      if (bs.tab === "browser") connectBrowserFeed();
    }
  } catch (err) {
    $("browserStreamError").textContent = err.message;
    $("browserStreamError").classList.remove("hidden");
  } finally {
    bs.starting = false;
    renderBrowserStream();
  }
};


// --- v1.9: manual interaction with the mirrored page ------------------------------------

function browserViewportScale() {
  const img = $("browserStreamImg");
  const meta = state.browserStream.lastMeta || {};
  const deviceWidth = Number(meta.deviceWidth) || img.naturalWidth || 1280;
  const shown = img.clientWidth || deviceWidth;
  return deviceWidth / Math.max(1, shown);
}

function browserPoint(event) {
  const img = $("browserStreamImg");
  const rect = img.getBoundingClientRect();
  const scale = browserViewportScale();
  return { x: Math.round((event.clientX - rect.left) * scale), y: Math.round((event.clientY - rect.top) * scale) };
}

async function browserInput(payload) {
  try {
    return await api("/api/browser/input", { method: "POST", body: JSON.stringify(payload) });
  } catch (err) { addLocalError(err.message); return null; }
}

(function wireBrowserInteraction() {
  const img = $("browserStreamImg");
  if (!img) return;
  // Visible black cursor follows the embedded browser view, not the OS pointer.
  // Browser coordinates are converted using the existing browserPoint() helper.
  const pointer = $("browserCursor");
  img.addEventListener("pointermove", event => {
    const rect = img.getBoundingClientRect();
    const host = img.parentElement.getBoundingClientRect();
    pointer.style.left = `${event.clientX - host.left}px`;
    pointer.style.top = `${event.clientY - host.top}px`;
    pointer.style.display = "block";
  });
  img.addEventListener("pointerleave", () => { pointer.style.display = "none"; });
  img.addEventListener("click", async (event) => {
    const point = browserPoint(event);
    await browserInput({ type: "click", x: point.x, y: point.y, button: "left" });
    img.focus({preventScroll:true});
  });
  img.addEventListener("dblclick", async (event) => {
    const point = browserPoint(event);
    await browserInput({ type: "dblclick", x: point.x, y: point.y });
  });
  img.addEventListener("wheel", async (event) => {
    event.preventDefault();
    const point = browserPoint(event);
    await browserInput({ type: "wheel", x: point.x, y: point.y, deltaX: event.deltaX, deltaY: event.deltaY });
  }, { passive: false });

  async function browserNavigate(value) {
    const text = (value || "").trim();
    if (!text) return;
    const target = /^https?:\/\//i.test(text) ? text : `https://${text}`;
    await browserInput({ type: "navigate", url: target });
  }
  $("browserGoBtn").onclick = () => browserNavigate($("browserUrlInput").value);
  $("browserUrlInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") browserNavigate($("browserUrlInput").value);
  });
  $("browserNewTabBtn").onclick = () => browserInput({ type: "new_tab", url: "about:blank" });
  $("browserEnterBtn").onclick = () => browserInput({ type: "press", key: "Enter", code: "Enter", keyCode: 13 });
  img.addEventListener('keydown', async (event) => {
    if (event.key === 'Escape') { $('messageInput').focus(); return; }
    if (event.metaKey || event.ctrlKey || event.altKey || event.key === 'Tab') return;
    event.preventDefault();
    const special = {Enter:13, Backspace:8, Delete:46, ArrowLeft:37, ArrowRight:39, ArrowUp:38, ArrowDown:40};
    if (event.key.length === 1) await browserInput({type:'type', text:event.key});
    else if (Object.hasOwn(special, event.key)) await browserInput({type:'press', key:event.key, code:event.code || event.key, keyCode:special[event.key]});
  });
})();

function initPaneResizers() {
  const grid = document.querySelector('.main-grid');
  const sidebarHandle = $('sidebarResizer');
  const activityHandle = $('activityResizer');
  if (!grid || !sidebarHandle || !activityHandle) return;
  let sidebarWidth = Number(localStorage.getItem('intigno-sidebar-width')) || 250;
  let activityWidth = Number(localStorage.getItem('intigno-activity-width')) || 480;
  const desktop = () => window.innerWidth > 920;
  const applyWidths = () => {
    if (!desktop()) { grid.style.gridTemplateColumns = ''; return; }
    const sidebar = grid.classList.contains('sidebar-collapsed') ? 0 : sidebarWidth;
    grid.style.gridTemplateColumns = `${sidebar}px minmax(320px, 1fr) ${activityWidth}px`;
    grid.style.setProperty('--sidebar-resizer-left', `${sidebar}px`);
    grid.style.setProperty('--activity-resizer-right', `${activityWidth}px`);
  };
  const begin = (event, which) => {
    if (!desktop()) return;
    event.preventDefault();
    const startX = event.clientX;
    const startSidebar = sidebarWidth;
    const startActivity = activityWidth;
    const onMove = (moveEvent) => {
      const dx = moveEvent.clientX - startX;
      if (which === 'sidebar') {
        sidebarWidth = Math.max(190, Math.min(390, startSidebar + dx));
        localStorage.setItem('intigno-sidebar-width', String(Math.round(sidebarWidth)));
      } else {
        activityWidth = Math.max(270, Math.min(560, startActivity - dx));
        localStorage.setItem('intigno-activity-width', String(Math.round(activityWidth)));
      }
      applyWidths();
    };
    const end = () => {
      document.body.classList.remove('resizing-panes');
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', end);
    };
    document.body.classList.add('resizing-panes');
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', end, { once: true });
  };
  sidebarHandle.addEventListener('pointerdown', event => begin(event, 'sidebar'));
  activityHandle.addEventListener('pointerdown', event => begin(event, 'activity'));
  window.addEventListener('resize', applyWidths);
  applyWidths();
}

(async function boot() {
  initPaneResizers();
  refreshBrowserStreamStatus();
  setBrowserTabActive("browser");
  await refreshStatus();
  toggleSidebar(!window.matchMedia('(max-width: 920px)').matches);
  await createSession();
  document.querySelectorAll('[data-example]').forEach(btn => { btn.onclick = () => sendMessage(btn.dataset.example); });
  setInterval(refreshStatus, 5000);
  setInterval(() => { if (!state.busy) refreshChatList(); }, 8000);
})();
