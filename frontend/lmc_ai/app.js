(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let bootstrap = null;
  let storageKey = "";
  let conversation = { fingerprint: "", messages: [] };
  let abortController = null;
  let provisional = [];
  let backendChanged = false;
  let pollGeneration = 0;
  let conversationGeneration = 0;

  function totalCharacters(messages) {
    return messages.reduce((sum, item) => sum + String(item.content || "").length, 0);
  }

  function localKey(identity) {
    return `lmc-ai-chat:v1:${encodeURIComponent(identity)}`;
  }

  function loadLocal() {
    conversation = { fingerprint: "", messages: [] };
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (value && typeof value.fingerprint === "string" && Array.isArray(value.messages)) {
        const messages = value.messages.filter((item) =>
          item && ["user", "assistant"].includes(item.role) && typeof item.content === "string",
        );
        if (messages.length <= bootstrap.history_limits.messages &&
            totalCharacters(messages) <= bootstrap.history_limits.characters) {
          conversation = { fingerprint: value.fingerprint, messages };
        }
      }
    } catch (_) {
      conversation = { fingerprint: "", messages: [] };
    }
  }

  function saveLocal() {
    localStorage.setItem(storageKey, JSON.stringify(conversation));
  }

  function setNotice(message) {
    $("contextNotice").textContent = message || "";
    $("contextNotice").classList.toggle("show", Boolean(message));
  }

  function messageElement(item) {
    const bubble = document.createElement("article");
    bubble.className = `message ${item.role}`;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = item.role === "user" ? "你" : `${bootstrap.emoji} ${bootstrap.name}`;
    const body = document.createElement("div");
    if (item.role === "assistant") body.innerHTML = SafeMarkdown.render(item.content);
    else body.textContent = item.content;
    bubble.append(who, body);
    return bubble;
  }

  function renderMessages() {
    const panel = $("conversation");
    panel.replaceChildren();
    const all = [...conversation.messages, ...provisional];
    if (!all.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = `${bootstrap.emoji} 我係 ${bootstrap.name}，有咩辯論上嘅事想一齊拆解？`;
      panel.append(empty);
    } else {
      all.forEach((item) => panel.append(messageElement(item)));
    }
    panel.scrollTop = panel.scrollHeight;
  }

  const statusLabels = {
    unconfigured: "尚未選擇 AI 電腦",
    unavailable: "自家 AI 離線",
    draining: "自家 AI 暫停接新工作",
    busy: "自家 AI 忙碌",
    online: "自家 AI 在線",
    suspended: "互動功能暫停",
  };

  function renderStatus() {
    const service = bootstrap.service;
    let label = statusLabels[service.state] || "服務狀態未知";
    if (service.queue_length) label += `・${service.queue_length} 個工作排隊`;
    $("serviceStatus").textContent = label;
    $("statusDot").className = `dot ${service.state === "online" ? "online" : service.state === "busy" ? "busy" : "offline"}`;
    $("identityStatus").textContent = bootstrap.identity.developer
      ? "Developer 試用"
      : `已登入：${bootstrap.identity.id}`;
    backendChanged = Boolean(
      conversation.messages.length && conversation.fingerprint &&
      bootstrap.backend_fingerprint &&
      (!conversation.fingerprint || conversation.fingerprint !== bootstrap.backend_fingerprint),
    );
    if (backendChanged) setNotice("AI 電腦、模型或 persona 已更新。舊對話仍可查看，但必須開始新對話先可以繼續。");
    updateControls();
  }

  function updateControls() {
    if (!bootstrap) return;
    const running = Boolean(abortController);
    const serviceReady = ["online", "busy"].includes(bootstrap.service.state);
    const limits = bootstrap.history_limits;
    const capReached = conversation.messages.length + 2 > limits.messages ||
      totalCharacters(conversation.messages) >= limits.characters;
    $("sendButton").disabled = running || backendChanged || !serviceReady || capReached;
    $("stopButton").disabled = !running;
    $("messageInput").disabled = running || backendChanged;
    if (capReached && !backendChanged) {
      setNotice("本機對話已到保存上限，請先開始新對話或刪除目前對話。舊內容唔會被自動移除。");
    }
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
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
      const response = await fetch("/api/lmc-ai/bootstrap", { credentials: "same-origin", cache: "no-store" });
      if (generation !== pollGeneration) return;
      if (response.status === 401) {
        if (showLogin) $("loginCard").classList.add("show");
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      const identityChanged = !bootstrap || bootstrap.identity.id !== data.identity.id;
      bootstrap = data;
      if (identityChanged) {
        storageKey = localKey(data.identity.id);
        loadLocal();
        provisional = [];
        renderMessages();
      }
      $("loginCard").classList.remove("show");
      renderStatus();
    } catch (error) {
      $("serviceStatus").textContent = error.message || "未能讀取服務狀態";
      $("statusDot").className = "dot offline";
    }
  }

  function requestContext(currentText) {
    const maxChars = bootstrap.history_limits.context_characters;
    const maxMessages = bootstrap.history_limits.request_messages;
    const selected = [{ role: "user", content: currentText }];
    let characters = currentText.length;
    const history = conversation.messages;
    for (let index = history.length - 2; index >= 0; index -= 2) {
      const pair = history.slice(index, index + 2);
      if (pair.length !== 2 || pair[0].role !== "user" || pair[1].role !== "assistant") break;
      const pairChars = totalCharacters(pair);
      if (selected.length + 2 > maxMessages || characters + pairChars > maxChars) break;
      selected.unshift(...pair);
      characters += pairChars;
    }
    return { messages: selected, trimmed: selected.length < history.length + 1 };
  }

  async function parseEventStream(response, onEvent) {
    if (!response.body) throw new Error("Browser 不支援串流回覆。");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
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

  async function sendMessage() {
    const text = $("messageInput").value;
    if (!text.trim() || abortController || !bootstrap) return;
    if (text.length > bootstrap.history_limits.user_message_characters) {
      setNotice("訊息超過字數上限。");
      return;
    }
    const projected = totalCharacters(conversation.messages) + text.length;
    if (conversation.messages.length + 2 > bootstrap.history_limits.messages ||
        projected >= bootstrap.history_limits.characters) {
      setNotice("加入今次回合會超過本機保存上限，請先開始新對話或刪除目前對話。");
      return;
    }
    const context = requestContext(text);
    if (context.trimmed) setNotice("較舊訊息仍然保留喺畫面，但今次未有送入模型 context。要引用舊內容請喺新訊息重點講返。");
    const fingerprint = conversation.fingerprint || bootstrap.backend_fingerprint;
    if (!fingerprint) {
      setNotice("自家 AI 暫時未準備好。");
      return;
    }
    provisional = [{ role: "user", content: text }, { role: "assistant", content: "" }];
    renderMessages();
    $("messageInput").value = "";
    $("characterCount").textContent = `0 / ${bootstrap.history_limits.user_message_characters}`;
    const requestGeneration = conversationGeneration;
    const requestController = new AbortController();
    abortController = requestController;
    updateControls();
    let answer = "";
    let completed = false;
    try {
      const response = await fetch("/api/lmc-ai/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: context.messages,
          expected_fingerprint: fingerprint,
          has_history: conversation.messages.length > 0,
        }),
        signal: requestController.signal,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      await parseEventStream(response, (event, payload) => {
        if (requestGeneration !== conversationGeneration) return;
        if (event === "queued") {
          $("serviceStatus").textContent = `排隊位置：${payload.position}`;
        } else if (event === "status") {
          $("serviceStatus").textContent = payload.state === "generating" ? "正在生成回覆…" : "正在啟動模型…";
        } else if (event === "delta") {
          answer += String(payload.text || "");
          if (projected + answer.length > bootstrap.history_limits.characters) {
            abortController.abort();
            throw new Error("回覆會超過本機保存上限，已停止生成；請開始新對話。");
          }
          provisional[1].content = answer;
          renderMessages();
        } else if (event === "complete") {
          completed = true;
          conversation.messages.push({ role: "user", content: text }, { role: "assistant", content: answer });
          conversation.fingerprint = payload.fingerprint || fingerprint;
          saveLocal();
          provisional = [];
          if (payload.model_changed) {
            backendChanged = true;
            setNotice("AI 模型喺今次回覆期間更新；回覆已保留，下一步請開始新對話。");
          }
        } else if (event === "error") {
          throw new Error(payload.message || "AI 未能完成今次回覆。");
        }
      });
      if (requestGeneration !== conversationGeneration) return;
      if (!completed) throw new Error("AI 串流提早中斷。");
    } catch (error) {
      if (requestGeneration === conversationGeneration) {
        if (error.name === "AbortError") setNotice("已停止生成；未完成嘅回合冇保存到本機對話。");
        else setNotice(error.message || "AI 未能完成今次回覆。");
        provisional = [];
      }
    } finally {
      if (abortController === requestController) abortController = null;
      if (requestGeneration === conversationGeneration) renderMessages();
      updateControls();
      refreshBootstrap(false);
    }
  }

  function clearConversation(label) {
    if (!bootstrap || !confirm(`${label}？目前帳戶喺呢部裝置嘅對話會被清除，無法還原。`)) return;
    conversationGeneration += 1;
    abortController?.abort();
    localStorage.removeItem(storageKey);
    conversation = { fingerprint: bootstrap.backend_fingerprint || "", messages: [] };
    provisional = [];
    backendChanged = false;
    setNotice("");
    renderMessages();
    updateControls();
  }

  $("messageInput").addEventListener("input", () => {
    const maximum = bootstrap?.history_limits.user_message_characters || $("messageInput").maxLength;
    $("characterCount").textContent = `${$("messageInput").value.length} / ${maximum}`;
  });
  $("messageInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });
  $("sendButton").onclick = sendMessage;
  $("stopButton").onclick = () => abortController?.abort();
  $("newChat").onclick = () => clearConversation("開始新對話");
  $("deleteChat").onclick = () => clearConversation("刪除對話");
  $("loginButton").onclick = async () => {
    $("loginError").textContent = "";
    try {
      await api("/api/committee/login", {
        method: "POST",
        body: JSON.stringify({ user_id: $("loginUser").value.trim(), password: $("loginPassword").value }),
      });
      $("loginPassword").value = "";
      await refreshBootstrap();
    } catch (error) {
      $("loginError").textContent = error.message;
    }
  };

  refreshBootstrap();
  setInterval(() => { if (!abortController) refreshBootstrap(false); }, 10000);
})();
