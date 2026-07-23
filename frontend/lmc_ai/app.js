(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const DB_NAME = "lmc-ai-workspace";
  const DB_VERSION = 1;
  const STORE_NAME = "workspaces";
  const EDITOR_LEASE_TTL_MS = 8000;
  const EDITOR_LEASE_POLL_MS = 2500;
  const TAB_ID = newId("tab");
  let bootstrap = null;
  let database = null;
  let workspace = null;
  let workspaceIdentity = "";
  let workspaceEditable = false;
  let editorLeaseGeneration = 0;
  let editorLeaseTimer = null;
  let editorLeaseAttempting = false;
  let abortController = null;
  let provisional = [];
  let provisionalBase = null;
  let backendChanged = false;
  let pollGeneration = 0;
  let conversationGeneration = 0;
  let pendingPrompt = "";
  let saveChain = Promise.resolve();
  let documentSaveTimer = null;
  let toastTimer = null;
  let previewVisible = false;

  function newId(prefix) {
    const token = globalThis.crypto?.randomUUID?.()
      || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return `${prefix}-${token}`;
  }

  function totalCharacters(messages) {
    return messages.reduce((sum, item) => sum + String(item.content || "").length, 0);
  }

  function localKey(identity) {
    return `lmc-ai-chat:v1:${encodeURIComponent(identity)}`;
  }

  function requireDefaultMode(source = bootstrap) {
    const modes = source?.service?.modes;
    const selected = source?.default_mode;
    if (!Array.isArray(modes) || typeof selected !== "string"
        || !modes.some((mode) => mode.id === selected)) {
      throw new Error("自家 AI 回答模式設定無效。");
    }
    return selected;
  }

  function normalizeMode(value) {
    const migrated = {complex: "daily", thinking: "deep"}[value] || value;
    const allowed = bootstrap?.service?.modes?.map((item) => item.id) || [];
    const fallback = requireDefaultMode();
    return allowed.includes(migrated) ? migrated : fallback;
  }

  function fingerprintForMode(mode) {
    const selected = normalizeMode(mode);
    const fingerprints = bootstrap?.backend_fingerprints;
    if (fingerprints && typeof fingerprints[selected] === "string") {
      return fingerprints[selected];
    }
    return selected === requireDefaultMode()
      && typeof bootstrap?.backend_fingerprint === "string"
      ? bootstrap.backend_fingerprint
      : "";
  }

  function modeLabel(mode) {
    const selected = normalizeMode(mode);
    return bootstrap?.service?.modes?.find((item) => item.id === selected)?.label
      || selected;
  }

  function makeConversation(mode = requireDefaultMode()) {
    const now = new Date().toISOString();
    return {
      id: newId("chat"),
      title: "新對話",
      mode: normalizeMode(mode),
      fingerprint: "",
      messages: [],
      createdAt: now,
      updatedAt: now,
    };
  }

  function makeDocument(title = "未命名文件", content = "") {
    const now = new Date().toISOString();
    return {
      id: newId("document"),
      title: String(title || "未命名文件").slice(0, 200),
      content: String(content || ""),
      createdAt: now,
      updatedAt: now,
    };
  }

  function activeConversation() {
    return workspace?.conversations?.find(
      (item) => item.id === workspace.activeConversationId,
    ) || null;
  }

  function activeDocument() {
    return workspace?.documents?.find(
      (item) => item.id === workspace.activeDocumentId,
    ) || null;
  }

  function editorLeaseKey(identity) {
    return `__editor_lease__:${identity}`;
  }

  function openDatabase() {
    if (database) return Promise.resolve(database);
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(STORE_NAME)) {
          request.result.createObjectStore(STORE_NAME);
        }
      };
      request.onsuccess = () => {
        database = request.result;
        database.onversionchange = () => database.close();
        resolve(database);
      };
      request.onerror = () => reject(request.error || new Error("無法開啟本機工作區。"));
    });
  }

  async function readStoredWorkspace(identity) {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE_NAME, "readonly");
      const request = transaction.objectStore(STORE_NAME).get(identity);
      request.onsuccess = () => resolve(request.result || null);
      request.onerror = () => reject(request.error || new Error("無法讀取本機工作區。"));
    });
  }

  async function writeStoredWorkspace(identity, snapshot) {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE_NAME, "readwrite");
      const store = transaction.objectStore(STORE_NAME);
      const leaseRequest = store.get(editorLeaseKey(identity));
      let leaseError = null;
      leaseRequest.onsuccess = () => {
        const lease = leaseRequest.result;
        if (!lease || lease.owner !== TAB_ID || Number(lease.expiresAt || 0) <= Date.now()) {
          leaseError = new Error("另一個分頁已取得工作區編輯權。");
          leaseError.code = "workspace_lease_lost";
          transaction.abort();
          return;
        }
        store.put(snapshot, identity);
        store.put(
          {owner: TAB_ID, expiresAt: Date.now() + EDITOR_LEASE_TTL_MS},
          editorLeaseKey(identity),
        );
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(
        leaseError || transaction.error || new Error("無法保存本機工作區。"),
      );
      transaction.onabort = () => reject(
        leaseError || transaction.error || new Error("無法保存本機工作區。"),
      );
    });
  }

  async function acquireOrRenewEditorLease(identity) {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE_NAME, "readwrite");
      const store = transaction.objectStore(STORE_NAME);
      const request = store.get(editorLeaseKey(identity));
      let acquired = false;
      request.onsuccess = () => {
        const now = Date.now();
        const lease = request.result;
        if (!lease || lease.owner === TAB_ID || Number(lease.expiresAt || 0) <= now) {
          acquired = true;
          store.put(
            {owner: TAB_ID, expiresAt: now + EDITOR_LEASE_TTL_MS},
            editorLeaseKey(identity),
          );
        }
      };
      transaction.oncomplete = () => resolve(acquired);
      transaction.onerror = () => reject(
        transaction.error || new Error("無法檢查工作區編輯權。"),
      );
      transaction.onabort = () => reject(
        transaction.error || new Error("無法檢查工作區編輯權。"),
      );
    });
  }

  async function releaseEditorLease(identity) {
    if (!identity) return;
    const db = await openDatabase();
    return new Promise((resolve) => {
      const transaction = db.transaction(STORE_NAME, "readwrite");
      const store = transaction.objectStore(STORE_NAME);
      const request = store.get(editorLeaseKey(identity));
      request.onsuccess = () => {
        if (request.result?.owner === TAB_ID) store.delete(editorLeaseKey(identity));
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => resolve();
      transaction.onabort = () => resolve();
    });
  }

  function validMessages(messages) {
    if (!Array.isArray(messages)) return [];
    const filtered = messages.filter((item) =>
      item && ["user", "assistant"].includes(item.role)
      && typeof item.content === "string",
    );
    if (filtered.length > bootstrap.history_limits.messages
        || totalCharacters(filtered) > bootstrap.history_limits.characters) {
      return [];
    }
    return filtered;
  }

  function normalizeWorkspace(value) {
    const conversations = Array.isArray(value?.conversations)
      ? value.conversations.map((item) => ({
        id: typeof item?.id === "string" ? item.id : newId("chat"),
        title: typeof item?.title === "string" && item.title.trim()
          ? item.title.slice(0, 80)
          : "新對話",
        mode: normalizeMode(item?.mode),
        fingerprint: typeof item?.fingerprint === "string" ? item.fingerprint : "",
        messages: validMessages(item?.messages),
        createdAt: item?.createdAt || new Date().toISOString(),
        updatedAt: item?.updatedAt || item?.createdAt || new Date().toISOString(),
      }))
      : [];
    if (!conversations.length) conversations.push(makeConversation());
    const documents = Array.isArray(value?.documents)
      ? value.documents.filter((item) =>
        item && typeof item.id === "string" && typeof item.content === "string",
      ).map((item) => ({
        id: item.id,
        title: typeof item.title === "string" && item.title.trim()
          ? item.title.slice(0, 200)
          : "未命名文件",
        content: item.content,
        createdAt: item.createdAt || new Date().toISOString(),
        updatedAt: item.updatedAt || item.createdAt || new Date().toISOString(),
      }))
      : [];
    return {
      version: 2,
      activeConversationId: conversations.some(
        (item) => item.id === value?.activeConversationId,
      ) ? value.activeConversationId : conversations[0].id,
      activeDocumentId: documents.some(
        (item) => item.id === value?.activeDocumentId,
      ) ? value.activeDocumentId : (documents[0]?.id || ""),
      conversations,
      documents,
    };
  }

  function migrateLegacyConversation(identity) {
    try {
      const legacy = JSON.parse(localStorage.getItem(localKey(identity)) || "null");
      if (!legacy || !Array.isArray(legacy.messages)) return null;
      const messages = validMessages(legacy.messages);
      if (!messages.length) return null;
      const conversation = makeConversation(legacy.mode);
      conversation.messages = messages;
      conversation.fingerprint = typeof legacy.fingerprint === "string"
        ? legacy.fingerprint
        : "";
      conversation.title = titleFromText(
        messages.find((item) => item.role === "user")?.content || "舊對話",
      );
      return {
        version: 2,
        activeConversationId: conversation.id,
        activeDocumentId: "",
        conversations: [conversation],
        documents: [],
      };
    } catch (_) {
      return null;
    }
  }

  async function loadWorkspace(identity) {
    const previousIdentity = workspaceIdentity;
    if (previousIdentity && previousIdentity !== identity) {
      editorLeaseGeneration += 1;
      if (editorLeaseTimer) clearTimeout(editorLeaseTimer);
      editorLeaseTimer = null;
      workspaceEditable = false;
      await releaseEditorLease(previousIdentity);
    }
    workspaceIdentity = identity;
    const stored = await readStoredWorkspace(identity);
    const migrated = stored ? null : migrateLegacyConversation(identity);
    workspace = normalizeWorkspace(stored || migrated);
    return {needsPersist: !stored || Boolean(migrated), migrated: Boolean(migrated)};
  }

  function setWorkspaceEditable(editable) {
    workspaceEditable = Boolean(editable);
    const message = workspaceEditable
      ? ""
      : "此帳戶已在另一個分頁編輯工作區。本分頁暫時為唯讀，取得編輯權後會自動載入最新內容。";
    $("workspaceReadOnlyBanner").textContent = message;
    $("workspaceReadOnlyBanner").classList.toggle("hidden", workspaceEditable);
    $("chatSaveStatus").textContent = workspaceEditable
      ? "已儲存在此瀏覽器"
      : "唯讀模式";
    if (activeDocument()) {
      $("documentSaveStatus").textContent = workspaceEditable
        ? "已儲存在此瀏覽器"
        : "唯讀模式";
    }
    updateControls();
    renderConversationList();
    renderPromptCards();
    renderDocuments(true);
  }

  function scheduleEditorLeaseCheck(generation, delay = EDITOR_LEASE_POLL_MS) {
    if (editorLeaseTimer) clearTimeout(editorLeaseTimer);
    editorLeaseTimer = setTimeout(
      () => maintainEditorLease(generation),
      delay,
    );
  }

  async function maintainEditorLease(generation = editorLeaseGeneration) {
    if (!workspaceIdentity || generation !== editorLeaseGeneration || editorLeaseAttempting) return;
    editorLeaseAttempting = true;
    const identity = workspaceIdentity;
    try {
      const acquired = await acquireOrRenewEditorLease(identity);
      if (generation !== editorLeaseGeneration || identity !== workspaceIdentity) {
        if (acquired) await releaseEditorLease(identity);
        return;
      }
      if (acquired && !workspaceEditable) {
        const stored = await readStoredWorkspace(identity);
        if (generation !== editorLeaseGeneration || identity !== workspaceIdentity) return;
        if (stored) workspace = normalizeWorkspace(stored);
        setWorkspaceEditable(true);
        renderWorkspace();
        showToast("已取得工作區編輯權，並載入最新內容。");
      } else if (!acquired && workspaceEditable) {
        abortController?.abort();
        const stored = await readStoredWorkspace(identity);
        if (stored) workspace = normalizeWorkspace(stored);
        setWorkspaceEditable(false);
        renderWorkspace();
      }
    } catch (_) {
      setWorkspaceEditable(false);
    } finally {
      editorLeaseAttempting = false;
      if (generation === editorLeaseGeneration && identity === workspaceIdentity) {
        scheduleEditorLeaseCheck(generation);
      }
    }
  }

  async function startEditorLease({needsPersist = false, migrated = false} = {}) {
    editorLeaseGeneration += 1;
    const generation = editorLeaseGeneration;
    if (editorLeaseTimer) clearTimeout(editorLeaseTimer);
    editorLeaseTimer = null;
    setWorkspaceEditable(false);
    const acquired = await acquireOrRenewEditorLease(workspaceIdentity);
    if (generation !== editorLeaseGeneration) return;
    setWorkspaceEditable(acquired);
    if (acquired && needsPersist) {
      await persistWorkspace();
      if (migrated) localStorage.removeItem(localKey(workspaceIdentity));
    }
    scheduleEditorLeaseCheck(generation);
  }

  function persistWorkspace() {
    if (!workspace || !workspaceIdentity || !workspaceEditable) return Promise.resolve();
    const identity = workspaceIdentity;
    const snapshot = JSON.parse(JSON.stringify(workspace));
    $("chatSaveStatus").textContent = "正在保存…";
    if (activeDocument()) $("documentSaveStatus").textContent = "正在保存…";
    saveChain = saveChain.catch(() => undefined).then(
      () => writeStoredWorkspace(identity, snapshot),
    ).then(() => {
      if (identity !== workspaceIdentity) return;
      $("chatSaveStatus").textContent = "已儲存在此瀏覽器";
      if (activeDocument()) $("documentSaveStatus").textContent = "已儲存在此瀏覽器";
    }).catch((error) => {
      if (identity !== workspaceIdentity) return;
      setWorkspaceEditable(false);
      setNotice("未能保存至此瀏覽器，請先複製重要內容再重新載入。");
      $("chatSaveStatus").textContent = "保存失敗";
      if (activeDocument()) $("documentSaveStatus").textContent = "保存失敗";
      if (error?.code === "workspace_lease_lost") {
        void readStoredWorkspace(identity).then((stored) => {
          if (!stored || identity !== workspaceIdentity) return;
          workspace = normalizeWorkspace(stored);
          renderWorkspace();
        }).catch(() => undefined);
      }
    });
    return saveChain;
  }

  function setNotice(message) {
    $("contextNotice").textContent = message || "";
    $("contextNotice").classList.toggle("show", Boolean(message));
  }

  function requireWorkspaceEditor() {
    if (workspaceEditable) return true;
    showToast("此分頁目前為唯讀，請返回正在編輯的分頁。");
    return false;
  }

  async function confirmWorkspaceEditorLease() {
    if (!requireWorkspaceEditor()) return false;
    const identity = workspaceIdentity;
    try {
      const acquired = await acquireOrRenewEditorLease(identity);
      if (acquired && identity === workspaceIdentity) return true;
    } catch (_) {
      // Fall through to the same fail-closed read-only state.
    }
    if (identity === workspaceIdentity) {
      setWorkspaceEditable(false);
      const stored = await readStoredWorkspace(identity).catch(() => null);
      if (stored && identity === workspaceIdentity) {
        workspace = normalizeWorkspace(stored);
        renderWorkspace();
      }
    }
    showToast("另一個分頁已取得編輯權，今次操作沒有送出。");
    return false;
  }

  function showToast(message) {
    const toast = $("workspaceToast");
    toast.textContent = message || "";
    toast.classList.toggle("show", Boolean(message));
    if (toastTimer) clearTimeout(toastTimer);
    if (message) {
      toastTimer = setTimeout(() => {
        toast.classList.remove("show");
        toastTimer = null;
      }, 4200);
    }
  }

  function titleFromText(value) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text ? `${text.slice(0, 28)}${text.length > 28 ? "…" : ""}` : "新對話";
  }

  function relativeTime(value) {
    const timestamp = Date.parse(value || "");
    if (!Number.isFinite(timestamp)) return "";
    const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60000));
    if (minutes < 1) return "剛剛";
    if (minutes < 60) return `${minutes} 分鐘前`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} 小時前`;
    const days = Math.floor(hours / 24);
    return `${days} 日前`;
  }

  function renderConversationList() {
    const panel = $("conversationList");
    panel.replaceChildren();
    const query = $("conversationSearch").value.trim().toLocaleLowerCase("zh-HK");
    const conversations = [...workspace.conversations].sort(
      (a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)),
    ).filter((item) => !query || item.title.toLocaleLowerCase("zh-HK").includes(query));
    $("conversationCount").textContent =
      `${workspace.conversations.length} / ${bootstrap.history_limits.conversations}`;
    conversations.forEach((item) => {
      const row = document.createElement("div");
      row.className = `conversation-item${item.id === workspace.activeConversationId ? " active" : ""}`;
      const select = document.createElement("button");
      select.className = "conversation-select";
      select.type = "button";
      const title = document.createElement("span");
      title.className = "conversation-title";
      title.textContent = item.title;
      const meta = document.createElement("span");
      meta.className = "conversation-meta";
      meta.textContent = `${modeLabel(item.mode)}・${relativeTime(item.updatedAt)}`;
      select.append(title, meta);
      select.onclick = () => selectConversation(item.id);
      const remove = document.createElement("button");
      remove.className = "conversation-menu danger";
      remove.type = "button";
      remove.textContent = "×";
      remove.title = "刪除對話";
      remove.setAttribute("aria-label", `刪除對話：${item.title}`);
      remove.disabled = !workspaceEditable;
      remove.onclick = () => deleteConversation(item.id);
      row.append(select, remove);
      panel.append(row);
    });
    if (!conversations.length) {
      const empty = document.createElement("p");
      empty.className = "caption";
      empty.textContent = "找不到相符對話。";
      panel.append(empty);
    }
  }

  function actionButton(label, handler, requiresEdit = false) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.disabled = requiresEdit && !workspaceEditable;
    button.onclick = handler;
    return button;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(String(text || ""));
      setNotice("內容已複製。");
    } catch (_) {
      setNotice("瀏覽器未能複製內容，請手動選取後複製。");
    }
  }

  function messageElement(item, index, persisted) {
    const wrap = document.createElement("div");
    wrap.className = `message-wrap ${item.role}`;
    if (item.role === "assistant") {
      const avatar = document.createElement("img");
      avatar.className = "assistant-avatar";
      avatar.src = $("conversation").dataset.avatarSrc;
      avatar.alt = `${bootstrap.name} 頭像`;
      avatar.width = 32;
      avatar.height = 32;
      avatar.loading = "lazy";
      avatar.decoding = "async";
      wrap.append(avatar);
    }
    const stack = document.createElement("div");
    stack.className = "message-stack";
    const bubble = document.createElement("article");
    bubble.className = `message ${item.role}`;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = item.role === "user" ? "你" : `${bootstrap.emoji} ${bootstrap.name}`;
    const body = document.createElement("div");
    if (item.role === "assistant") body.innerHTML = SafeMarkdown.render(item.content);
    else body.textContent = item.content;
    bubble.append(who, body);
    const actions = document.createElement("div");
    actions.className = "message-actions";
    actions.append(actionButton("複製", () => copyText(item.content)));
    if (persisted && item.role === "user") {
      actions.append(actionButton("編輯再問", () => insertPrompt(item.content), true));
    }
    if (persisted && item.role === "assistant") {
      actions.append(
        actionButton("再生成", () => regenerateAnswer(index), true),
        actionButton("建立文件", () => createDocumentFromAnswer(item.content), true),
        actionButton("加入文件", () => appendAnswerToDocument(item.content), true),
      );
    }
    stack.append(bubble, actions);
    wrap.append(stack);
    return wrap;
  }

  function renderMessages() {
    const panel = $("conversation");
    panel.replaceChildren();
    const conversation = activeConversation();
    if (!conversation) return;
    const displayedMessages = provisionalBase || conversation.messages;
    const all = [...displayedMessages, ...provisional];
    if (!all.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.innerHTML = SafeMarkdown.render(
        `## ${bootstrap.emoji} 我哋開始啦！\n\n`
        + "揀上面嘅 Prompt，或者直接講低你想處理嘅內容。",
      );
      panel.append(empty);
    } else {
      all.forEach((item, index) => {
        panel.append(messageElement(item, index, index < displayedMessages.length));
      });
    }
    panel.scrollTop = panel.scrollHeight;
  }

  function renderModeOptions() {
    const select = $("thinkingMode");
    const conversation = activeConversation();
    if (!conversation) return;
    const modes = bootstrap?.service?.modes || [];
    select.replaceChildren(...modes.map((mode) => {
      const option = document.createElement("option");
      option.value = mode.id;
      option.textContent = mode.label;
      option.disabled = !fingerprintForMode(mode.id);
      return option;
    }));
    conversation.mode = normalizeMode(conversation.mode);
    select.value = conversation.mode;
  }

  const statusLabels = {
    unconfigured: "尚未選擇 AI 電腦",
    unavailable: "自家 AI 離線",
    draining: "自家 AI 暫停接新工作",
    busy: "自家 AI 忙碌",
    online: "自家 AI 在線",
    suspended: "互動功能暫停",
  };

  function renderNodes() {
    const grid = $("nodeGrid");
    const node = bootstrap?.workstation;
    grid.replaceChildren();
    if (!node) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "尚未設定 AI Workstation。";
      grid.append(empty);
      return;
    }
    const labels = {
      online: "在線",
      busy: "生成中",
      draining: "準備休眠／暫停接單",
      offline: "離線",
    };
    const card = document.createElement("article");
    card.className = "node-card";
    const title = document.createElement("h3");
    title.textContent = node.name || "AI Workstation";
    const meta = document.createElement("div");
    meta.className = "node-meta";
    [
      `狀態：${labels[node.state] || "未知"}`,
      `可用模型：${Array.isArray(node.models) && node.models.length ? node.models.join("、") : "等待檢查"}`,
      `排隊：${Number(node.queue_length || 0)}`,
    ].forEach((text) => {
      const item = document.createElement("span");
      item.textContent = text;
      meta.append(item);
    });
    card.append(title, meta);
    grid.append(card);
  }

  function renderStatus() {
    const service = bootstrap.service;
    let label = statusLabels[service.state] || "服務狀態未知";
    if (service.queue_length) label += `・${service.queue_length} 個工作排隊`;
    $("serviceStatus").textContent = label;
    $("statusDot").className = `status-dot ${
      service.state === "online" ? "online" : service.state === "busy" ? "busy" : "offline"
    }`;
    $("mobileStatusButton").textContent =
      ["online", "busy"].includes(service.state) ? "●" : "○";
    $("identityStatus").textContent = bootstrap.identity.developer
      ? "Developer 試用"
      : `已登入：${bootstrap.identity.id}`;
    renderModeOptions();
    const conversation = activeConversation();
    const currentFingerprint = fingerprintForMode(conversation.mode);
    backendChanged = Boolean(
      conversation.messages.length && conversation.fingerprint
      && currentFingerprint && conversation.fingerprint !== currentFingerprint,
    );
    if (backendChanged) {
      setNotice("AI 電腦、模型或 persona 已更新。舊對話仍可查看，請開始新對話後再繼續。");
    }
    renderNodes();
    updateControls();
  }

  function updateControls() {
    if (!bootstrap || !workspace) return;
    const conversation = activeConversation();
    const running = Boolean(abortController);
    const serviceReady = ["online", "busy"].includes(bootstrap.service.state);
    const limits = bootstrap.history_limits;
    const capReached = conversation.messages.length + 2 > limits.messages
      || totalCharacters(conversation.messages) >= limits.characters;
    const modeReady = Boolean(fingerprintForMode(conversation.mode));
    $("sendButton").disabled =
      running || backendChanged || !serviceReady || !modeReady || capReached
      || !workspaceEditable;
    $("stopButton").disabled = !running;
    $("messageInput").disabled = running || backendChanged || !workspaceEditable;
    $("thinkingMode").disabled = running || !workspaceEditable;
    $("newChat").disabled = running || !workspaceEditable;
    if (capReached && !backendChanged) {
      setNotice("此對話已到本機保存上限，請開始新對話。舊內容不會被自動移除。");
    }
  }

  function renderPromptCards() {
    const grid = $("promptGrid");
    grid.replaceChildren();
    (bootstrap.prompt_templates || []).forEach((template) => {
      const button = document.createElement("button");
      button.className = "prompt-card";
      button.type = "button";
      const title = document.createElement("strong");
      title.textContent = template.title;
      const description = document.createElement("span");
      description.textContent = template.description;
      button.append(title, description);
      button.disabled = !workspaceEditable;
      button.onclick = () => insertPrompt(template.prompt);
      grid.append(button);
    });
  }

  function renderDocuments(preserveEditor = false) {
    const list = $("documentList");
    list.replaceChildren();
    $("documentCount").textContent =
      `${workspace.documents.length} / ${bootstrap.history_limits.documents}`;
    [...workspace.documents].sort(
      (a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)),
    ).forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className =
        `document-chip${item.id === workspace.activeDocumentId ? " active" : ""}`;
      button.textContent = item.title;
      button.title = item.title;
      button.onclick = () => selectDocument(item.id);
      list.append(button);
    });
    const current = activeDocument();
    $("documentEmpty").classList.toggle("hidden", Boolean(current));
    $("documentEditorPanel").classList.toggle("hidden", !current);
    $("deleteDocument").disabled = !current;
    $("exportDocument").disabled = !current;
    $("newDocument").disabled = !workspaceEditable;
    $("newDocumentSidebar").disabled = !workspaceEditable;
    $("emptyNewDocument").disabled = !workspaceEditable;
    $("deleteDocument").disabled = !current || !workspaceEditable;
    $("documentTitle").readOnly = !workspaceEditable;
    $("documentEditor").readOnly = !workspaceEditable;
    document.querySelectorAll("[data-rewrite]").forEach((button) => {
      button.disabled = !current || !workspaceEditable;
    });
    if (!current) return;
    if (preserveEditor) return;
    $("documentTitle").value = current.title;
    $("documentEditor").value = current.content;
    $("documentEditor").maxLength = bootstrap.history_limits.characters;
    if (previewVisible) {
      $("documentPreview").innerHTML = SafeMarkdown.render(current.content);
    }
  }

  function renderWorkspace() {
    if (!workspace || !bootstrap) return;
    const conversation = activeConversation();
    $("activeConversationTitle").textContent = conversation.title;
    renderConversationList();
    renderMessages();
    renderPromptCards();
    renderDocuments();
    renderStatus();
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", ...(options.headers || {})},
      ...options,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.detail || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  async function refreshBootstrap(showLogin = true) {
    const generation = ++pollGeneration;
    try {
      const response = await fetch(
        "/api/lmc-ai/bootstrap",
        {credentials: "same-origin", cache: "no-store"},
      );
      if (generation !== pollGeneration) return;
      if (response.status === 401) {
        if (showLogin) $("loginCard").classList.add("show");
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      requireDefaultMode(data);
      const identityChanged =
        !bootstrap || bootstrap.identity.id !== data.identity.id || !workspace;
      bootstrap = data;
      if (identityChanged) {
        const loadState = await loadWorkspace(data.identity.id);
        if (generation !== pollGeneration) return;
        provisional = [];
        provisionalBase = null;
        await startEditorLease(loadState);
        if (generation !== pollGeneration) return;
        renderWorkspace();
      } else {
        renderStatus();
      }
      $("loginCard").classList.remove("show");
    } catch (error) {
      $("serviceStatus").textContent = error.message || "未能讀取服務狀態";
      $("statusDot").className = "status-dot offline";
      if (error.message === "自家 AI 回答模式設定無效。") {
        bootstrap = null;
        abortController?.abort();
      }
      $("messageInput").disabled = true;
      $("thinkingMode").disabled = true;
      $("sendButton").disabled = true;
    }
  }

  function requestContext(currentText, history = activeConversation().messages) {
    const maxChars = bootstrap.history_limits.context_characters;
    const maxMessages = bootstrap.history_limits.request_messages;
    const selected = [{role: "user", content: currentText}];
    let characters = currentText.length;
    for (let index = history.length - 2; index >= 0; index -= 2) {
      const pair = history.slice(index, index + 2);
      if (pair.length !== 2 || pair[0].role !== "user" || pair[1].role !== "assistant") break;
      const pairChars = totalCharacters(pair);
      if (selected.length + 2 > maxMessages || characters + pairChars > maxChars) break;
      selected.unshift(...pair);
      characters += pairChars;
    }
    return {messages: selected, trimmed: selected.length < history.length + 1};
  }

  async function parseEventStream(response, onEvent) {
    if (!response.body) throw new Error("瀏覽器不支援串流回覆。");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        let event = "message";
        let data = "";
        block.split("\n").forEach((line) => {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          if (line.startsWith("data:")) data += line.slice(5).trim();
        });
        if (data) onEvent(event, JSON.parse(data));
      }
      if (done) break;
    }
  }

  async function sendMessage({replacementFromIndex = null} = {}) {
    const text = $("messageInput").value;
    if (!text.trim() || abortController || !bootstrap || !activeConversation()
        || !requireWorkspaceEditor()) return;
    if (!await confirmWorkspaceEditorLease()) return;
    const conversation = activeConversation();
    const replacing = Number.isInteger(replacementFromIndex)
      && replacementFromIndex >= 0
      && replacementFromIndex < conversation.messages.length;
    const baseMessages = replacing
      ? conversation.messages.slice(0, replacementFromIndex)
      : conversation.messages.slice();
    if (text.length > bootstrap.history_limits.user_message_characters) {
      setNotice("訊息超過字數上限。");
      return;
    }
    const projected = totalCharacters(baseMessages) + text.length;
    if (baseMessages.length + 2 > bootstrap.history_limits.messages
        || projected >= bootstrap.history_limits.characters) {
      setNotice("加入這個回合會超過本機保存上限，請開始新對話。");
      return;
    }
    const context = requestContext(text, baseMessages);
    if (context.trimmed) {
      setNotice("較舊訊息仍保留在畫面，但今次未有送入模型 context。如要引用，請在新訊息重述重點。");
    }
    const fingerprint = baseMessages.length
      ? conversation.fingerprint
      : fingerprintForMode(conversation.mode);
    if (!fingerprint) {
      setNotice("自家 AI 暫時未準備好。");
      return;
    }
    provisionalBase = baseMessages;
    provisional = [{role: "user", content: text}, {role: "assistant", content: ""}];
    renderMessages();
    $("messageInput").value = "";
    updateCharacterCount();
    const requestGeneration = conversationGeneration;
    const requestConversationId = conversation.id;
    const requestController = new AbortController();
    abortController = requestController;
    updateControls();
    let answer = "";
    let completed = false;
    try {
      const response = await fetch("/api/lmc-ai/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          messages: context.messages,
          expected_fingerprint: fingerprint,
          has_history: baseMessages.length > 0,
          mode: conversation.mode,
        }),
        signal: requestController.signal,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      await parseEventStream(response, (event, payload) => {
        if (requestGeneration !== conversationGeneration
            || requestConversationId !== workspace.activeConversationId) return;
        if (event === "queued") {
          $("serviceStatus").textContent = `排隊位置：${payload.position}`;
        } else if (event === "status") {
          $("serviceStatus").textContent =
            payload.state === "generating" ? "正在生成回覆…" : "正在啟動模型…";
        } else if (event === "delta") {
          answer += String(payload.text || "");
          if (projected + answer.length > bootstrap.history_limits.characters) {
            requestController.abort();
            throw new Error("回覆會超過本機保存上限，已停止生成。請開始新對話。");
          }
          provisional[1].content = answer;
          renderMessages();
        } else if (event === "complete") {
          completed = true;
          conversation.messages = [
            ...baseMessages,
            {role: "user", content: text},
            {role: "assistant", content: answer},
          ];
          conversation.fingerprint = payload.fingerprint || fingerprint;
          conversation.updatedAt = new Date().toISOString();
          if (conversation.title === "新對話") conversation.title = titleFromText(text);
          provisional = [];
          provisionalBase = null;
          persistWorkspace();
          if (payload.model_changed) {
            backendChanged = true;
            setNotice("AI 模型在今次回覆期間更新，回覆已保留。下一步請開始新對話。");
          }
        } else if (event === "error") {
          throw new Error(payload.message || "AI 未能完成今次回覆。");
        }
      });
      if (requestGeneration !== conversationGeneration) return;
      if (!completed) throw new Error("AI 串流提早中斷。");
    } catch (error) {
      if (requestGeneration === conversationGeneration) {
        if (error.name === "AbortError") {
          setNotice("已停止生成，未完成的回合沒有保存。");
        } else {
          setNotice(error.message || "AI 未能完成今次回覆。");
        }
        provisional = [];
        provisionalBase = null;
      }
    } finally {
      if (abortController === requestController) abortController = null;
      if (requestGeneration === conversationGeneration) renderWorkspace();
      refreshBootstrap(false);
    }
  }

  function createConversation(mode = requireDefaultMode()) {
    if (!requireWorkspaceEditor()) return null;
    if (abortController) {
      setNotice("請先停止目前回覆，再開始新對話。");
      return null;
    }
    if (workspace.conversations.length >= bootstrap.history_limits.conversations) {
      const message =
        `已達 ${bootstrap.history_limits.conversations} 個對話上限。舊對話不會被自動刪除，請到「最近」手動刪除一個對話。`;
      setNotice(message);
      showToast(message);
      showMobileView("recent");
      return null;
    }
    const conversation = makeConversation(mode);
    workspace.conversations.push(conversation);
    workspace.activeConversationId = conversation.id;
    conversationGeneration += 1;
    provisional = [];
    provisionalBase = null;
    backendChanged = false;
    setNotice("");
    persistWorkspace();
    renderWorkspace();
    showMobileView("chat");
    $("messageInput").focus();
    return conversation;
  }

  function selectConversation(id) {
    if (id === workspace.activeConversationId) {
      showMobileView("chat");
      return;
    }
    if (abortController) {
      setNotice("請先停止目前回覆，再切換對話。");
      return;
    }
    if (!workspace.conversations.some((item) => item.id === id)) return;
    workspace.activeConversationId = id;
    conversationGeneration += 1;
    provisional = [];
    provisionalBase = null;
    backendChanged = false;
    setNotice("");
    persistWorkspace();
    renderWorkspace();
    showMobileView("chat");
  }

  function deleteConversation(id) {
    if (!requireWorkspaceEditor()) return;
    const conversation = workspace.conversations.find((item) => item.id === id);
    if (!conversation || abortController) {
      if (abortController) setNotice("請先停止目前回覆，再刪除對話。");
      return;
    }
    if (!confirm(`刪除「${conversation.title}」？內容只存在此瀏覽器，刪除後無法還原。`)) return;
    workspace.conversations = workspace.conversations.filter((item) => item.id !== id);
    if (!workspace.conversations.length) workspace.conversations.push(makeConversation());
    if (workspace.activeConversationId === id) {
      workspace.activeConversationId = workspace.conversations[0].id;
      conversationGeneration += 1;
      provisional = [];
      provisionalBase = null;
      backendChanged = false;
    }
    persistWorkspace();
    renderWorkspace();
  }

  function switchConversationMode() {
    if (!requireWorkspaceEditor()) return;
    const conversation = activeConversation();
    const nextMode = normalizeMode($("thinkingMode").value);
    const previousMode = conversation.mode;
    if (nextMode === previousMode) return;
    if (!conversation.messages.length) {
      conversation.mode = nextMode;
      conversation.fingerprint = fingerprintForMode(nextMode);
      conversation.updatedAt = new Date().toISOString();
      persistWorkspace();
      renderWorkspace();
      return;
    }
    if (!confirm(
      `使用「${modeLabel(nextMode)}」另開新對話？目前對話會完整保留。`,
    )) {
      $("thinkingMode").value = previousMode;
      return;
    }
    if (!createConversation(nextMode)) $("thinkingMode").value = previousMode;
  }

  function regenerateAnswer(index) {
    if (!requireWorkspaceEditor()) return;
    const conversation = activeConversation();
    const prompt = conversation.messages[index - 1];
    if (abortController || !prompt || prompt.role !== "user") return;
    const laterTurns = index < conversation.messages.length - 1;
    if (laterTurns && !confirm("從這則訊息重新生成會移除其後的對話內容，是否繼續？")) return;
    $("messageInput").value = prompt.content;
    updateCharacterCount();
    sendMessage({replacementFromIndex: index - 1});
  }

  function openPromptDialog() {
    if (typeof $("promptDialog").showModal === "function") {
      $("promptDialog").showModal();
    }
  }

  function applyPrompt(text, append = false) {
    if (!requireWorkspaceEditor()) return;
    const input = $("messageInput");
    const combined = append && input.value
      ? `${input.value.trimEnd()}\n\n${text}`
      : text;
    if (combined.length > bootstrap.history_limits.user_message_characters) {
      setNotice("加入後會超過訊息字數上限，請先縮短現有內容。");
      return;
    }
    input.value = combined;
    updateCharacterCount();
    showMobileView("chat");
    input.focus();
  }

  function insertPrompt(text) {
    if (!bootstrap || !text || !requireWorkspaceEditor()) return;
    const input = $("messageInput");
    if (!input.value.trim()) {
      applyPrompt(text);
      return;
    }
    pendingPrompt = text;
    openPromptDialog();
  }

  function createDocument(title = "未命名文件", content = "") {
    if (!requireWorkspaceEditor()) return null;
    if (workspace.documents.length >= bootstrap.history_limits.documents) {
      const message =
        `已達 ${bootstrap.history_limits.documents} 份文件上限。舊文件不會被自動刪除，請先手動刪除一份文件。`;
      setNotice(message);
      showToast(message);
      showMobileView("documents");
      return null;
    }
    if (content.length > bootstrap.history_limits.characters) {
      setNotice("文件內容超過本機保存上限。");
      return null;
    }
    const item = makeDocument(title, content);
    workspace.documents.push(item);
    workspace.activeDocumentId = item.id;
    previewVisible = false;
    persistWorkspace();
    renderDocuments();
    showMobileView("documents");
    return item;
  }

  function createDocumentFromAnswer(content) {
    const heading = String(content || "").match(/^#{1,6}\s+(.+)$/m)?.[1];
    const firstLine = String(content || "").split("\n").find((line) => line.trim());
    const title = (heading || firstLine || "AI 回覆文件")
      .replace(/[*_`#]/g, "").trim().slice(0, 80);
    createDocument(title, content);
  }

  function appendAnswerToDocument(content) {
    if (!requireWorkspaceEditor()) return;
    let target = activeDocument();
    if (!target) {
      createDocumentFromAnswer(content);
      return;
    }
    const combined = `${target.content.trimEnd()}${target.content.trim() ? "\n\n" : ""}${content}`;
    if (combined.length > bootstrap.history_limits.characters) {
      setNotice("加入後會超過文件保存上限，請先縮短文件內容。");
      return;
    }
    target.content = combined;
    target.updatedAt = new Date().toISOString();
    persistWorkspace();
    renderDocuments();
    showMobileView("documents");
  }

  function selectDocument(id) {
    if (!workspace.documents.some((item) => item.id === id)) return;
    if (documentSaveTimer) {
      clearTimeout(documentSaveTimer);
      documentSaveTimer = null;
      persistWorkspace();
    }
    workspace.activeDocumentId = id;
    previewVisible = false;
    persistWorkspace();
    renderDocuments();
  }

  function deleteDocument() {
    if (!requireWorkspaceEditor()) return;
    const item = activeDocument();
    if (!item) return;
    if (!confirm(`刪除「${item.title}」？文件只存在此瀏覽器，刪除後無法還原。`)) return;
    workspace.documents = workspace.documents.filter((document) => document.id !== item.id);
    workspace.activeDocumentId = workspace.documents[0]?.id || "";
    previewVisible = false;
    persistWorkspace();
    renderDocuments();
  }

  function scheduleDocumentSave() {
    if (!workspaceEditable) return;
    if (documentSaveTimer) clearTimeout(documentSaveTimer);
    $("documentSaveStatus").textContent = "正在編輯…";
    documentSaveTimer = setTimeout(() => {
      documentSaveTimer = null;
      persistWorkspace();
      renderConversationList();
      renderDocuments(true);
    }, 300);
  }

  function updateDocumentFromEditor() {
    if (!requireWorkspaceEditor()) return;
    const item = activeDocument();
    if (!item) return;
    item.title = $("documentTitle").value.trim().slice(0, 200) || "未命名文件";
    item.content = $("documentEditor").value;
    item.updatedAt = new Date().toISOString();
    if (previewVisible) $("documentPreview").innerHTML = SafeMarkdown.render(item.content);
    scheduleDocumentSave();
  }

  function selectedDocumentPrompt(action) {
    if (!requireWorkspaceEditor()) return;
    const editor = $("documentEditor");
    const selected = editor.value.slice(editor.selectionStart, editor.selectionEnd).trim();
    if (!selected) {
      setNotice("請先在文件中選取一段內容。");
      return;
    }
    const instructions = {
      shorten: "幫我將下面呢段改短啲，保留關鍵論點同證據，唔好改變原意：",
      tone: "幫我將下面呢段改到更自然、更適合香港中學生直接講出嚟：",
      example: "幫我為下面呢段補一至兩個具體例子，並解釋例子點樣支持論點：",
      rewrite: "幫我重寫下面呢段，令結構同推論更清楚，但保留原本立場：",
    };
    const prompt = `${instructions[action]}\n\n${selected}`;
    if (prompt.length > bootstrap.history_limits.user_message_characters) {
      setNotice("所選內容太長，請縮窄選取範圍後再試。");
      return;
    }
    insertPrompt(prompt);
  }

  function safeDownloadName(title, extension) {
    const stem = String(title || "自家AI文件").replace(/[\\/:*?"<>|]+/g, "").trim();
    return `${stem || "自家AI文件"}.${extension}`;
  }

  async function exportDocument() {
    const item = activeDocument();
    if (!item) return;
    $("exportDocument").disabled = true;
    $("documentSaveStatus").textContent = "正在製作匯出檔案…";
    try {
      const format = $("exportFormat").value;
      const response = await fetch("/api/lmc-ai/documents/export", {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({title: item.title, content: item.content, format}),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = safeDownloadName(item.title, format === "markdown" ? "md" : format);
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      $("documentSaveStatus").textContent = "匯出完成";
    } catch (error) {
      $("documentSaveStatus").textContent = error.message || "未能匯出文件";
    } finally {
      $("exportDocument").disabled = false;
    }
  }

  function togglePreview() {
    const item = activeDocument();
    if (!item) return;
    previewVisible = !previewVisible;
    $("documentEditor").classList.toggle("hidden", previewVisible);
    $("documentPreview").classList.toggle("hidden", !previewVisible);
    $("togglePreview").textContent = previewVisible ? "返回編輯" : "預覽";
    if (previewVisible) $("documentPreview").innerHTML = SafeMarkdown.render(item.content);
  }

  function showMobileView(view) {
    document.body.dataset.mobileView = view;
    document.querySelectorAll("[data-mobile-target]").forEach((button) => {
      button.classList.toggle("active", button.dataset.mobileTarget === view);
    });
  }

  function updateCharacterCount() {
    const maximum =
      bootstrap?.history_limits.user_message_characters || $("messageInput").maxLength;
    $("characterCount").textContent = `${$("messageInput").value.length} / ${maximum}`;
  }

  function setComposerExpanded(expanded) {
    $("composerBox").classList.toggle("expanded", expanded);
    $("expandComposer").disabled = expanded;
    $("collapseComposer").disabled = !expanded;
  }

  function openStatus() {
    if (typeof $("statusDialog").showModal === "function") $("statusDialog").showModal();
  }

  $("messageInput").addEventListener("input", updateCharacterCount);
  $("messageInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });
  $("sendButton").onclick = () => sendMessage();
  $("stopButton").onclick = () => abortController?.abort();
  $("expandComposer").onclick = () => setComposerExpanded(true);
  $("collapseComposer").onclick = () => setComposerExpanded(false);
  $("thinkingMode").onchange = switchConversationMode;
  $("newChat").onclick = () => createConversation();
  $("conversationSearch").oninput = () => workspace && renderConversationList();
  $("statusButton").onclick = openStatus;
  $("mobileStatusButton").onclick = openStatus;
  $("newDocument").onclick = () => createDocument();
  $("newDocumentSidebar").onclick = () => createDocument();
  $("emptyNewDocument").onclick = () => createDocument();
  $("deleteDocument").onclick = deleteDocument;
  $("documentTitle").oninput = updateDocumentFromEditor;
  $("documentEditor").oninput = updateDocumentFromEditor;
  $("togglePreview").onclick = togglePreview;
  $("exportDocument").onclick = exportDocument;
  document.querySelectorAll("[data-rewrite]").forEach((button) => {
    button.onclick = () => selectedDocumentPrompt(button.dataset.rewrite);
  });
  document.querySelectorAll("[data-mobile-target]").forEach((button) => {
    button.onclick = () => showMobileView(button.dataset.mobileTarget);
  });
  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.onclick = () => $(button.dataset.closeDialog).close();
  });
  $("appendPrompt").onclick = () => {
    $("promptDialog").close();
    applyPrompt(pendingPrompt, true);
    pendingPrompt = "";
  };
  $("replacePrompt").onclick = () => {
    $("promptDialog").close();
    applyPrompt(pendingPrompt);
    pendingPrompt = "";
  };
  $("loginButton").onclick = async () => {
    $("loginError").textContent = "";
    try {
      await api("/api/committee/login", {
        method: "POST",
        body: JSON.stringify({
          user_id: $("loginUser").value.trim(),
          password: $("loginPassword").value,
        }),
      });
      $("loginPassword").value = "";
      await refreshBootstrap();
    } catch (error) {
      $("loginError").textContent = error.message;
    }
  };
  window.addEventListener("pagehide", () => {
    editorLeaseGeneration += 1;
    if (editorLeaseTimer) clearTimeout(editorLeaseTimer);
    editorLeaseTimer = null;
    void releaseEditorLease(workspaceIdentity);
  });

  refreshBootstrap();
  setInterval(() => {
    if (!abortController) refreshBootstrap(false);
  }, 10000);
})();
