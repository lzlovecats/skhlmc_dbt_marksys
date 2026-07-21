/* Direct-HTML parity behaviour for AI Coach. */
(() => {
  if (location.pathname !== "/ai-coach") return;

  const $ = (id) => document.getElementById(id);
  const toast = (message) => VoteUI.toast($("toast"), message);
  const busy = (value) => VoteUI.setBusy($("busy"), value);
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (character) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[character],
    );
  const api = async (url, options = {}) => {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) throw new Error("未登入");
    if (!response.ok) {
      const error = new Error(data.detail || "操作失敗");
      error.status = response.status;
      throw error;
    }
    return data;
  };

  let meta;
  let selectedMatchId = "";
  let recorder = null;
  let audioIntentId = "";
  let audioMime = "audio/webm";
  let audioDurationSeconds = 0;
  let reviewAttempt = "initial";
  let previousReviewMarkdown = "";
  let reviewContextSnapshot = "";
  let retakeConsumed = false;
  let pendingRoomCode = "";
  let pendingRoomSuccess = "";
  let roomHandshakeTimer = 0;
  let roomPhase = "";
  let roomLeaveToken = "";
  const pendingPrepOperations = new Map();

  const modelByLabel = (label) =>
    meta?.models.find((model) => model.label === label);
  const currentModel = () => modelByLabel($("globalModel").value);
  const localModeStatus = (mode = $("localMode").value) =>
    meta?.local_ai?.modes?.find((item) => item.id === mode) || null;
  const selectedModelStatus = () => {
    const model = currentModel();
    if (!model) return { available: false, message: "未能載入 AI 模型設定。" };
    if (!model.local_node) {
      return {
        available: Boolean(model.available),
        message: model.available ? "" : `未設定 ${model.api_key_name}。`,
      };
    }
    if (!meta?.local_ai?.available) {
      return {
        available: false,
        message: meta?.local_ai?.message || "自家 AI 暫時未準備好。",
      };
    }
    const mode = localModeStatus();
    return {
      available: Boolean(mode?.available),
      message: mode?.message || "所選自家 AI 回答模式暫時不可用。",
    };
  };
  window.AICoachSelectedModelStatus = selectedModelStatus;
  const effectiveSearchModel = (model) => {
    if (model?.supports_web_search) return model;
    if (model?.local_node) return null;
    const fallback = modelByLabel(meta?.external_default_model);
    return fallback?.supports_web_search ? fallback : null;
  };
  const hkd = (amount) => `HKD ${Number(amount || 0).toFixed(4)}`;
  const estimateText = (estimate) =>
    `US$${Number(estimate?.usd || 0).toFixed(4)} ≈ ${hkd(estimate?.hkd)} / 次`;
  const download = (name, text) => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(
      new Blob([text], { type: "text/plain;charset=utf-8" }),
    );
    link.download = name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  };

  function hasUnendedRoom() {
    return Boolean(
      (pendingRoomCode || sessionStorage.aiRoom)
      && roomPhase !== "ended"
    );
  }

  function showPane(name, options = {}) {
    if (name !== "room" && !options.force && hasUnendedRoom()) {
      toast("⚠️ 連線房間仍在進行；請先返回房間並按「離開房間」，或由主持結束房間。");
      return false;
    }
    document
      .querySelectorAll(".pane, #tabs > button")
      .forEach((element) => element.classList.remove("active"));
    $(name).classList.add("active");
    document.querySelector(`[data-pane="${name}"]`).classList.add("active");
    return true;
  }

  function currentReviewContext() {
    return JSON.stringify({
      match_id: selectedMatchId,
      topic: $("reviewTopic").value.trim(),
      side: $("reviewSide").value,
      position: Number($("reviewPosition").value),
      debate_format: $("reviewFormat").value,
      review_mode: $("reviewMode").value,
    });
  }

  function setReviewContextLocked(locked) {
    [
      $("reviewTopic"),
      $("reviewSide"),
      $("reviewPosition"),
      $("reviewFormat"),
      $("reviewMode"),
      $("globalModel"),
      $("localMode"),
    ].forEach((element) => {
      element.disabled = locked;
    });
    $("reviewForm")
      .querySelectorAll(".topic-source, .topic-picker")
      .forEach((element) => {
        element.disabled = locked;
      });
  }

  function clearRecordedAudio() {
    const activeRecorder = recorder;
    recorder = null;
    if (activeRecorder) {
      activeRecorder.discard = true;
      if (activeRecorder.state === "recording") activeRecorder.stop();
    }
    audioIntentId = "";
    audioMime = "audio/webm";
    audioDurationSeconds = 0;
    const preview = $("audioPreview");
    preview.pause();
    if (preview.src.startsWith("blob:")) URL.revokeObjectURL(preview.src);
    preview.removeAttribute("src");
    preview.load();
    preview.classList.add("hidden");
    $("record").textContent = "開始錄音";
    $("recordState").textContent = "未錄音";
    syncModel();
  }

  function resetReviewCycle(message = "") {
    const hadCycle = Boolean(
      previousReviewMarkdown ||
        reviewAttempt === "retake" ||
        retakeConsumed ||
        !$("retakeResult").classList.contains("hidden"),
    );
    reviewAttempt = "initial";
    previousReviewMarkdown = "";
    reviewContextSnapshot = "";
    retakeConsumed = false;
    setReviewContextLocked(false);
    $("reviewSubmit").textContent = "分析發言";
    $("retakeOffer").classList.add("hidden");
    $("retakeActive").classList.add("hidden");
    $("retakeResultTitle").classList.add("hidden");
    $("retakeResult").classList.add("hidden");
    $("retakeResult").innerHTML = "";
    if (hadCycle) {
      $("reviewResult").classList.add("caption");
      $("reviewResult").textContent =
        message || "內容已更改，請重新分析發言。";
      $("downloadReview").classList.add("hidden");
    }
    clearRecordedAudio();
  }

  function topicSource(container) {
    const target = $(container.dataset.topicTarget);
    const source = container.dataset.topicSource;
    const supportsMatches = source === "matches";
    container.innerHTML = `<div class="field-grid"><label>辯題來源<select class="topic-source">
      <option>手動輸入</option><option>${supportsMatches ? "從系統場次載入" : "從辯題庫選擇"}</option>
      </select></label><label class="topic-picker-wrap hidden">${supportsMatches ? "選擇場次" : "選擇辯題"}<select class="topic-picker"></select></label></div><p class="caption topic-note"></p>`;
    const mode = container.querySelector(".topic-source");
    const pickerWrap = container.querySelector(".topic-picker-wrap");
    const picker = container.querySelector(".topic-picker");
    const note = container.querySelector(".topic-note");
    const targetWrap = target.closest("label");

    const choose = () => {
      if (supportsMatches)
        resetReviewCycle("場次已更改，請重新分析發言。");
      const rows = supportsMatches ? meta.matches : meta.topics;
      const row = rows[Number(picker.value)];
      if (!row) return;
      target.value = row.topic_text || "";
      if (supportsMatches) {
        selectedMatchId = String(row.match_id || "");
        note.textContent = `辯題：${row.topic_text || "未設定"}｜正方：${row.pro_team || "—"}｜反方：${row.con_team || "—"}`;
      } else {
        note.textContent = `類別：${row.category || "—"}｜難度：${difficultyLabel(row.difficulty)}`;
      }
    };
    picker.addEventListener("change", choose);
    mode.addEventListener("change", () => {
      if (supportsMatches)
        resetReviewCycle("辯題來源已更改，請重新分析發言。");
      const manual = mode.value === "手動輸入";
      pickerWrap.classList.toggle("hidden", manual);
      targetWrap.classList.toggle("hidden", !manual);
      note.textContent = "";
      if (supportsMatches) selectedMatchId = "";
      if (manual) {
        target.value = "";
        syncReviewPositions();
        return;
      }
      const rows = supportsMatches ? meta.matches : meta.topics;
      if (!rows.length) {
        mode.value = "手動輸入";
        pickerWrap.classList.add("hidden");
        targetWrap.classList.remove("hidden");
        note.textContent = supportsMatches
          ? "目前沒有比賽場次，請手動輸入。"
          : "辯題庫為空，請手動輸入辯題。";
        return;
      }
      picker.innerHTML = rows
        .map(
          (row, index) =>
            `<option value="${index}">${esc(
              supportsMatches
                ? `${row.match_id}｜${row.topic_text || "未設定"}`
                : `[${row.category || "未分類"}] ${row.topic_text}`,
            )}</option>`,
        )
        .join("");
      choose();
      syncReviewPositions();
    });
  }

  function difficultyLabel(value) {
    const levels = {
      1: "Lv1 — 概念日常",
      2: "Lv2 — 一般議題",
      3: "Lv3 — 進階專業",
    };
    return levels[Number(value)] || value || "—";
  }

  function syncReviewPositions() {
    if (!$("reviewPosition")) return;
    const allowThirdDeputy = $("reviewFormat").value === "聯中";
    const option = $("reviewPosition").querySelector('option[value="5"]');
    option.hidden = !allowThirdDeputy;
    option.disabled = !allowThirdDeputy;
    if (!allowThirdDeputy && $("reviewPosition").value === "5")
      $("reviewPosition").value = "1";
  }

  const oppositeSide = () =>
    $("reviewSide").value === "正方" ? "反方" : "正方";
  function renderQaFields() {
    const mode = $("reviewMode").value;
    const box = $("qaFields");
    $("reviewTextLabel").firstChild.textContent =
      mode === "台上發言" ? "輸入文字稿" : "補充文字稿（可選）";
    if (mode === "台上發言") {
      box.innerHTML = "";
    } else if (mode === "台下發問") {
      box.innerHTML = `<label>台下發問模式<select id="floorMode"><option>我問，AI 答</option><option>AI 問一條問題，我答</option></select></label><div id="floorFields"></div>`;
      $("floorMode").addEventListener("change", renderFloorFields);
      renderFloorFields();
    } else {
      box.innerHTML = `<label>交互次序<select id="exchangeOrder"><option>我問，AI 答＋問，我再答</option><option>AI 問，我答＋問，AI 再答</option></select></label><div id="exchangeFields"></div>`;
      $("exchangeOrder").addEventListener("change", renderExchangeFields);
      renderExchangeFields();
    }
  }

  function renderFloorFields() {
    if ($("floorMode").value === "我問，AI 答") {
      $("floorFields").innerHTML =
        '<label>我的問題<textarea id="floorQuestion" rows="4" placeholder="輸入你想向對方或 AI 提出的問題…"></textarea></label>';
    } else {
      $("floorFields").innerHTML =
        '<label>AI / 對方問題（可留空，AI 會先問）<textarea id="floorAiQuestion" rows="3" placeholder="如已有題目，可貼上問題；如留空，AI 會根據辯題先問一條問題。"></textarea></label><label>我的回答（如想 AI 先問，可留空）<textarea id="floorAnswer" rows="4" placeholder="輸入你對問題的回答…"></textarea></label>';
    }
  }

  function renderExchangeFields() {
    if ($("exchangeOrder").value === "我問，AI 答＋問，我再答") {
      $("exchangeFields").innerHTML =
        '<label>我的問題<textarea id="exchangeQuestion" rows="3" placeholder="輸入你想先問的問題…"></textarea></label><label>我對 AI 追問的回答（可留空，AI 會先答＋追問）<textarea id="exchangeFinalAnswer" rows="4"></textarea></label>';
    } else {
      $("exchangeFields").innerHTML =
        '<label>AI / 對方問題（可留空，AI 會先問）<textarea id="exchangeAiQuestion" rows="3"></textarea></label><label>我的回答<textarea id="exchangeAnswer" rows="4"></textarea></label><label>我的追問<textarea id="exchangeFollowUp" rows="3"></textarea></label>';
    }
  }

  function buildReviewText() {
    const mode = $("reviewMode").value;
    const speech = $("reviewText").value.trim();
    if (mode === "台上發言") return { text: speech, warning: "" };
    const lines = [`## ${mode}練習`];
    let warning = "";
    if (mode === "台下發問") {
      const floorMode = $("floorMode").value;
      lines.push(
        `模式：${floorMode}`,
        `你的角色是${oppositeSide()}辯員，請以${oppositeSide()}立場參與問答。`,
      );
      if (floorMode === "我問，AI 答") {
        const question = $("floorQuestion").value.trim();
        if (!question) warning = "請輸入你想向 AI 提出的問題。";
        lines.push(
          `我的問題：${question}`,
          "請以對方辯員身分回答這條問題，再評估問題是否清晰、尖銳及有追問空間。",
        );
      } else {
        const question = $("floorAiQuestion").value.trim();
        const answer = $("floorAnswer").value.trim();
        if (question) lines.push(`AI / 對方問題：${question}`);
        if (answer)
          lines.push(
            `我的回答：${answer}`,
            "請評估我的回答，並指出如何回答得更直接、更有防守力。",
          );
        else if (question)
          lines.push(
            "我未提供回答；請重申這條問題，提示我可以從哪些方向作答，暫時毋須評分。",
          );
        else
          lines.push(
            `我未提供回答；請以${oppositeSide()}辯員身分，根據辯題向我提出一條台下發問問題，暫時毋須評分。`,
          );
      }
    } else {
      const order = $("exchangeOrder").value;
      lines.push(
        `交互次序：${order}`,
        `你的角色是${oppositeSide()}辯員，請以${oppositeSide()}立場參與問答。`,
      );
      if (order === "我問，AI 答＋問，我再答") {
        const question = $("exchangeQuestion").value.trim();
        const answer = $("exchangeFinalAnswer").value.trim();
        if (!question) warning = "請輸入你想先向 AI 提出的問題。";
        lines.push(
          `我的問題：${question}`,
          "請以對方辯員身分回答我的問題，然後追問我一條相關問題。",
        );
        if (answer)
          lines.push(`我對追問的回答：${answer}`, "請同時評估我的提問及回答。");
      } else {
        const question = $("exchangeAiQuestion").value.trim();
        const answer = $("exchangeAnswer").value.trim();
        const followUp = $("exchangeFollowUp").value.trim();
        if (question) lines.push(`AI / 對方問題：${question}`);
        if (answer) lines.push(`我的回答：${answer}`);
        if (followUp) lines.push(`我的追問：${followUp}`);
        if (answer && followUp)
          lines.push(
            "請以對方辯員身分回答我的追問，並評估我的回答及追問質素。",
          );
        else if (question && !answer)
          warning = "已有對方問題，請輸入你的回答。";
        else
          lines.push(
            `我未完成回答及追問；請以${oppositeSide()}辯員身分，根據辯題向我提出一條交互答問問題，暫時毋須評分。`,
          );
      }
    }
    if (speech) lines.unshift(speech, "");
    return { text: lines.join("\n"), warning };
  }

  async function recordAudio() {
    try {
      if (recorder) return recorder.stop();
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chunks = [];
      // Speech-quality mono Opus without an application-defined duration or
      // byte quota. Provider and system-wide storage/network boundaries remain.
      const activeRecorder = new MediaRecorder(stream, {
        audioBitsPerSecond: 32_000,
      });
      recorder = activeRecorder;
      activeRecorder.startedAt = Date.now();
      activeRecorder.ondataavailable = (event) => {
        if (!event.data?.size) return;
        chunks.push(event.data);
      };
      activeRecorder.onstop = async () => {
        const blob = new Blob(chunks, {
          type: activeRecorder.mimeType || "audio/webm",
        });
        const duration = (Date.now() - activeRecorder.startedAt) / 1000;
        stream.getTracks().forEach((track) => track.stop());
        if (recorder === activeRecorder) recorder = null;
        if (activeRecorder.discard) return;
        if (duration < 1 || !blob.size) {
          audioIntentId = "";
          audioDurationSeconds = 0;
          $("record").textContent = "重新錄音";
          $("recordState").textContent = "錄音無效";
          toast(
            "⚠️ 錄音必須至少 1 秒。",
          );
          return;
        }
        audioMime = activeRecorder.mimeType || "audio/webm";
        const bytes = new Uint8Array(await blob.arrayBuffer());
        const digest = Array.from(
          new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
        ).map((value) => value.toString(16).padStart(2, "0")).join("");
        audioDurationSeconds = duration;
        const preview = $("audioPreview");
        if (preview.src.startsWith("blob:")) URL.revokeObjectURL(preview.src);
        preview.src = URL.createObjectURL(blob);
        preview.classList.remove("hidden");
        $("recordState").textContent = "正在直傳 R2…";
        $("record").textContent = "重新錄音";
        try {
          const intent = await api("/api/ai-coach/recording-intent", {
            method: "POST",
            body: JSON.stringify({
              mime_type: audioMime,
              byte_size: blob.size,
              sha256: digest,
            }),
          });
          const uploaded = await fetch(intent.upload.url, {
            method: "PUT",
            headers: intent.upload.headers,
            body: blob,
          });
          if (!uploaded.ok) throw new Error(`R2 HTTP ${uploaded.status}`);
          await api("/api/ai-coach/recording-complete", {
            method: "POST",
            body: JSON.stringify({ intent_id: intent.intent_id }),
          });
          audioIntentId = intent.intent_id;
          $("recordState").textContent = "已錄音並安全上載，可分析";
          syncModel();
        } catch (error) {
          audioIntentId = "";
          $("recordState").textContent = "錄音上載失敗";
          toast(`⚠️ ${error.message}`);
        }
      };
      activeRecorder.start(1000);
      $("record").textContent = "停止錄音";
      $("recordState").textContent = "錄音中…";
    } catch (error) {
      toast(`⚠️ 未能使用咪高峰：${error.message}`);
    }
  }

  async function run(feature, payload, outputId, downloadId) {
    const selectedStatus = selectedModelStatus();
    if (!selectedStatus.available) {
      toast(`⚠️ ${selectedStatus.message}`);
      return null;
    }
    const prepProjectId = (
      ["strategy", "speech_review"].includes(feature)
      ? Number($("prepProjectId")?.value) : 0
    );
    const prepManuscriptId = Number($("prepManuscriptId")?.value) || null;
    const operationKey = prepProjectId
      ? `${prepProjectId}:${feature}:${prepManuscriptId || 0}:${payload.review_attempt || "initial"}`
      : "";
    const operationId = operationKey
      ? pendingPrepOperations.get(operationKey) || `prep-${crypto.randomUUID()}`
      : "";
    if (operationKey) pendingPrepOperations.set(operationKey, operationId);
    busy(true);
    try {
      const data = await api("/api/ai-coach/run", {
        method: "POST",
        body: JSON.stringify({
          ...payload,
          feature,
          model_label: $("globalModel").value,
          local_mode: $("localMode").value,
          ...(
            ["strategy", "speech_review"].includes(feature)
            && prepProjectId
              ? {
                  prep_project_id: prepProjectId,
                  prep_manuscript_id: prepManuscriptId,
                  operation_id: operationId,
                }
              : {}
          ),
        }),
      });
      if (operationKey) pendingPrepOperations.delete(operationKey);
      $(outputId).classList.remove("caption", "hidden");
      $(outputId).innerHTML = SafeMarkdown.render(data.markdown);
      $(downloadId).classList.remove("hidden");
      toast("✅ AI 分析完成。");
      document.dispatchEvent(new CustomEvent("competition-prep-ai-complete"));
      return data;
    } catch (error) {
      toast(`⚠️ ${error.message}`);
      return null;
    } finally {
      busy(false);
    }
  }

  function syncModel() {
    const model = currentModel();
    if (!model) return;
    $("localModeWrap").classList.toggle("hidden", !model.local_node);
    const localOption = Array.from($("globalModel").options).find(
      (option) => modelByLabel(option.value)?.local_node,
    );
    if (localOption) localOption.disabled = !meta?.local_ai?.available;
    Array.from($("localMode").options).forEach((option) => {
      option.disabled = !Boolean(
        meta?.local_ai?.modes?.find((item) => item.id === option.value)?.available,
      );
    });
    const selectedStatus = selectedModelStatus();
    const localStatus = meta?.local_ai || {};
    const unavailableModeMessages = Array.from(new Set(
      (localStatus.modes || [])
        .filter((item) => !item.available && item.message !== localStatus.message)
        .map((item) => item.message)
        .filter(Boolean),
    ));
    $("localAiStatus").textContent = [
      `自家 AI：${localStatus.message || "正在檢查狀態…"}`,
      ...unavailableModeMessages,
    ].join(" ");
    $("localAiStatus").classList.toggle("warn", !localStatus.available);
    const searchModel = effectiveSearchModel(model);
    const searchReady = Boolean(searchModel?.available);
    const searchEstimate = (feature) =>
      searchModel
        ? estimateText(searchModel.estimates[feature])
        : "暫時未能估算";
    $("modelNote").textContent =
      `收費狀態：${model.pricing_label}。${model.note}`;
    $("modelEstimate").textContent =
      `主線策劃 ${estimateText(model.estimates.strategy)}；發言分析 ${estimateText(model.estimates.speech_review)}；搵料及 Fact Check ${searchModel ? `會使用 ${searchModel.label}，已包括一次搜尋工具，估算為 ${searchEstimate("web_research")}` : "因搜尋模型不可用而暫停"}。`;
    $("strategyEstimate").textContent =
      `估算成本：${estimateText(model.estimates.strategy)}。`;
    const reviewEstimate = audioIntentId
      ? model.estimates.speech_review_audio
      : model.estimates.speech_review;
    $("reviewEstimate").textContent =
      `估算成本：${estimateText(reviewEstimate)}。`;
    $("retakeEstimate").textContent = model.supports_audio
      ? `改進檢查會另作一次錄音分析，估算成本：${estimateText(model.estimates.speech_review_audio)}。`
      : "改進檢查必須選用支援錄音分析的模型，並會另作一次 AI 分析。";
    $("researchEstimate").textContent =
      `估算成本：${searchEstimate("web_research")}，${searchModel ? `按 ${searchModel.label} 及 1 次搜尋工具估算` : "搜尋模型暫時不可用"}。`;
    $("factEstimate").textContent =
      `估算成本：${searchEstimate("fact_check")}，${searchModel ? `按 ${searchModel.label} 及 1 次搜尋工具估算` : "搜尋模型暫時不可用"}。`;
    const warnings = [];
    if (model.is_premium)
      warnings.push("你正在使用高級模型。請確保不要濫用，避免資金用盡。");
    if (!model.available)
      warnings.push(`未設定 ${model.api_key_name}，此模型暫時未能使用。`);
    if (model.local_node && !selectedStatus.available)
      warnings.push(selectedStatus.message);
    if (meta.fund.balance_hkd < meta.fund.low_balance_hkd)
      warnings.push(
        `AI基金餘額偏低：HKD ${meta.fund.balance_hkd.toFixed(2)}。建議新增資金。`,
      );
    $("modelWarnings").innerHTML = warnings
      .map((text) => `<div class="notice warn">⚠️ ${esc(text)}</div>`)
      .join("");
    let reviewWarning = $("reviewForm").querySelector(".review-model-warning");
    if (!reviewWarning) {
      reviewWarning = document.createElement("div");
      reviewWarning.className = "review-model-warning";
      $("reviewForm").querySelector("h2").after(reviewWarning);
    }
    reviewWarning.innerHTML = model.supports_audio
      ? ""
      : '<div class="notice warn">⚠️ 此模型不支援錄音分析。如需錄音分析，請選擇支援錄音的模型（如 Gemini 系列）。</div>';
    const searchWarning = model.supports_web_search
      ? ""
      : searchModel
        ? `<div class="notice warn">⚠️ ${esc(model.label)} 不支援上網搜尋；搵料及 Fact Check 會自動改用 ${esc(searchModel.label)}。下列估算已按替代模型顯示。</div>`
        : model.local_node
          ? '<div class="notice warn">⚠️ 自家 AI 暫未支援上網搜尋，亦不會自動轉用雲端；請手動改選支援搜尋的 Gemini 模型。</div>'
          : '<div class="notice warn">⚠️ 此不支援上網搜尋，而預設搜尋模型亦不可用。請改選支援搜尋的模型。</div>';
    const searchAvailabilityWarning = searchModel?.available
      ? ""
      : searchModel
        ? `<div class="notice warn">⚠️ 未設定 ${esc(searchModel.api_key_name)}，${esc(searchModel.label)} 暫時未能執行搜尋。</div>`
        : "";
    $("researchWarning").innerHTML =
      searchWarning + searchAvailabilityWarning;
    $("factWarning").innerHTML =
      searchWarning + searchAvailabilityWarning;
    const liveProviderWarning = model.label.startsWith("Gemini")
      ? ""
      : model.local_node
        ? `<div class="notice warn">⚠️ 目前模型為 ${esc(model.label)}，不支援 Live，亦不會自動轉雲端；請手動改選支援搜尋的 Gemini 模型。</div>`
        : `<div class="notice warn">⚠️ 目前模型為 ${esc(model.label)}，不支援 Live；開始時會改用 Gemini Live。</div>`;
    const liveResearchWarning = model.supports_web_search
      ? searchAvailabilityWarning
      : searchModel
        ? `<div class="notice warn">⚠️ Live 賽前搵料會自動改用 ${esc(searchModel.label)}，估算成本為 ${esc(searchEstimate("web_research"))}。${searchModel.available ? "" : `未設定 ${esc(searchModel.api_key_name)}，相關練習暫時不可開始。`}</div>`
        : '<div class="notice warn">⚠️ Live 賽前資料搜集所需的搜尋模型不可用，相關練習暫時不可開始。</div>';
    const ttsWarning = meta.azure_tts
      ? ""
      : meta.server_tts_configured
        ? '<div class="notice warn">⚠️ 本月 Render 傳輸量已達 3.5GB，Solo server TTS 已停用；AI 會直接播放 Gemini 原生聲音。</div>'
        : '<div class="notice warn">⚠️ 未設定 Azure／custom server TTS，AI 讀音會 fallback 用 Gemini Live 原生聲音。</div>';
    const country = meta.country_status || {
      status: "unknown",
      supported: true,
    };
    const countryWarning =
      country.status === "blocked"
        ? `<div class="notice warn">⚠️ ${esc(country.message || "請先連接至 Google 支援地區網絡／VPN，再按重新檢查。")}
          <button id="liveCountryRetry" type="button">重新檢查</button></div>`
        : country.status === "unknown"
          ? `<div class="notice">ℹ️ ${esc(country.message || "地區資料不明，系統會允許嘗試直接連接 Google。")}</div>`
          : "";
    $("liveWarnings").innerHTML =
      liveProviderWarning + liveResearchWarning + ttsWarning + countryWarning;
    $("mockWarnings").innerHTML =
      liveProviderWarning + liveResearchWarning + ttsWarning +
      (country.status === "blocked"
        ? `<div class="notice warn">⚠️ ${esc(country.message || "請先連接至 Google 支援地區網絡／VPN，再按重新檢查。")}
          <button id="mockCountryRetry" type="button">重新檢查</button></div>`
        : country.status === "unknown"
          ? `<div class="notice">ℹ️ ${esc(country.message || "地區資料不明，系統會允許嘗試直接連接 Google。")}</div>`
          : "");
    [$("liveForm"), $("mockForm")].forEach((form) => {
      const submit = form.querySelector("button.primary");
      if (submit) submit.disabled = !country.supported || !searchReady;
    });
    [$("researchForm"), $("factForm")].forEach((form) => {
      const submit = form.querySelector("button.primary");
      if (submit) submit.disabled = !searchReady;
    });
    [$("strategyForm").querySelector("button.primary"), $("reviewSubmit")]
      .forEach((button) => {
        if (button) button.disabled = model.local_node && !selectedStatus.available;
      });
    document.dispatchEvent(new CustomEvent("ai-coach-model-status"));
    $("liveCountryRetry")?.addEventListener("click", () => location.reload());
    $("mockCountryRetry")?.addEventListener("click", () => location.reload());
  }

  async function prepareLive(mode, topic, side, format, minutes) {
    busy(true);
    toast("🔎 AI 正在賽前搵料，準備攻防…");
    try {
      const data = await api("/api/ai-coach/prepare-live", {
        method: "POST",
        body: JSON.stringify({
          mode,
          topic,
          side,
          debate_format: format,
          model_label: $("globalModel").value,
          local_mode: $("localMode").value,
        }),
      });
      location.href =
        "/practice/ai-debate/live?" +
        new URLSearchParams({
          mode,
          topic,
          side,
          format,
          minutes,
          brief_id: data.brief_id,
          practice_id: data.practice_id,
          source: "coach",
        });
    } catch (error) {
      toast(`⚠️ ${error.message}`);
      busy(false);
    }
  }

  function syncLive() {
    const linked = $("liveFormat").value === "聯中";
    $("liveMinutesWrap").classList.toggle("hidden", !linked);
    $("liveFixed").classList.toggle("hidden", linked);
    const minutes = linked ? Number($("liveMinutes").value) : 2.5;
    $("liveEstimate").textContent =
      `Live token 時長約 ${Math.max(3, Math.ceil(minutes * 2 + 2))} 分鐘；實際成本會記錄到 AI基金。`;
  }

  let mockPlanRequest = 0;
  async function syncMock() {
    const linked = $("mockFormat").value === "聯中";
    $("mockMinutesWrap").classList.toggle("hidden", !linked);
    const requestId = ++mockPlanRequest;
    try {
      const plan = await api(
        `/api/ai-coach/mock-plan?format=${encodeURIComponent($("mockFormat").value)}&minutes=${encodeURIComponent($("mockMinutes").value)}`,
      );
      if (requestId !== mockPlanRequest) return;
      $("mockInfo").textContent =
        `Mock 流程（${$("mockFormat").value}）：共 ${plan.segments.length} 段，全長約 ${plan.total_minutes.toFixed(0)} 分鐘，分 ${plan.session_count} 節連線（每節 ≤ 15 分鐘，自動接力）。各段會按照賽制鳴鐘。`;
      $("mockSequence").innerHTML = plan.segments
        .map((segment) => `<li>${esc(segment.label)}</li>`)
        .join("");
      $("mockEstimate").textContent =
        `Live 用量按全長約 ${plan.total_minutes.toFixed(0)} 分鐘、分 ${plan.session_count} 節逐節記錄。`;
    } catch (error) {
      toast(`⚠️ 未能載入 Mock 流程：${error.message}`);
    }
  }

  function syncRoom() {
    const structure = $("roomStructure").value;
    const free = structure === "free";
    $("roomFormat")
      .querySelectorAll("option")
      .forEach((option) => {
        option.hidden = free && ["星島", "基本法盃"].includes(option.value);
      });
    if (free && ["星島", "基本法盃"].includes($("roomFormat").value))
      $("roomFormat").value = "校園隨想";
    const format = $("roomFormat").value;
    const adjustable = format === "聯中";
    $("roomMinutesWrap").classList.toggle("hidden", !adjustable);
    $("roomMinutesWrap").firstChild.textContent =
      structure === "mock"
        ? "Mock 自由辯論每邊時間（分鐘）"
        : "自由辯論每邊時間（分鐘）";
    $("roomMinutes").min = structure === "mock" ? "2" : ".5";
    $("roomSideLabel").firstChild.textContent = "你的立場";
    $("roomModeInfo").textContent =
      "真人對真人（1 對 1）：只會在測試完成，而且伺服器確認輪到你發言後開啟麥克風；自由辯論由正方開始，每次停止收音後嚴格按正反方交替，一方用盡時間後才會跳過該方。主持人可以在比賽結束後按下按鈕要求 AI 評判。";
    $("roomTimeNote").textContent =
      !adjustable && free ? "校園隨想自由辯論為每邊 2:30。" : "";
    const minimum = structure === "mock" ? 2 : 0.5;
    const maximum = Number($("roomMinutes").max || 10);
    const current = Number($("roomMinutes").value);
    if (!Number.isFinite(current) || current < minimum)
      $("roomMinutes").value = String(minimum);
    else if (current > maximum)
      $("roomMinutes").value = String(maximum);
  }

  function switchRoomAction(action) {
    const create = action === "create";
    $("createRoom").classList.toggle("hidden", !create);
    $("joinRoom").classList.toggle("hidden", create);
    $("showCreate").classList.toggle("active", create);
    $("showJoin").classList.toggle("active", !create);
  }
  function clearRoomHandshake() {
    if (roomHandshakeTimer) clearTimeout(roomHandshakeTimer);
    roomHandshakeTimer = 0;
  }

  function resetRoomUi(clearStored = true) {
    clearRoomHandshake();
    pendingRoomCode = "";
    pendingRoomSuccess = "";
    roomPhase = "";
    roomLeaveToken = "";
    if (clearStored) {
      sessionStorage.removeItem("aiRoom");
      sessionStorage.removeItem("aiRoomMode");
    }
    $("roomFrame").src = "about:blank";
    $("retryRoom").classList.add("hidden");
    $("activeRoom").classList.add("hidden");
    $("roomSetup").classList.remove("hidden");
  }

  function roomOpen(code, options = {}) {
    const normalized = String(code || "").trim().toUpperCase();
    pendingRoomCode = normalized;
    pendingRoomSuccess = String(options.successMessage || "");
    roomPhase = String(options.initialPhase || "pending");
    roomLeaveToken = "";
    // Persist the authoritative create/info result before the iframe handshake
    // so a refresh during a slow control connection can restore the room.
    sessionStorage.aiRoom = normalized;
    sessionStorage.aiRoomMode = "A";
    $("roomCode").textContent = normalized;
    $("activeRoomNote").textContent = "正在確認房間連線…";
    $("retryRoom").classList.add("hidden");
    showPane("room", { force: true });
    $("roomFrame").src =
      `/ai-coach/room/${encodeURIComponent(normalized)}?embedded=1&attempt=${Date.now()}`;
    $("activeRoom").classList.remove("hidden");
    $("roomSetup").classList.add("hidden");
    clearRoomHandshake();
    roomHandshakeTimer = setTimeout(() => {
      if (pendingRoomCode !== normalized) return;
      roomHandshakeTimer = 0;
      $("activeRoomNote").textContent =
        "房間控制連線仍在重試；為免意外離開，系統已保留房間。你可以等候恢復，或按下「離開房間」。";
      toast("⚠️ 房間連線暫未恢復，系統會繼續重試。");
    }, 60000);
  }
  async function roomInfo(code, options = {}) {
    const normalized = String(code || "").trim().toUpperCase();
    if (!/^[A-HJ-KM-NP-Z2-9]{5}$/.test(normalized)) {
      throw new Error("房間代碼必須為 5 個有效大寫英文字母或數字。");
    }
    const data = await api(`/api/room/${encodeURIComponent(normalized)}`);
    roomOpen(data.code, { ...options, initialPhase: data.phase });
    return data;
  }

  window.addEventListener("message", (event) => {
    if (
      event.origin !== location.origin
      || event.source !== $("roomFrame").contentWindow
      || event.data?.channel !== "ai-coach-room"
    ) return;
    const message = event.data;
    const expectedCode = pendingRoomCode || sessionStorage.aiRoom || "";
    if (!expectedCode || String(message.code || "").toUpperCase() !== expectedCode)
      return;
    if (message.type === "room_ready") {
      clearRoomHandshake();
      pendingRoomCode = expectedCode;
      roomPhase = String(message.phase || roomPhase || "lobby");
      if (typeof message.leave_token === "string" && message.leave_token) {
        roomLeaveToken = message.leave_token;
      }
      sessionStorage.aiRoom = expectedCode;
      sessionStorage.aiRoomMode = "A";
      $("retryRoom").classList.add("hidden");
      $("activeRoomNote").textContent = message.phase === "ended"
        ? "房間已結束；可以查看短暫保留的逐字稿及 AI 評判。"
        : "真人對真人練習；只會在測試及輪到自己發言時以 P2P 傳送音訊，Render 會同步控制資料及已提交的逐字稿。";
      if (pendingRoomSuccess) toast(`✅ ${pendingRoomSuccess}`);
      pendingRoomSuccess = "";
    } else if (message.type === "room_phase") {
      roomPhase = String(message.phase || roomPhase);
      if (roomPhase === "ended") {
        clearRoomHandshake();
        $("activeRoomNote").textContent =
          "房間已結束；可以切換功能，亦可以留在本頁查看短暫保留的結果。";
      }
    } else if (message.type === "room_terminal") {
      const text = String(message.message || "房間連線已終止。");
      if (message.recoverable) {
        clearRoomHandshake();
        $("activeRoomNote").textContent =
          `${text} 房間仍然保留；請重新連線，或明確離開房間。`;
        $("retryRoom").classList.remove("hidden");
        showPane("room", { force: true });
        toast(`⚠️ ${text}`);
        return;
      }
      resetRoomUi(true);
      toast(`⚠️ ${text}`);
    }
  });

  async function boot() {
    try {
      const [data, localStatus] = await Promise.all([
        api("/api/ai-coach/data"),
        api("/api/ai-coach/local-status").catch((error) => ({
          available: false,
          selected: false,
          state: "unavailable",
          message: error.message || "未能讀取自家 AI 狀態。",
          modes: [],
        })),
      ]);
      meta = { ...data, local_ai: localStatus };
      const budget = meta.bandwidth_budget || {};
      const usedGb = Number(budget.total_bytes || 0) / 1e9;
      const fmtLimit = (bytes) =>
        (Number(bytes || 0) / 1e9)
          .toFixed(2)
          .replace(/\.00$/, "")
          .replace(/0$/, "");
      $("bandwidthUsage").textContent =
        `本月系統已記錄約 ${usedGb.toFixed(2)}GB；警告／停止Live／必要功能門檻為 ${fmtLimit(budget.warn_bytes)}／${fmtLimit(budget.stop_live_bytes)}／${fmtLimit(budget.essential_only_bytes)}GB；目前保護階段：${budget.stage || 0}。`;
      $("globalModel").innerHTML = meta.models
        .map(
          (model) =>
            `<option value="${esc(model.label)}" ${model.label === meta.default_model ? "selected" : ""}>${esc(model.selection_label ? `${model.label}（${model.selection_label}）` : model.label)}</option>`,
        )
        .join("");
      document.querySelectorAll("[data-topic-source]").forEach(topicSource);
      $("login").classList.add("hidden");
      $("app").classList.remove("hidden");
      syncModel();
      renderQaFields();
      syncReviewPositions();
      syncLive();
      await syncMock();
      syncRoom();
      if (sessionStorage.aiRoom) {
        const storedRoom = sessionStorage.aiRoom;
        try {
          await roomInfo(storedRoom, { restore: true });
        } catch (error) {
          if (error.message === "未登入") throw error;
          if ([403, 404, 409].includes(error.status)
              || !/^[A-HJ-KM-NP-Z2-9]{5}$/.test(storedRoom)) {
            sessionStorage.removeItem("aiRoom");
            sessionStorage.removeItem("aiRoomMode");
          } else {
            roomOpen(storedRoom, { restore: true });
            toast(`⚠️ 暫未能驗證房間，已保留並嘗試重新連線：${error.message}`);
          }
        }
      }
      return true;
    } catch (error) {
      $("app").classList.add("hidden");
      $("login").classList.remove("hidden");
      if (error.message !== "未登入") toast(`⚠️ ${error.message}`);
      return false;
    }
  }

  async function refreshLocalAiStatus() {
    if (!meta) return;
    try {
      meta.local_ai = await api("/api/ai-coach/local-status");
    } catch (error) {
      meta.local_ai = {
        available: false,
        selected: false,
        state: "unavailable",
        message: error.message || "未能讀取自家 AI 狀態。",
        modes: [],
      };
    }
    syncModel();
  }

  function startReviewRetake() {
    if (
      !previousReviewMarkdown ||
      retakeConsumed ||
      $("reviewMode").value !== "台上發言"
    )
      return;
    if (currentReviewContext() !== reviewContextSnapshot) {
      resetReviewCycle("發言資料已更改，請重新完成首次分析。");
      return toast("⚠️ 發言資料已更改，請重新完成首次分析。");
    }
    if (!currentModel()?.supports_audio)
      return toast("⚠️ 請先選擇支援錄音分析的模型，再開始重錄。");
    reviewAttempt = "retake";
    setReviewContextLocked(true);
    clearRecordedAudio();
    $("retakeOffer").classList.add("hidden");
    $("retakeActive").classList.remove("hidden");
    $("reviewSubmit").textContent = "檢查本次是否有改進";
    $("record").focus();
  }

  function cancelReviewRetake() {
    if (reviewAttempt !== "retake") return;
    reviewAttempt = "initial";
    setReviewContextLocked(false);
    clearRecordedAudio();
    $("retakeActive").classList.add("hidden");
    $("retakeOffer").classList.remove("hidden");
    $("reviewSubmit").textContent = "分析發言";
  }

  document
    .querySelectorAll("[data-pane]")
    .forEach((button) =>
      button.addEventListener("click", () => showPane(button.dataset.pane)),
    );
  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    busy(true);
    try {
      await api("/api/committee/login", {
        method: "POST",
        body: JSON.stringify({
          user_id: $("user").value,
          password: $("password").value,
        }),
      });
      if (await boot()) toast("✅ 已登入。");
    } catch (error) {
      toast(`⚠️ ${error.message}`);
    } finally {
      busy(false);
    }
  });
  $("globalModel").addEventListener("change", syncModel);
  $("localMode").addEventListener("change", syncModel);
  $("reviewTopic").addEventListener("input", () =>
    resetReviewCycle("辯題已更改，請重新分析發言。"),
  );
  $("reviewSide").addEventListener("change", () =>
    resetReviewCycle("立場已更改，請重新分析發言。"),
  );
  $("reviewFormat").addEventListener("change", () => {
    resetReviewCycle("賽制已更改，請重新分析發言。");
    syncReviewPositions();
  });
  $("reviewMode").addEventListener("change", () => {
    resetReviewCycle("練習類型已更改，請重新分析發言。");
    renderQaFields();
  });
  $("reviewPosition").addEventListener("change", () => {
    resetReviewCycle("辯位已更改，請重新分析發言。");
  });
  $("record").addEventListener("click", recordAudio);
  $("startRetake").addEventListener("click", startReviewRetake);
  $("cancelRetake").addEventListener("click", cancelReviewRetake);
  $("liveFormat").addEventListener("change", syncLive);
  $("liveMinutes").addEventListener("input", syncLive);
  $("mockFormat").addEventListener("change", syncMock);
  $("mockMinutes").addEventListener("change", syncMock);
  [$("roomStructure"), $("roomFormat")].forEach((element) =>
    element.addEventListener("change", syncRoom),
  );
  $("showCreate").addEventListener("click", () => switchRoomAction("create"));
  $("showJoin").addEventListener("click", () => switchRoomAction("join"));

  $("strategyForm").addEventListener("submit", (event) => {
    event.preventDefault();
    run(
      "strategy",
      {
        topic: $("strategyTopic").value.trim(),
        side: $("strategySide").value,
        debate_format: $("strategyFormat").value,
      },
      "strategyResult",
      "downloadStrategy",
    );
  });
  $("reviewForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const review = buildReviewText();
    if (review.warning) return toast(`⚠️ ${review.warning}`);
    const isRetake = reviewAttempt === "retake";
    if (isRetake) {
      if (
        $("reviewMode").value !== "台上發言" ||
        currentReviewContext() !== reviewContextSnapshot
      ) {
        resetReviewCycle("發言資料已更改，請重新完成首次分析。");
        return toast("⚠️ 發言資料已更改，請重新完成首次分析。");
      }
      if (!previousReviewMarkdown)
        return toast("⚠️ 缺少上次 AI 評語，請重新完成首次分析。");
      if (!audioIntentId)
        return toast("⚠️ 請先錄製一段全新錄音，再檢查本次是否有改進。");
      if (!currentModel()?.supports_audio)
        return toast("⚠️ 改進檢查必須使用支援錄音分析的模型。");
    } else if (!review.text && !audioIntentId) {
      return toast("⚠️ 請輸入文字稿或錄音。");
    }
    const data = await run(
      "speech_review",
      {
        topic: $("reviewTopic").value.trim(),
        match_id: selectedMatchId,
        side: $("reviewSide").value,
        position: Number($("reviewPosition").value),
        debate_format: $("reviewFormat").value,
        review_mode: $("reviewMode").value,
        review_attempt: isRetake ? "retake" : "initial",
        previous_review: isRetake ? previousReviewMarkdown : "",
        text: review.text,
        audio_intent_id: audioIntentId,
        audio_mime: audioMime,
        audio_duration_seconds: audioDurationSeconds,
      },
      isRetake ? "retakeResult" : "reviewResult",
      "downloadReview",
    );
    if (!data) return;
    if (isRetake) {
      reviewAttempt = "initial";
      previousReviewMarkdown = "";
      reviewContextSnapshot = "";
      retakeConsumed = true;
      setReviewContextLocked(false);
      $("reviewSubmit").textContent = "分析發言";
      $("retakeOffer").classList.add("hidden");
      $("retakeActive").classList.add("hidden");
      $("retakeResultTitle").classList.remove("hidden");
      return;
    }
    previousReviewMarkdown = data.markdown;
    reviewContextSnapshot = currentReviewContext();
    retakeConsumed = false;
    $("retakeActive").classList.add("hidden");
    $("retakeResultTitle").classList.add("hidden");
    $("retakeResult").classList.add("hidden");
    $("retakeResult").innerHTML = "";
    $("retakeOffer").classList.toggle(
      "hidden",
      $("reviewMode").value !== "台上發言",
    );
  });
  $("researchForm").addEventListener("submit", (event) => {
    event.preventDefault();
    run(
      "web_research",
      {
        topic: $("researchTopic").value.trim(),
        research_need: $("researchNeed").value.trim(),
      },
      "researchResult",
      "downloadResearch",
    );
  });
  $("factForm").addEventListener("submit", (event) => {
    event.preventDefault();
    run(
      "fact_check",
      { text: $("factText").value.trim() },
      "factResult",
      "downloadFact",
    );
  });
  $("liveForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const minutes =
      $("liveFormat").value === "聯中" ? $("liveMinutes").value : "2.5";
    prepareLive(
      "free",
      $("liveTopic").value.trim(),
      $("liveSide").value,
      $("liveFormat").value,
      minutes,
    );
  });
  $("mockForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const minutes =
      $("mockFormat").value === "聯中" ? $("mockMinutes").value : "2.5";
    prepareLive(
      "mock",
      $("mockTopic").value.trim(),
      $("mockSide").value,
      $("mockFormat").value,
      minutes,
    );
  });
  $("createRoom").addEventListener("submit", async (event) => {
    event.preventDefault();
    busy(true);
    try {
      const structure = $("roomStructure").value;
      const format = $("roomFormat").value;
      const payload = {
        mode: "A",
        structure,
        debate_format: format,
        topic: $("roomTopic").value.trim(),
        free_minutes: format === "聯中" ? Number($("roomMinutes").value) : 2.5,
      };
      payload.side = $("roomSide").value;
      const data = await api("/api/room/create", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      roomOpen(data.code, { successMessage: "房間已建立。" });
    } catch (error) {
      toast(`⚠️ ${error.message}`);
    } finally {
      busy(false);
    }
  });
  $("joinRoom").addEventListener("submit", async (event) => {
    event.preventDefault();
    busy(true);
    try {
      await roomInfo($("joinCode").value.trim().toUpperCase(), {
        successMessage: "已加入房間。",
      });
    } catch (error) {
      toast(`⚠️ ${error.message}`);
    } finally {
      busy(false);
    }
  });
  $("joinCode").addEventListener("input", function () {
    this.value = this.value
      .toUpperCase()
      .replace(/[^A-HJ-KM-NP-Z2-9]/g, "")
      .slice(0, 5);
  });
  $("leaveRoom").addEventListener("click", async () => {
    const code = sessionStorage.aiRoom || pendingRoomCode;
    if (code) {
      $("roomFrame").contentWindow?.postMessage({
        channel: "ai-coach-room",
        type: "parent_leave",
        code,
      }, location.origin);
      const token = roomLeaveToken;
      roomLeaveToken = "";
      const leaveRequest = token
        ? api(`/api/room/${encodeURIComponent(code)}/leave`, {
          method: "POST",
          body: JSON.stringify({ leave_token: token }),
          keepalive: true,
        }).catch(() => {})
        : Promise.resolve();
      resetRoomUi(true);
      await leaveRequest;
      return;
    }
    resetRoomUi(true);
  });
  $("retryRoom").addEventListener("click", () => {
    const code = sessionStorage.aiRoom || pendingRoomCode;
    if (!code) return;
    roomOpen(code, { initialPhase: roomPhase || "pending" });
  });

  window.addEventListener("beforeunload", (event) => {
    if (!hasUnendedRoom()) return;
    event.preventDefault();
    event.returnValue = "";
  });
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.("a[href]");
    if (!link || !hasUnendedRoom()) return;
    event.preventDefault();
    showPane("room", { force: true });
    toast("⚠️ 連線房間仍在進行；請先離開房間，或由主持結束房間。");
  });
  [
    ["downloadStrategy", "策略建議.txt", "strategyResult"],
    ["downloadResearch", "搵料結果.txt", "researchResult"],
    ["downloadFact", "fact_check結果.txt", "factResult"],
  ].forEach(([button, name, result]) =>
    $(button).addEventListener("click", () =>
      download(name, $(result).textContent),
    ),
  );
  $("downloadReview").addEventListener("click", () => {
    const sections = [`初次分析\n\n${$("reviewResult").textContent.trim()}`];
    if (!$("retakeResult").classList.contains("hidden"))
      sections.push(
        `改進檢查結果\n\n${$("retakeResult").textContent.trim()}`,
      );
    download("發言分析及改進檢查.txt", sections.join("\n\n---\n\n"));
  });

  boot();
  setInterval(refreshLocalAiStatus, 10000);
})();
