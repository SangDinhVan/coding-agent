/* ============================================================
   Nova — AI Chatbot Interface
   Vanilla JS: messaging, simulated AI replies, history (localStorage),
   theme toggle, mobile sidebar, history search.
   ============================================================ */

(() => {
  "use strict";

  /* ---------- Elements ---------- */
  const els = {
    messages: document.getElementById("messages"),
    input: document.getElementById("input"),
    sendBtn: document.getElementById("sendBtn"),
    newChatBtn: document.getElementById("newChatBtn"),
    historyList: document.getElementById("historyList"),
    historySearch: document.getElementById("historySearch"),
    themeToggle: document.getElementById("themeToggle"),
    menuBtn: document.getElementById("menuBtn"),
    scrim: document.getElementById("scrim"),
    emptyState: document.getElementById("emptyState"),
    conversationTitle: document.getElementById("conversationTitle"),
    attachBtn: document.getElementById("attachBtn"),
  };

  /* ---------- State ---------- */
  const STORAGE_KEY = "nova.conversations.v1";
  const THEME_KEY = "nova.theme";

  let conversations = loadConversations();
  let activeId = null;
  let isGenerating = false;
  let typingTimer = null;

  /* ---------- Sample AI responses ---------- */
  const SAMPLES = [
    "That's a great question! Here's how I'd approach it:\n\n1. **Start with the goal** — clarify what a successful outcome looks like.\n2. **Break it into small steps** — manageable pieces are easier to act on.\n3. **Measure as you go** — review progress and adjust early.\n\nWould you like me to go deeper on any of these steps?",
    "Here's a concise summary:\n\n- The core idea is straightforward and low-risk.\n- The main trade-off is time vs. polish.\n- I'd recommend shipping a minimal version first, then iterating based on feedback.\n\nWant me to draft a plan you can follow?",
    "I can help with that. A few things worth considering:\n\n• Clarify the audience and context first\n• Keep the structure simple — three to five key points\n• End with a clear next action\n\nTell me more about the specifics and I'll tailor the response.",
  ];

  /* ---------- Utilities ---------- */
  function $q(selector, root = document) {
    return root.querySelector(selector);
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function markdownLite(text) {
    let html = escapeHtml(text);

    // Inline code
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");

    // Bold
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

    // Unordered list lines: "- item" → <li>
    const listPattern = /(?:^|\n)((?:[-•]\s+.+(?:\n|$))+)/g;
    html = html.replace(listPattern, (_, block) => {
      const items = block
        .trim()
        .split("\n")
        .map((line) => line.replace(/^[-•]\s+/, "").trim())
        .filter(Boolean)
        .map((item) => `<li>${item}</li>`)
        .join("");
      return `\n<ul>${items}</ul>\n`;
    });

    // Numbered list lines: "1. item" → <li>
    const olPattern = /(?:^|\n)((?:\d+[.)]\s+.+(?:\n|$))+)/g;
    html = html.replace(olPattern, (_, block) => {
      const items = block
        .trim()
        .split("\n")
        .map((line) => line.replace(/^\d+[.)]\s+/, "").trim())
        .filter(Boolean)
        .map((item) => `<li>${item}</li>`)
        .join("");
      return `\n<ol>${items}</ol>\n`;
    });

    // Paragraphs
    const blocks = html.split(/\n{2,}/);
    html = blocks
      .map((block) => {
        const trimmed = block.trim();
        if (!trimmed) return "";
        if (/^<(ul|ol)>/.test(trimmed)) return trimmed;
        return `<p>${trimmed.replace(/\n/g, "<br>")}</p>`;
      })
      .join("");

    return html;
  }

  function nowTime() {
    return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  /* ---------- Storage ---------- */
  function loadConversations() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch {
      return [];
    }
  }

  function saveConversations() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
    } catch {
      /* storage unavailable — ignore */
    }
  }

  /* ---------- History rendering ---------- */
  function conversationTitleOf(conv) {
    if (conv.title) return conv.title;
    const firstUser = conv.messages.find((m) => m.role === "user");
    return firstUser ? firstUser.content.slice(0, 40) : "New chat";
  }

  function renderHistory() {
    const query = els.historySearch.value.trim().toLowerCase();

    const filtered = conversations.filter((c) =>
      conversationTitleOf(c).toLowerCase().includes(query)
    );

    els.historyList.innerHTML = "";

    if (filtered.length === 0) {
      const empty = document.createElement("div");
      empty.className = "history-empty";
      empty.textContent = query ? "No matching conversations" : "No conversations yet";
      els.historyList.appendChild(empty);
      return;
    }

    // Group: Today / Previous 7 days / Older
    const groups = { today: [], week: [], older: [] };
    const now = Date.now();
    const day = 24 * 60 * 60 * 1000;

    filtered.forEach((c) => {
      const age = now - c.updatedAt;
      if (age < day) groups.today.push(c);
      else if (age < 7 * day) groups.week.push(c);
      else groups.older.push(c);
    });

    const labels = [
      ["today", "Today"],
      ["week", "Previous 7 days"],
      ["older", "Older"],
    ];

    labels.forEach(([key, label]) => {
      const items = groups[key];
      if (!items.length) return;

      const section = document.createElement("div");
      section.className = "history-section";

      const title = document.createElement("div");
      title.className = "history-section-title";
      title.textContent = label;
      section.appendChild(title);

      items.forEach((conv) => {
        section.appendChild(historyItem(conv));
      });

      els.historyList.appendChild(section);
    });
  }

  function historyItem(conv) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "history-item" + (conv.id === activeId ? " active" : "");
    btn.dataset.id = conv.id;
    btn.setAttribute("aria-label", `Open conversation: ${conversationTitleOf(conv)}`);

    btn.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
      <span class="history-label"></span>
      <span class="delete-btn" role="button" tabindex="0" aria-label="Delete conversation">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
          <path d="M18 6L6 18M6 6l12 12"/>
        </svg>
      </span>
    `;

    $q(".history-label", btn).textContent = conversationTitleOf(conv);

    btn.addEventListener("click", (e) => {
      if (e.target.closest(".delete-btn")) return;
      openConversation(conv.id);
    });

    const del = $q(".delete-btn", btn);
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteConversation(conv.id);
    });
    del.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        e.stopPropagation();
        deleteConversation(conv.id);
      }
    });

    return btn;
  }

  /* ---------- Conversation management ---------- */
  function createConversation() {
    const conv = {
      id: "c" + Date.now(),
      messages: [],
      createdAt: Date.now(),
      updatedAt: Date.now(),
      title: null,
    };
    conversations.unshift(conv);
    saveConversations();
    return conv;
  }

  function openConversation(id) {
    activeId = id;
    const conv = conversations.find((c) => c.id === id);
    if (!conv) return;

    renderMessages(conv);
    renderHistory();
    updateTitle();

    // Close mobile sidebar
    document.body.classList.remove("sidebar-open");
  }

  function deleteConversation(id) {
    const idx = conversations.findIndex((c) => c.id === id);
    if (idx === -1) return;
    conversations.splice(idx, 1);

    if (activeId === id) {
      activeId = null;
      showEmptyState();
      updateTitle();
    }
    saveConversations();
    renderHistory();
  }

  function updateTitle() {
    const conv = conversations.find((c) => c.id === activeId);
    els.conversationTitle.textContent = conv ? conversationTitleOf(conv) : "New chat";
  }

  /* ---------- Message rendering ---------- */
  function showEmptyState() {
    els.messages.innerHTML = "";
    const clone = els.emptyState.cloneNode(true);
    clone.style.display = "";
    clone.id = "emptyState";
    els.messages.appendChild(clone);

    // Re-bind suggestion buttons
    clone.querySelectorAll(".suggestion").forEach((s) => {
      s.addEventListener("click", () => {
        sendMessage(s.dataset.prompt);
      });
    });
  }

  function renderMessages(conv) {
    els.messages.innerHTML = "";

    if (!conv.messages.length) {
      showEmptyState();
      return;
    }

    const inner = document.createElement("div");
    inner.className = "messages-inner";
    conv.messages.forEach((m) => inner.appendChild(messageRow(m)));
    els.messages.appendChild(inner);
    scrollToBottom(false);
  }

  function messageRow(msg) {
    const row = document.createElement("div");
    row.className = "msg";

    const isUser = msg.role === "user";

    const avatar = document.createElement("div");
    avatar.className = "msg-avatar " + (isUser ? "user" : "ai");
    avatar.setAttribute("aria-hidden", "true");
    avatar.innerHTML = isUser
      ? "YOU"
      : `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l2.1 7.9L22 12l-7.9 2.1L12 22l-2.1-7.9L2 12l7.9-2.1z"/></svg>`;

    const body = document.createElement("div");
    body.className = "msg-body";

    const meta = document.createElement("div");
    meta.className = "msg-meta";
    const author = document.createElement("span");
    author.className = "msg-author";
    author.textContent = isUser ? "You" : "Nova";
    const time = document.createElement("span");
    time.className = "msg-time";
    time.textContent = msg.time || nowTime();
    meta.appendChild(author);
    meta.appendChild(time);

    const content = document.createElement("div");
    content.className = "msg-content " + (isUser ? "user" : "ai");
    content.innerHTML = isUser ? escapeHtml(msg.content) : markdownLite(msg.content);

    body.appendChild(meta);
    body.appendChild(content);
    row.appendChild(avatar);
    row.appendChild(body);

    return row;
  }

  function scrollToBottom(smooth = true) {
    requestAnimationFrame(() => {
      els.messages.scrollTo({
        top: els.messages.scrollHeight,
        behavior: smooth ? "smooth" : "auto",
      });
    });
  }

  /* ---------- Typing indicator ---------- */
  function showTypingIndicator() {
    const row = document.createElement("div");
    row.className = "msg";
    row.id = "typingRow";

    row.innerHTML = `
      <div class="msg-avatar ai" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l2.1 7.9L22 12l-7.9 2.1L12 22l-2.1-7.9L2 12l7.9-2.1z"/></svg>
      </div>
      <div class="msg-body">
        <div class="msg-meta"><span class="msg-author">Nova</span></div>
        <div class="msg-content ai"><span class="typing" aria-label="Nova is typing"><i></i><i></i><i></i></span></div>
      </div>
    `;

    els.messages.appendChild(row);
    scrollToBottom();
    return row;
  }

  function removeTypingIndicator() {
    const row = document.getElementById("typingRow");
    if (row) row.remove();
  }

  /* ---------- Sending ---------- */
  function ensureConversation() {
    if (activeId) {
      const conv = conversations.find((c) => c.id === activeId);
      if (conv) return conv;
    }
    const conv = createConversation();
    activeId = conv.id;
    return conv;
  }

  function sendMessage(text) {
    const content = (text ?? els.input.value).trim();
    if (!content || isGenerating) return;

    // Hide empty state
    const empty = document.getElementById("emptyState");
    if (empty) empty.remove();

    const conv = ensureConversation();

    // User message
    const userMsg = { role: "user", content, time: nowTime() };
    conv.messages.push(userMsg);
    conv.updatedAt = Date.now();
    if (!conv.title) conv.title = content.slice(0, 40);
    saveConversations();

    // Ensure inner container exists
    let inner = $q(".messages-inner", els.messages);
    if (!inner) {
      inner = document.createElement("div");
      inner.className = "messages-inner";
      els.messages.appendChild(inner);
    }

    inner.appendChild(messageRow(userMsg));
    scrollToBottom();

    // Clear input & reset
    els.input.value = "";
    autoResize();
    setSendDisabled(true);
    isGenerating = true;

    // Typing indicator
    const typingRow = showTypingIndicator();

    // Simulated AI reply
    const delay = 900 + Math.random() * 700;
    typingTimer = setTimeout(() => {
      const aiContent = SAMPLES[Math.floor(Math.random() * SAMPLES.length)];
      const aiMsg = { role: "assistant", content: aiContent, time: nowTime() };

      removeTypingIndicator();
      conv.messages.push(aiMsg);
      conv.updatedAt = Date.now();
      saveConversations();

      inner.appendChild(messageRow(aiMsg));
      scrollToBottom();

      isGenerating = false;
      setSendDisabled(!els.input.value.trim());
      renderHistory();
      updateTitle();
    }, delay);
  }

  function setSendDisabled(disabled) {
    els.sendBtn.disabled = disabled;
    els.sendBtn.setAttribute("aria-disabled", String(disabled));
  }

  /* ---------- Input handling ---------- */
  function autoResize() {
    els.input.style.height = "auto";
    els.input.style.height = Math.min(els.input.scrollHeight, 160) + "px";
  }

  els.input.addEventListener("input", () => {
    autoResize();
    setSendDisabled(!els.input.value.trim() || isGenerating);
  });

  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!isGenerating && els.input.value.trim()) {
        sendMessage();
      }
    }
  });

  els.sendBtn.addEventListener("click", sendMessage);
  els.newChatBtn.addEventListener("click", () => {
    // Stop any pending generation
    clearTimeout(typingTimer);
    isGenerating = false;
    removeTypingIndicator();

    activeId = null;
    showEmptyState();
    updateTitle();
    renderHistory();
    els.input.focus();
    setSendDisabled(true);
  });

  /* ---------- Suggestions (delegated) ---------- */
  els.messages.addEventListener("click", (e) => {
    const btn = e.target.closest(".suggestion");
    if (btn) sendMessage(btn.dataset.prompt);
  });

  /* ---------- History search ---------- */
  els.historySearch.addEventListener("input", renderHistory);

  /* ---------- Theme ---------- */
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    els.themeToggle.setAttribute(
      "aria-label",
      theme === "dark" ? "Switch to light theme" : "Switch to dark theme"
    );
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch { /* ignore */ }
  }

  els.themeToggle.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme;
    applyTheme(current === "dark" ? "light" : "dark");
  });

  /* ---------- Mobile sidebar ---------- */
  function setSidebar(open) {
    document.body.classList.toggle("sidebar-open", open);
  }

  els.menuBtn.addEventListener("click", () => setSidebar(true));
  els.scrim.addEventListener("click", () => setSidebar(false));

  /* ---------- Attach (demo placeholder) ---------- */
  els.attachBtn.addEventListener("click", () => {
    els.attachBtn.classList.add("attach-shake");
    setTimeout(() => els.attachBtn.classList.remove("attach-shake"), 400);
  });

  /* ---------- Init ---------- */
  (function init() {
    // Theme from storage or system preference
    let theme = "light";
    try {
      theme = localStorage.getItem(THEME_KEY) ||
        (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    } catch { /* ignore */ }
    applyTheme(theme);

    // Seed a welcome conversation on first run
    if (conversations.length === 0) {
      const conv = createConversation();
      conv.title = "Getting started with Nova";
      conv.messages = [
        {
          role: "assistant",
          content: "Hi! I'm **Nova**, your AI assistant. I can help you summarize documents, brainstorm ideas, explain concepts, and plan your day.\n\nTry one of the suggestions below, or just type a message to get started.",
          time: nowTime(),
        },
      ];
      saveConversations();
    }

    // Open the most recent conversation if it has content, else show empty state
    const latest = [...conversations].sort((a, b) => b.updatedAt - a.updatedAt)[0];
    if (latest && latest.messages.length > 0) {
      openConversation(latest.id);
    } else {
      activeId = null;
      showEmptyState();
      renderHistory();
      updateTitle();
    }

    setSendDisabled(true);
    els.input.focus();
  })();
})();
