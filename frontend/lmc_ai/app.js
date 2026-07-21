(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const DEFAULT_MODE = "daily";
  let bootstrap = null;
  let storageKey = "";
  let conversation = { fingerprint: "", mode: DEFAULT_MODE, messages: [] };
  let abortController = null;
  let provisional = [];
  let backendChanged = false;
  let pollGeneration = 0;
  let conversationGeneration = 0;
  let abBootstrap = null;
  let abAssignment = null;
  let abGeneration = 0;
  let abBusy = false;
  let activePanel = "chatPanel";

  const reviewQuestions = [
    ["overall", "整體偏好"],
    ["cantonese", "香港粵語自然度"],
    ["reasoning", "論證／推理"],
    ["usefulness", "具體及實用程度"],
    ["factual", "事實可靠性"],
    ["privacy", "私隱及安全"],
  ];
  const reviewChoices = [
    ["left", "答案 A 較好"], ["right", "答案 B 較好"],
    ["tie", "相若"], ["both_bad", "兩個都不合格"],
  ];

  function totalCharacters(messages) {
    return messages.reduce((sum, item) => sum + String(item.content || "").length, 0);
  }

  function localKey(identity) {
    return `lmc-ai-chat:v1:${encodeURIComponent(identity)}`;
  }

  function normalizeMode(value) {
    const migrated = { complex: "daily", thinking: "deep" }[value] || value;
    const allowed = (bootstrap?.service?.modes || []).map((item) => item.id);
    const fallback = bootstrap?.default_mode || DEFAULT_MODE;
    return (allowed.length ? allowed : ["fast", "daily", "deep"]).includes(migrated)
      ? migrated
      : fallback;
  }

  function fingerprintForMode(mode) {
    const selected = normalizeMode(mode);
    const fingerprints = bootstrap?.backend_fingerprints;
    if (fingerprints && typeof fingerprints[selected] === "string") {
      return fingerprints[selected];
    }
    return selected === DEFAULT_MODE && typeof bootstrap?.backend_fingerprint === "string"
      ? bootstrap.backend_fingerprint
      : "";
  }

  function modeLabel(mode) {
    const selected = normalizeMode(mode);
    return bootstrap?.service?.modes?.find((item) => item.id === selected)?.label
      || selected;
  }

  function renderModeOptions() {
    const select = $("thinkingMode");
    const modes = bootstrap?.service?.modes || [];
    const current = normalizeMode(conversation.mode);
    select.replaceChildren(...modes.map((mode) => {
      const option = document.createElement("option");
      option.value = mode.id;
      option.textContent = mode.label;
      option.disabled = !fingerprintForMode(mode.id);
      return option;
    }));
    conversation.mode = current;
    select.value = current;
  }

  function loadLocal() {
    conversation = { fingerprint: "", mode: DEFAULT_MODE, messages: [] };
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (value && typeof value.fingerprint === "string" && Array.isArray(value.messages)) {
        const messages = value.messages.filter((item) =>
          item && ["user", "assistant"].includes(item.role) && typeof item.content === "string",
        );
        if (messages.length <= bootstrap.history_limits.messages &&
            totalCharacters(messages) <= bootstrap.history_limits.characters) {
          let mode = normalizeMode(value.mode);
          conversation = { fingerprint: value.fingerprint, mode, messages };
        }
      }
    } catch (_) {
      conversation = { fingerprint: "", mode: DEFAULT_MODE, messages: [] };
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
    if (service.model_set_label) label += `・${service.model_set_label}`;
    if (service.queue_length) label += `・${service.queue_length} 個工作排隊`;
    $("serviceStatus").textContent = label;
    $("statusDot").className = `dot ${service.state === "online" ? "online" : service.state === "busy" ? "busy" : "offline"}`;
    $("identityStatus").textContent = bootstrap.identity.developer
      ? "Developer 試用"
      : `已登入：${bootstrap.identity.id}`;
    renderModeOptions();
    const currentFingerprint = fingerprintForMode(conversation.mode);
    backendChanged = Boolean(
      conversation.messages.length && conversation.fingerprint &&
      currentFingerprint && conversation.fingerprint !== currentFingerprint,
    );
    if (backendChanged) setNotice("AI 電腦、模型或 persona 已更新。舊對話仍可查看，但必須開始新對話先可以繼續。");
    renderNodes();
    updateControls();
  }

  function renderNodes() {
    const grid = $("nodeGrid");
    const nodes = Array.isArray(bootstrap?.nodes) ? bootstrap.nodes : [];
    grid.replaceChildren();
    if (!nodes.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "尚未登記 AI 電腦。";
      grid.append(empty);
      return;
    }
    const labels = {
      online: "在線",
      busy: "生成中",
      draining: "準備休眠／暫停接單",
      offline: "離線",
    };
    nodes.forEach((node) => {
      const card = document.createElement("article");
      card.className = "node-card";
      const title = document.createElement("h2");
      title.textContent = `${node.selected ? "★ " : ""}${node.name || "AI 電腦"}`;
      const meta = document.createElement("div");
      meta.className = "node-meta";
      const modelText = Array.isArray(node.models) && node.models.length
        ? node.models.join("、")
        : "等待新 node preflight";
      [
        `狀態：${labels[node.state] || "未知"}`,
        node.selected ? "目前選用" : "備用電腦",
        `模型：${modelText}`,
        `排隊：${Number(node.queue_length || 0)}`,
      ].forEach((text) => {
        const pill = document.createElement("span");
        pill.className = "pill";
        pill.textContent = text;
        meta.append(pill);
      });
      card.append(title, meta);
      grid.append(card);
    });
  }

  function updateControls() {
    if (!bootstrap) return;
    const running = Boolean(abortController);
    const serviceReady = ["online", "busy"].includes(bootstrap.service.state);
    const limits = bootstrap.history_limits;
    const capReached = conversation.messages.length + 2 > limits.messages ||
      totalCharacters(conversation.messages) >= limits.characters;
    const modeReady = Boolean(fingerprintForMode(conversation.mode));
    $("sendButton").disabled = running || backendChanged || !serviceReady || !modeReady || capReached;
    $("stopButton").disabled = !running;
    $("messageInput").disabled = running || backendChanged;
    $("thinkingMode").disabled = running;
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

  function setAbNotice(message) {
    $("abNotice").textContent = message || "";
    $("abNotice").classList.toggle("show", Boolean(message));
  }

  function metric(label, value, current = 0, maximum = 0) {
    const card = document.createElement("div");
    card.className = "ab-metric";
    const strong = document.createElement("strong");
    strong.textContent = String(value);
    const caption = document.createElement("span");
    caption.className = "caption";
    caption.textContent = label;
    card.append(strong, caption);
    if (maximum > 0) {
      const progress = document.createElement("progress");
      progress.max = maximum;
      progress.value = Math.min(maximum, Math.max(0, current));
      progress.setAttribute("aria-label", label);
      card.append(progress);
    }
    return card;
  }

  function renderAbEmpty(title, message, icon = "🧪") {
    $("abEmptyState").classList.remove("hidden");
    $("abEmptyState").querySelector(".empty-icon").textContent = icon;
    $("abEmptyTitle").textContent = title;
    $("abEmptyText").textContent = message;
  }

  function renderReviewQuestions() {
    const container = $("abReviewQuestions");
    container.replaceChildren();
    reviewQuestions.forEach(([name, label]) => {
      const fieldset = document.createElement("fieldset");
      fieldset.className = "review-row";
      const legend = document.createElement("legend");
      legend.textContent = label;
      const options = document.createElement("div");
      options.className = "review-options";
      reviewChoices.forEach(([value, choiceLabel]) => {
        const wrapper = document.createElement("label");
        const input = document.createElement("input");
        input.type = "radio";
        input.name = name;
        input.value = value;
        input.required = true;
        wrapper.append(input, document.createTextNode(choiceLabel));
        options.append(wrapper);
      });
      fieldset.append(legend, options);
      container.append(fieldset);
    });
  }

  function formatCaseInput(input) {
    if (!input || typeof input !== "object") return "";
    const labels = {
      topic: "辯題", side: "立場", text: "題目內容",
      opponent: "對方講法", question: "對方問題", transcript: "比賽片段",
    };
    return Object.entries(input).map(([key, value]) => `${labels[key] || key}：${String(value)}`).join("\n");
  }

  function renderAssignment(value) {
    abAssignment = value || null;
    $("abReviewCard").classList.toggle("hidden", !abAssignment);
    if (!abAssignment) return;
    $("abEmptyState").classList.add("hidden");
    const completed = Number(abBootstrap?.progress?.reviewer_completed || 0);
    const totalPairs = Number(abBootstrap?.progress?.quorum?.total_pairs || 0);
    $("abReviewCounter").textContent = abAssignment.preview
      ? "Developer 預覽"
      : `第 ${Math.min(totalPairs, completed + 1)} / ${totalPairs} 組`;
    $("abCaseTitle").textContent = abAssignment.title || "比較兩個回答";
    $("abCaseInput").textContent = formatCaseInput(abAssignment.input);
    $("abCaseInput").style.whiteSpace = "pre-wrap";
    $("abReference").textContent = abAssignment.reference_text || "";
    $("abLeftAnswer").innerHTML = SafeMarkdown.render(String(abAssignment.left_answer || ""));
    $("abRightAnswer").innerHTML = SafeMarkdown.render(String(abAssignment.right_answer || ""));
    $("abReviewForm").reset();
    $("abReviewForm").classList.toggle("hidden", Boolean(abAssignment.preview));
    if (abAssignment.preview) setAbNotice("Developer 預覽模式：呢組比較唔會記錄或計入正式結果。");
  }

  function renderAb() {
    const data = abBootstrap;
    const campaign = data?.campaign;
    const progress = data?.progress;
    $("abProgress").replaceChildren();
    if (!campaign) {
      $("abStatusBadge").textContent = "未有測試";
      $("abStatus").textContent = "暫時未有開放中嘅測試";
      $("abStatusDetail").textContent = "管理員準備好新一輪測試後，呢度就會出現匿名回答比較。";
      renderAssignment(null);
      renderAbEmpty("暫時未有內容", "稍後再返嚟，就可以幫手測試自家 AI。", "💬");
      return;
    }
    const succeeded = Number(progress?.generation?.succeeded || 0);
    const submitted = Number(progress?.quorum?.submitted || 0);
    const required = Number(progress?.quorum?.required || 0);
    const completed = Number(progress?.reviewer_completed || 0);
    const answerTotal = Number(progress?.generation?.total || 0);
    const pairTotal = Number(progress?.quorum?.total_pairs || 0);
    if (progress) {
      if (campaign.status === "generating") {
        $("abProgress").append(metric("已準備回答", `${succeeded} / ${answerTotal}`, succeeded, answerTotal));
      } else {
        $("abProgress").append(
          metric("你已回饋", `${completed} / ${pairTotal} 組`, completed, pairTotal),
          metric("全體已收集", `${submitted} / ${required} 份`, submitted, required),
        );
      }
    }
    if (campaign.status === "generating") {
      $("abStatusBadge").textContent = "準備中";
      $("abStatus").textContent = "新一輪回答準備中";
      $("abStatusDetail").textContent = "管理員正逐步準備匿名回答，全部完成後先會開放回饋。";
      renderAssignment(null);
      renderAbEmpty("回答準備中", "毋須停留喺呢頁；開放後再返嚟即可。", "⏳");
      return;
    }
    if (campaign.status === "reviewing") {
      $("abStatusBadge").textContent = "現正開放";
      $("abStatus").textContent = "可以開始測試並回饋";
      $("abStatusDetail").textContent = "每次提交一組，系統會自動顯示下一組可比較嘅回答。";
      if (!abAssignment) {
        renderAbEmpty("正在準備下一組比較…", "如果暫時未有可派發內容，稍後重新打開呢個分頁即可。", "🔎");
      }
      return;
    }
    renderAssignment(null);
    if (campaign.status === "closed") {
      $("abStatusBadge").textContent = "已完成";
      $("abStatus").textContent = "今輪測試已完成";
      $("abStatusDetail").textContent = "多謝你提供回饋，管理員會綜合所有匿名評分檢視結果。";
      renderAbEmpty("多謝你嘅回饋", completed ? `你今輪完成咗 ${completed} 組比較。` : "今輪已停止收集新回饋。", "✅");
      return;
    }
    $("abStatusBadge").textContent = "已停止";
    $("abStatus").textContent = "今輪測試已停止";
    $("abStatusDetail").textContent = "呢輪資料唔會再派發；管理員會另行準備新一輪測試。";
    renderAbEmpty("今輪不再收集回饋", "稍後有新測試時再返嚟參與。", "⏹️");
  }

  async function refreshAb(loadAssignment = true) {
    const generation = ++abGeneration;
    try {
      const data = await api("/api/lmc-ai/ab-tests/bootstrap", { method: "GET" });
      if (generation !== abGeneration) return;
      const previousCampaign = abBootstrap?.campaign?.campaign_id;
      abBootstrap = data;
      if (previousCampaign !== data.campaign?.campaign_id || data.campaign?.status !== "reviewing") {
        renderAssignment(null);
      }
      renderAb();
      const campaign = data.campaign;
      if (loadAssignment && campaign?.status === "reviewing" && (data.identity.can_review || data.identity.manager)) {
        const next = await api(`/api/lmc-ai/ab-tests/campaigns/${encodeURIComponent(campaign.campaign_id)}/reviews/next`, { method: "GET" });
        if (generation !== abGeneration || abBootstrap?.campaign?.campaign_id !== campaign.campaign_id) return;
        renderAssignment(next.assignment);
        if (!next.assignment) {
          const completed = Number(abBootstrap?.progress?.reviewer_completed || 0);
          const totalPairs = Number(abBootstrap?.progress?.quorum?.total_pairs || 0);
          renderAbEmpty(
            totalPairs > 0 && completed >= totalPairs ? "你已完成今輪全部比較" : "暫時未有可派發嘅比較",
            totalPairs > 0 && completed >= totalPairs ? `多謝你完成 ${totalPairs} 組回饋。` : "其他評審可能正處理餘下組合，稍後再返嚟即可。",
            totalPairs > 0 && completed >= totalPairs ? "🎉" : "⏳",
          );
        }
      }
    } catch (error) {
      if (generation === abGeneration) {
        renderAssignment(null);
        renderAbEmpty("暫時載入唔到測試", "請稍後再試；如問題持續，請通知管理員。", "⚠️");
        setAbNotice(error.message || "未能載入測試並回饋。");
      }
    }
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
      if (activePanel === "abTestPanel" && !abBusy) refreshAb(false);
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
    const fingerprint = conversation.messages.length
      ? conversation.fingerprint
      : fingerprintForMode(conversation.mode);
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
          mode: conversation.mode,
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
    conversation = {
      fingerprint: fingerprintForMode(conversation.mode),
      mode: conversation.mode,
      messages: [],
    };
    saveLocal();
    provisional = [];
    backendChanged = false;
    setNotice("");
    renderMessages();
    updateControls();
  }

  function switchConversationMode() {
    const nextMode = normalizeMode($("thinkingMode").value);
    const previousMode = conversation.mode;
    if (nextMode === previousMode) return;
    if (conversation.messages.length && !confirm(
      `切換回答模式至「${modeLabel(nextMode)}」？目前對話會被清除並開始新對話，無法還原。`,
    )) {
      $("thinkingMode").value = previousMode;
      return;
    }
    conversationGeneration += 1;
    abortController?.abort();
    conversation = {
      fingerprint: fingerprintForMode(nextMode),
      mode: nextMode,
      messages: [],
    };
    provisional = [];
    backendChanged = false;
    saveLocal();
    setNotice(`已切換至「${modeLabel(nextMode)}」，並開始新對話。`);
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
  $("thinkingMode").onchange = switchConversationMode;
  function selectPanel(button) {
      activePanel = button.dataset.panel;
      document.querySelectorAll("[data-panel]").forEach((item) => {
        item.classList.remove("active");
        item.setAttribute("aria-selected", "false");
        item.tabIndex = -1;
      });
      document.querySelectorAll(".panel").forEach((panel) => panel.classList.add("hidden"));
      button.classList.add("active");
      button.setAttribute("aria-selected", "true");
      button.tabIndex = 0;
      $(button.dataset.panel).classList.remove("hidden");
      $("composer").classList.toggle("hidden", button.dataset.panel !== "chatPanel");
      if (button.dataset.panel === "abTestPanel") refreshAb();
  }
  document.querySelectorAll("[data-panel]").forEach((button) => {
    button.addEventListener("click", () => selectPanel(button));
    button.addEventListener("keydown", (event) => {
      const tabs = Array.from(document.querySelectorAll("[role=tab]"));
      const index = tabs.indexOf(button);
      let target = -1;
      if (event.key === "ArrowRight") target = (index + 1) % tabs.length;
      if (event.key === "ArrowLeft") target = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") target = 0;
      if (event.key === "End") target = tabs.length - 1;
      if (target >= 0) { event.preventDefault(); tabs[target].focus(); selectPanel(tabs[target]); }
    });
  });
  renderReviewQuestions();
  $("abReviewForm").onsubmit = async (event) => {
    event.preventDefault();
    if (!abAssignment || abBusy) return;
    const form = new FormData(event.currentTarget);
    const body = { note: $("abReviewNote").value };
    reviewQuestions.forEach(([name]) => { body[name] = form.get(name); });
    abBusy = true; $("abSubmitReview").disabled = true; setAbNotice("");
    try {
      await api(`/api/lmc-ai/ab-tests/reviews/${encodeURIComponent(abAssignment.review_id)}`, { method: "POST", body: JSON.stringify(body) });
      renderAssignment(null);
      await refreshAb();
    } catch (error) {
      setAbNotice(error.message || "未能提交回饋。");
    } finally {
      abBusy = false; $("abSubmitReview").disabled = false; renderAb();
    }
  };
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
