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
  let abPendingAssignments = [];
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
    const migrated = { fast: "daily", thinking: "complex" }[value] || value;
    return ["daily", "complex", "deep"].includes(migrated)
      ? migrated
      : DEFAULT_MODE;
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
    return {
      daily: "日常預設（4B）",
      complex: "複雜問題（4B Thinking）",
      deep: "深入思考（9B Thinking）",
    }[normalizeMode(mode)];
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
    if (service.queue_length) label += `・${service.queue_length} 個工作排隊`;
    $("serviceStatus").textContent = label;
    $("statusDot").className = `dot ${service.state === "online" ? "online" : service.state === "busy" ? "busy" : "offline"}`;
    $("identityStatus").textContent = bootstrap.identity.developer
      ? "Developer 試用"
      : `已登入：${bootstrap.identity.id}`;
    $("thinkingMode").value = conversation.mode;
    Array.from($("thinkingMode").options).forEach((option) => {
      option.disabled = !fingerprintForMode(option.value);
    });
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

  function metric(label, value) {
    const card = document.createElement("div");
    card.className = "ab-metric";
    const strong = document.createElement("strong");
    strong.textContent = String(value);
    const caption = document.createElement("span");
    caption.className = "caption";
    caption.textContent = label;
    card.append(strong, caption);
    return card;
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
    $("abCaseTitle").textContent = abAssignment.title || "盲評比較";
    $("abCaseInput").textContent = formatCaseInput(abAssignment.input);
    $("abCaseInput").style.whiteSpace = "pre-wrap";
    $("abReference").textContent = abAssignment.reference_text || "";
    $("abLeftAnswer").innerHTML = SafeMarkdown.render(String(abAssignment.left_answer || ""));
    $("abRightAnswer").innerHTML = SafeMarkdown.render(String(abAssignment.right_answer || ""));
    $("abReviewForm").reset();
    $("abReviewForm").classList.toggle("hidden", Boolean(abAssignment.preview));
    if (abAssignment.preview) setAbNotice("Developer preview：呢個比較唔會建立assignment，亦唔可以提交正式票。");
  }

  function renderResults(summary) {
    const panel = $("abResults");
    panel.replaceChildren();
    panel.classList.toggle("hidden", !summary);
    if (!summary) return;
    const heading = document.createElement("h2");
    heading.textContent = "Campaign 描述性結果";
    const warning = document.createElement("p");
    warning.className = "muted";
    warning.textContent = "結果只比較三個本地模式，唔會自動宣布winner、切換production預設或成為訓練資料。";
    const table = document.createElement("table");
    table.className = "summary-table";
    const head = document.createElement("tr");
    ["模式", "整體", "粵語", "推理", "實用", "事實", "私隱安全"].forEach((value) => {
      const th = document.createElement("th"); th.textContent = value; head.append(th);
    });
    const thead = document.createElement("thead"); thead.append(head); table.append(thead);
    const tbody = document.createElement("tbody");
    const labels = { daily: "4B 日常", complex: "4B Thinking", deep: "9B Thinking" };
    Object.entries(summary.mode_scores || {}).forEach(([mode, scores]) => {
      const row = document.createElement("tr");
      [labels[mode] || mode, scores.overall, scores.cantonese, scores.reasoning, scores.usefulness, scores.factual, scores.privacy].forEach((value, index) => {
        const cell = document.createElement(index ? "td" : "th");
        cell.textContent = index ? `${(Number(value || 0) * 100).toFixed(1)}%` : value;
        row.append(cell);
      });
      tbody.append(row);
    });
    table.append(tbody);
    const details = document.createElement("details");
    const detailTitle = document.createElement("summary");
    detailTitle.textContent = "按題型分拆同 head-to-head";
    const taskTable = document.createElement("table");
    taskTable.className = "summary-table";
    const taskHead = document.createElement("tr");
    ["題型", "4B 日常", "4B Thinking", "9B Thinking"].forEach((value) => {
      const th = document.createElement("th"); th.textContent = value; taskHead.append(th);
    });
    const taskThead = document.createElement("thead"); taskThead.append(taskHead); taskTable.append(taskThead);
    const taskBody = document.createElement("tbody");
    const taskLabels = { speech_review: "發言評改", strategy: "策略", attack_defence: "攻防", mock_judgement: "模擬評判", cantonese_style: "粵語風格" };
    Object.entries(summary.task_type_scores || {}).forEach(([task, values]) => {
      const row = document.createElement("tr");
      [taskLabels[task] || task, values.daily?.overall, values.complex?.overall, values.deep?.overall].forEach((value, index) => {
        const cell = document.createElement(index ? "td" : "th");
        cell.textContent = index ? `${(Number(value || 0) * 100).toFixed(1)}%` : value;
        row.append(cell);
      });
      taskBody.append(row);
    });
    taskTable.append(taskBody);
    const pairList = document.createElement("ul");
    Object.values(summary.head_to_head || {}).forEach((pair) => {
      const item = document.createElement("li");
      item.textContent = `${labels[pair.mode_a] || pair.mode_a} 勝 ${Number(pair.mode_a_wins || 0)}；${labels[pair.mode_b] || pair.mode_b} 勝 ${Number(pair.mode_b_wins || 0)}；相若 ${Number(pair.ties || 0)}；兩個都不合格 ${Number(pair.both_bad || 0)}`;
      pairList.append(item);
    });
    details.append(detailTitle, taskTable, pairList);
    const risks = document.createElement("p");
    risks.textContent = `兩個都不合格題目：${(summary.both_bad_cases || []).join("、") || "沒有"}；私隱／安全雙失敗：${(summary.safety_failure_cases || []).join("、") || "沒有"}`;
    panel.append(heading, warning, table, risks, details);
  }

  function campaignDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-HK");
  }

  function historyAction(label, className, handler, disabled = false) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.className = className || "";
    button.disabled = disabled || abBusy;
    button.onclick = handler;
    return button;
  }

  function renderCampaignHistory() {
    const panel = $("abManagerPanel");
    const history = $("abCampaignHistory");
    const pending = $("abPendingAssignments");
    const manager = Boolean(abBootstrap?.identity?.manager);
    panel.classList.toggle("hidden", !manager);
    history.replaceChildren();
    pending.replaceChildren();
    if (!manager) return;
    const campaigns = Array.isArray(abBootstrap?.campaigns) ? abBootstrap.campaigns : [];
    if (!campaigns.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "未有 campaign。";
      history.append(empty);
    }
    campaigns.forEach((item) => {
      const row = document.createElement("div");
      row.className = "actions";
      const summary = document.createElement("span");
      summary.textContent = `${item.campaign_id}・${item.status}・${campaignDate(item.created_at)}`;
      row.append(summary);
      if (["closed", "invalidated"].includes(item.status)) {
        row.append(historyAction("下載", "", () => downloadCampaign(item.campaign_id)));
        row.append(historyAction(
          "清除", "danger", () => confirmPurgeCampaign(item.campaign_id), !item.exported_at,
        ));
      }
      history.append(row);
    });
    if (!abPendingAssignments.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "沒有未提交 reservation。";
      pending.append(empty);
      return;
    }
    abPendingAssignments.forEach((item) => {
      const row = document.createElement("div");
      row.className = "actions";
      const summary = document.createElement("span");
      summary.textContent = `${item.case_id}・${item.pair_key}・${item.expired ? "已過期" : `到期 ${campaignDate(item.expires_at)}`}`;
      row.append(summary, historyAction("釋放", "danger", () => releaseReservation(item.review_id)));
      pending.append(row);
    });
  }

  function renderAb() {
    const data = abBootstrap;
    const campaign = data?.campaign;
    const progress = data?.progress;
    const manager = Boolean(data?.identity?.manager);
    const actions = $("abManagerActions");
    actions.classList.toggle("hidden", !manager);
    $("abProgress").replaceChildren();
    $("abCreate").classList.toggle("hidden", !data?.can_create_campaign);
    $("abGenerate").classList.toggle("hidden", campaign?.status !== "generating");
    $("abOpenReview").classList.toggle("hidden", campaign?.status !== "generating");
    $("abClose").classList.toggle("hidden", campaign?.status !== "reviewing");
    $("abInvalidate").classList.toggle("hidden", !campaign || campaign.status === "invalidated");
    const terminal = ["closed", "invalidated"].includes(campaign?.status);
    $("abExport").classList.toggle("hidden", !terminal);
    $("abPurge").classList.toggle("hidden", !terminal || !data?.manager?.exported_at);
    ["abCreate", "abGenerate", "abOpenReview", "abClose", "abInvalidate", "abExport", "abPurge"].forEach((id) => { $(id).disabled = abBusy; });
    renderCampaignHistory();
    if (!campaign) {
      $("abStatus").textContent = manager ? "未有 campaign，可以建立固定30題三模式測試。" : "暫未有開放中嘅 A/B Test。";
      renderAssignment(null); renderResults(null); return;
    }
    const labels = { generating: "答案準備中", reviewing: "盲評進行中", closed: "已完成", invalidated: "已作廢" };
    $("abStatus").textContent = labels[campaign.status] || campaign.status;
    if (campaign.status === "invalidated" && campaign.invalidation_reason) {
      setAbNotice(data.manager?.invalidation_reason || campaign.invalidation_reason);
    }
    if (progress) {
      $("abProgress").append(
        metric("已成功固定答案", `${Number(progress.generation?.succeeded || 0)} / 90`),
        metric("正式盲評票", `${Number(progress.quorum?.submitted || 0)} / 270`),
        metric("你已完成", Number(progress.reviewer_completed || 0)),
      );
    }
    renderResults(campaign.status === "closed" && manager ? data.manager?.summary : null);
    if (campaign.status !== "reviewing") renderAssignment(null);
  }

  async function refreshAb(loadAssignment = true) {
    const generation = ++abGeneration;
    try {
      const data = await api("/api/lmc-ai/ab-tests/bootstrap", { method: "GET" });
      if (generation !== abGeneration) return;
      abBootstrap = data;
      abPendingAssignments = [];
      if (data.identity.manager && data.campaign?.status === "reviewing") {
        const reservations = await api(`/api/lmc-ai/ab-tests/campaigns/${encodeURIComponent(data.campaign.campaign_id)}/assignments`, { method: "GET" });
        if (generation !== abGeneration) return;
        abPendingAssignments = Array.isArray(reservations.assignments) ? reservations.assignments : [];
      }
      renderAb();
      const campaign = data.campaign;
      if (loadAssignment && campaign?.status === "reviewing" && (data.identity.can_review || data.identity.manager)) {
        const next = await api(`/api/lmc-ai/ab-tests/campaigns/${encodeURIComponent(campaign.campaign_id)}/reviews/next`, { method: "GET" });
        if (generation !== abGeneration || abBootstrap?.campaign?.campaign_id !== campaign.campaign_id) return;
        renderAssignment(next.assignment);
      }
    } catch (error) {
      if (generation === abGeneration) setAbNotice(error.message || "未能載入 A/B Test。 ");
    }
  }

  async function abAction(path, options = {}) {
    if (abBusy) return;
    abBusy = true; setAbNotice(""); renderAb();
    try {
      const result = await api(path, { method: "POST", body: options.body ? JSON.stringify(options.body) : undefined });
      if (result.error) setAbNotice(result.error);
      await refreshAb();
    } catch (error) {
      setAbNotice(error.message || "A/B Test 動作失敗。");
    } finally {
      abBusy = false; renderAb();
    }
  }

  async function downloadCampaign(campaignId) {
    if (abBusy) return;
    abBusy = true; setAbNotice(""); renderAb();
    try {
      const response = await fetch(`/api/lmc-ai/ab-tests/campaigns/${encodeURIComponent(campaignId)}/export.json`, {
        method: "POST", credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      const blob = await response.blob();
      const link = document.createElement("a");
      const url = URL.createObjectURL(blob);
      link.href = url;
      link.download = `lmc-ai-eval-${campaignId}.json`;
      link.click();
      URL.revokeObjectURL(url);
      await refreshAb(false);
    } catch (error) {
      setAbNotice(error.message || "未能下載 audit JSON。");
    } finally {
      abBusy = false; renderAb();
    }
  }

  function confirmPurgeCampaign(campaignId) {
    const confirmation = prompt(`清除後只保留 audit 記錄。請輸入完整 campaign ID：\n${campaignId}`);
    if (confirmation !== campaignId) {
      if (confirmation !== null) setAbNotice("確認文字不正確，未有清除資料。");
      return;
    }
    const reason = prompt("請填寫清除原因：");
    if (reason?.trim()) {
      abAction(`/api/lmc-ai/ab-tests/campaigns/${encodeURIComponent(campaignId)}/purge`, {
        body: { confirmation, reason: reason.trim() },
      });
    }
  }

  function releaseReservation(reviewId) {
    const campaignId = abBootstrap?.campaign?.campaign_id;
    if (!campaignId) return;
    const reason = prompt("請填寫提早釋放 reservation 原因：");
    if (reason?.trim()) {
      abAction(`/api/lmc-ai/ab-tests/campaigns/${encodeURIComponent(campaignId)}/assignments/${encodeURIComponent(reviewId)}/release`, {
        body: { reason: reason.trim() },
      });
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
  $("abCreate").onclick = () => abAction("/api/lmc-ai/ab-tests/campaigns", { body: { note: "" } });
  $("abGenerate").onclick = () => abAction(`/api/lmc-ai/ab-tests/campaigns/${encodeURIComponent(abBootstrap.campaign.campaign_id)}/generate-next`);
  $("abOpenReview").onclick = () => abAction(`/api/lmc-ai/ab-tests/campaigns/${encodeURIComponent(abBootstrap.campaign.campaign_id)}/open-review`);
  $("abClose").onclick = () => abAction(`/api/lmc-ai/ab-tests/campaigns/${encodeURIComponent(abBootstrap.campaign.campaign_id)}/close`);
  $("abInvalidate").onclick = () => {
    const reason = prompt("請填寫invalidate原因（資料會保留，不會刪除）：");
    if (reason?.trim()) abAction(`/api/lmc-ai/ab-tests/campaigns/${encodeURIComponent(abBootstrap.campaign.campaign_id)}/invalidate`, { body: { reason: reason.trim() } });
  };
  $("abExport").onclick = () => downloadCampaign(abBootstrap.campaign.campaign_id);
  $("abPurge").onclick = () => confirmPurgeCampaign(abBootstrap.campaign.campaign_id);
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
      setAbNotice(error.message || "未能提交盲評。");
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
