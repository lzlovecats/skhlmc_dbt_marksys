(() => {
  "use strict";
  const $ = (id) => document.getElementById(id),
    esc = (v) =>
      String(v ?? "").replace(
        /[&<>"']/g,
        (c) =>
          ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
          })[c],
      );
  let data,
    blob,
    rec,
    current,
    started = 0,
    recordedSeconds = 0,
    pendingAudio,
    pendingUpload,
    pendingLlm,
    suggestions = [],
    scriptPage = 1,
    recordStopTimer = null,
    skipped = new Set();
  const FACTORY_LANGUAGE = "yue-Hant-HK",
    FACTORY_RECIPE_LABELS = {
      rag_knowledge_card: "RAG來源知識卡",
      rag_knowledge_card_v1: "RAG來源知識卡",
      rag_argument_decomposition: "RAG論證拆解卡",
      rag_argument_decomposition_v1: "RAG論證拆解卡",
      sft_speech_critique: "演辭評改",
      sft_speech_critique_v1: "演辭評改",
      sft_attack_defence_dialogue: "SFT攻防演練對話",
      sft_attack_defence_v1: "SFT攻防演練對話",
      transcript_structure: "完整逐字稿結構拆分",
      transcript_structure_v1: "完整逐字稿結構拆分",
    },
    FACTORY_TRANSCRIPT_RECIPE = "transcript_structure_v1",
    factoryState = {
      bootstrap: null,
      selectedSource: null,
      preview: null,
      selectedRecipe: "",
      transcriptRunPage: 1,
      transcriptRunPages: 1,
      reviewKind: "standard",
      retryJobId: "",
      sourcePage: 1,
      sourcePages: 1,
      jobPage: 1,
      jobPages: 1,
      reviewPage: 1,
      reviewPages: 1,
      reviewItems: [],
      reviewIndex: 0,
      reviewContext: null,
      approvedPage: 1,
      approvedPages: 1,
      releasePage: 1,
      releasePages: 1,
      selectedApproved: new Map(),
      requestGeneration: Object.create(null),
      loading: Object.create(null),
    };
  const toast = (x) => VoteUI.toast($("toast"), x),
    busy = (x) => VoteUI.setBusy($("busy"), x),
    api = async (url, opt = {}) => {
      const r = await fetch(url, {
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          ...opt,
        }),
        d = await r.json().catch(() => ({}));
      if (r.status === 401) throw Error("未登入");
      if (!r.ok) {
        const e = Error(d.detail || "操作失敗");
        e.status = r.status;
        throw e;
      }
      return d;
    },
    confirmAsk = (title, text) =>
      new Promise((ok) => {
        const d = $("confirmDialog");
        $("confirmTitle").textContent = title;
        $("confirmText").textContent = text;
        d.returnValue = "";
        d.onclose = () => ok(d.returnValue === "ok");
        d.showModal();
      });
  const table = (rows, cols, action) =>
    rows.length
      ? `<div class="table-wrap"><table><thead><tr>${cols.map((x) => `<th>${x[0]}</th>`).join("")}${action ? "<th>操作</th>" : ""}</tr></thead><tbody>${rows.map((r) => `<tr>${cols.map((x) => `<td>${x[2] ? x[1](r) : esc(x[1](r))}</td>`).join("")}${action ? `<td>${action(r)}</td>` : ""}</tr>`).join("")}</tbody></table></div>`
      : '<p class="caption">暫無紀錄。</p>';
  const paged = (id, url, renderer, preservePage = false) => {
    const target = $(id),
      page = preservePage ? Number(target._voteServerSpec?.page || 1) : 1;
    return VoteUI.serverPaged(target, url, renderer, page).catch((e) =>
      toast("⚠️ " + e.message),
    );
  };
  function loadCollections() {
    paged("myRecordings", "/api/ai-training/collection/my-recordings", (rows) =>
      table(
        rows,
        [
          ["句子", (r) => r.script_id],
          ["狀態", (r) => r.status],
          ["時間", (r) => r.created_at],
          ["備註", (r) => r.review_note || ""],
        ],
        (r) =>
          `<audio controls preload="none" src="/api/ai-training/recordings/${r.id}/audio"></audio>`,
      ),
    );
    paged("myLlm", "/api/ai-training/collection/my-llm", (rows) =>
      table(
        rows,
        [
          ["類型", (r) => r.data_type],
          ["標題", (r) => r.title || ""],
          ["AI 預檢", (r) => r.ai_review_status],
          ["狀態", (r) => r.status],
          ["時間", (r) => r.created_at],
        ],
        (r) =>
          ["pending", "accepted"].includes(r.status)
            ? `<button data-withdraw-llm="${r.id}" class="danger">撤回</button>`
            : "",
      ),
    );
    paged("publicLexicon", "/api/ai-training/collection/lexicon", (rows) =>
      table(rows, [
        ["詞語", (r) => r.term],
        ["讀法", (r) => r.reading],
        ["粵拼", (r) => r.jyutping || ""],
        ["例句", (r) => r.example || ""],
        ["備註", (r) => r.note || ""],
        ["類別", (r) => r.category || ""],
      ]),
    );
    if (data.is_admin) {
      loadRecordings();
      loadLlmSubmissions();
      paged("lexiconTable", "/api/ai-training/collection/lexicon", (rows) => {
        window.lexPage = Object.fromEntries(rows.map((x) => [x.id, x]));
        return table(
          rows,
          [
            ["詞語", (r) => r.term],
            ["讀法", (r) => r.reading],
            ["粵拼", (r) => r.jyutping],
            ["類別", (r) => r.category],
          ],
          (r) => `<button data-edit-lex="${esc(r.id)}">編輯</button>`,
        );
      });
    }
  }
  function loadRecordings(resetPage = false) {
    const status = $("recordFilter").value,
      speaker = $("speakerFilter").value;
    paged(
      "adminRecordings",
      `/api/ai-training/admin/recordings?status=${encodeURIComponent(status)}&speaker=${encodeURIComponent(speaker)}`,
      (rows) =>
        table(
          rows,
          [
            ["提交者", (r) => r.speaker_user_id],
            ["句子", (r) => r.script_id],
            ["稿句", (r) => r.prompt_text],
            [
              "AI 預檢",
              (r) =>
                `${r.ai_review_status}${r.ai_transcript ? `｜${r.ai_transcript}` : ""}`,
            ],
            ["狀態", (r) => r.status],
            [
              "錄音",
              (r) =>
                `<audio controls preload="none" src="/api/ai-training/recordings/${r.id}/audio"></audio>`,
              true,
            ],
          ],
          (r) =>
            r.status === "pending"
              ? `<textarea class="review-note" data-note="recordings-${r.id}" placeholder="審核備註"></textarea><button data-review="recordings" data-id="${r.id}" data-status="accepted">接受</button><button data-review="recordings" data-id="${r.id}" data-status="rejected" class="danger">拒絕</button>`
              : "",
        ),
      !resetPage,
    );
  }
  function loadLlmSubmissions(resetPage = false) {
    const status = $("llmFilter").value,
      submitter = $("llmSubmitterFilter").value;
    paged(
      "adminLlm",
      `/api/ai-training/admin/submissions?status=${encodeURIComponent(status)}&submitter=${encodeURIComponent(submitter)}`,
      (rows) =>
        table(
          rows,
          [
            ["提交者", (r) => r.submitted_by],
            ["類型", (r) => r.data_type],
            ["標題", (r) => r.title || ""],
            ["AI 預檢", (r) => r.ai_review_status || "未檢查"],
            ["狀態", (r) => r.status],
            [
              "內容",
              (r) =>
                `<details><summary>原文 / 預檢</summary><p><b>立場／角色：</b>${esc(r.side || "不適用")}</p><p><b>辯題／情境：</b>${esc(r.topic_text || "不適用")}</p><p><b>來源／備註：</b>${esc(r.source_note || "沒有提供")}</p><p><b>原文：</b></p><p>${esc(r.content_text)}</p><p><b>AI 預檢：</b></p><pre class="json">${esc(r.ai_review_json || "沒有 AI 預檢 JSON")}</pre></details>`,
              true,
            ],
          ],
          (r) =>
            r.status === "pending"
              ? `<textarea class="review-note" data-note="llm-${r.id}" placeholder="審核備註"></textarea><button data-review="llm" data-id="${r.id}" data-status="accepted">接受</button><button data-review="llm" data-id="${r.id}" data-status="rejected" class="danger">拒絕</button>`
              : "",
        ),
      !resetPage,
    );
  }
  function syncRecordingExport() {
    const speaker = $("speakerFilter").value.trim();
    $("recordExport").href =
      "/api/ai-training/export/recordings.json" +
      (speaker ? "?speaker=" + encodeURIComponent(speaker) : "");
  }
  function syncLlmExport() {
    const submitter = $("llmSubmitterFilter").value.trim();
    $("llmExport").href =
      "/api/ai-training/export/llm.jsonl" +
      (submitter ? "?submitter=" + encodeURIComponent(submitter) : "");
  }
  const factoryRows = (payload) => {
      if (Array.isArray(payload)) return payload;
      for (const key of ["items", "rows", "sources", "jobs", "releases"]) {
        if (Array.isArray(payload?.[key])) return payload[key];
      }
      return [];
    },
    factoryId = (item) =>
      String(
        item?.id ??
          item?.item_id ??
          item?.job_id ??
          item?.release_id ??
          item?.source_id ??
          "",
      ),
    factoryPageCount = (payload, current) => {
      const direct = Number(
        payload?.total_pages ?? payload?.pages ?? payload?.page_count ?? 0,
      );
      if (Number.isFinite(direct) && direct > 0) return Math.floor(direct);
      if (payload?.has_next) return current + 1;
      return current;
    },
    factoryEl = (tag, className = "", value = "") => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (value !== "") node.textContent = String(value);
      return node;
    };

  function factoryClear(target) {
    while (target.firstChild) target.removeChild(target.firstChild);
  }

  function factoryRequest(key) {
    const generation = Number(factoryState.requestGeneration[key] || 0) + 1;
    factoryState.requestGeneration[key] = generation;
    return generation;
  }

  function factoryRequestIsCurrent(key, generation) {
    return factoryState.requestGeneration[key] === generation;
  }

  function factorySetLoading(key, value, ...buttons) {
    factoryState.loading[key] = Boolean(value);
    buttons.filter(Boolean).forEach((button) => (button.disabled = Boolean(value)));
  }

  function factoryRecipeId(item, defaultValue = "") {
    return String(
      item?.recipe_id ??
        item?.recipe_key ??
        item?.artifact_kind ??
        item?.kind ??
        item?.id ??
        defaultValue,
    );
  }

  function factoryRecipeLabel(item, key) {
    return String(item?.label || item?.name || FACTORY_RECIPE_LABELS[key] || key);
  }

  function factoryArtifactKind(item) {
    return String(
      item?.artifact_kind ||
        item?.recipe_id ||
        item?.recipe_key ||
        item?.recipe ||
        item?.kind ||
        "",
    );
  }

  function factoryDatasetKind(item) {
    const explicit = String(
      item?.dataset_kind || item?.release_kind || "",
    ).toLowerCase();
    if (explicit === "rag" || explicit === "sft") return explicit;
    return factoryArtifactKind(item).startsWith("sft_") ? "sft" : "rag";
  }

  function factorySourceKind(item) {
    const kind = String(item?.source_kind || item?.kind || "");
    return ["paste", "admin_paste"].includes(kind) ? "paste" : "submission";
  }

  function factorySourceText(item) {
    const nested = item?.source && typeof item.source === "object" ? item.source : {};
    return String(
      item?.source_text ||
        item?.content_text ||
        item?.content ||
        nested.content_text ||
        nested.content ||
        "",
    );
  }

  function factoryCandidatePayload(item) {
    for (const value of [
      item?.reviewed_payload,
      item?.reviewed_json,
      item?.candidate_payload,
      item?.original_json,
      item?.payload,
      item?.content_json,
      item?.candidate,
    ]) {
      if (value !== undefined && value !== null && value !== "") return value;
    }
    return {};
  }

  function factoryPrettyJson(value) {
    if (typeof value === "string") {
      try {
        return JSON.stringify(JSON.parse(value), null, 2);
      } catch (_error) {
        return value;
      }
    }
    return JSON.stringify(value ?? {}, null, 2);
  }

  function factoryRenderSelect(select, items, valueOf, labelOf, selected, available) {
    factoryClear(select);
    items.forEach((item) => {
      const option = document.createElement("option"),
        value = String(valueOf(item));
      option.value = value;
      option.textContent = String(labelOf(item));
      if (available && !available(item)) option.disabled = true;
      if (value === String(selected || "")) option.selected = true;
      select.appendChild(option);
    });
  }

  function factoryNormalizeRecipes(value) {
    if (Array.isArray(value)) return value;
    if (value && typeof value === "object") {
      return Object.entries(value).map(([key, item]) =>
        typeof item === "object" && item !== null
          ? { recipe_id: key, ...item }
          : { recipe_id: key, label: item },
      );
    }
    return Object.entries(FACTORY_RECIPE_LABELS).map(([recipe_id, label]) => ({
      recipe_id,
      label,
    }));
  }

  function factoryNormalizeModels(value) {
    if (Array.isArray(value)) {
      return value.map((item) =>
        typeof item === "string" ? { label: item, available: true } : item,
      );
    }
    if (value && typeof value === "object") {
      return Object.entries(value).map(([label, item]) => ({
        label,
        ...(typeof item === "object" && item !== null ? item : {}),
      }));
    }
    return [];
  }

  function selectFactoryProduct(recipeId, { load = true } = {}) {
    const selected = String(recipeId || ""),
      transcript = selected === FACTORY_TRANSCRIPT_RECIPE;
    factoryState.selectedRecipe = selected;
    $("factoryRecipe").value = selected;
    document.querySelectorAll(".factory-product-card").forEach((card) => {
      const active = card.dataset.recipeId === selected;
      card.classList.toggle("selected", active);
      const input = card.querySelector('input[type="radio"]');
      if (input) input.checked = active;
    });
    $("factoryStandardWorkflow").classList.toggle("hidden", transcript);
    $("factoryTranscriptForm").classList.toggle("hidden", !transcript);
    $("factoryJobs").classList.toggle("hidden", transcript);
    $("factoryTranscriptRuns").classList.toggle("hidden", !transcript);
    syncFactoryJobPagination();
    if (load) {
      if (transcript) void loadTranscriptRuns(true);
      else void Promise.all([loadFactorySources(), loadFactoryJobs()]);
    }
  }

  function renderFactoryProducts(recipes) {
    const target = $("factoryProducts");
    factoryClear(target);
    recipes.forEach((item, index) => {
      const recipeId = factoryRecipeId(item),
        label = factoryEl("label", "factory-product-card"),
        radio = document.createElement("input"),
        body = factoryEl("span"),
        title = factoryEl("strong", "", factoryRecipeLabel(item, recipeId)),
        description = factoryEl(
          "span",
          "",
          item.description || "用途說明未能載入。",
        );
      label.dataset.recipeId = recipeId;
      radio.type = "radio";
      radio.name = "factoryProduct";
      radio.value = recipeId;
      radio.addEventListener("change", () => selectFactoryProduct(recipeId));
      body.append(title, description);
      label.append(radio, body);
      target.appendChild(label);
      if (index === 0 && !factoryState.selectedRecipe) {
        factoryState.selectedRecipe = recipeId;
      }
    });
    const available = recipes.some(
      (item) => factoryRecipeId(item) === factoryState.selectedRecipe,
    );
    selectFactoryProduct(
      available ? factoryState.selectedRecipe : factoryRecipeId(recipes[0]),
      { load: false },
    );
  }

  function renderFactoryBootstrap(payload) {
    factoryState.bootstrap = payload || {};
    const readiness = payload?.readiness || {},
      ready = readiness.ready !== false && readiness.status !== "blocked",
      readinessText =
        readiness.message ||
        (ready
          ? `已就緒｜輸出語言：香港粵語繁體中文（${FACTORY_LANGUAGE}）`
          : "資料工廠尚未就緒，請通知 developer。");
    $("factoryReadiness").textContent = readinessText;
    $("factoryReadiness").classList.toggle("warn", !ready);

    const recipes = factoryNormalizeRecipes(payload?.recipes),
      models = factoryNormalizeModels(payload?.models),
      defaultModel = String(payload?.default_model || "");
    factoryRenderSelect(
      $("factoryRecipe"),
      recipes,
      (item) => factoryRecipeId(item),
      (item) => factoryRecipeLabel(item, factoryRecipeId(item)),
      recipes[0] ? factoryRecipeId(recipes[0]) : "",
    );
    renderFactoryProducts(recipes);
    factoryRenderSelect(
      $("factoryModel"),
      models,
      (item) => item.label || item.model_label || item.id,
      (item) => {
        const label = item.label || item.model_label || item.id,
          cost = item.pricing_label || item.cost_note || "";
        return `${label}${cost ? `｜${cost}` : ""}${item.available === false ? "｜未設定" : ""}`;
      },
      defaultModel,
      (item) => item.available !== false,
    );
    factoryRenderSelect(
      $("factoryTranscriptModel"),
      models,
      (item) => item.label || item.model_label || item.id,
      (item) => {
        const label = item.label || item.model_label || item.id,
          cost = item.pricing_label || item.cost_note || "";
        return `${label}${cost ? `｜${cost}` : ""}${item.available === false ? "｜未設定" : ""}`;
      },
      defaultModel,
      (item) => item.available !== false,
    );
    if (!$("factoryModel").value && models.length) {
      const firstAvailable = [...$("factoryModel").options].find(
        (option) => !option.disabled,
      );
      if (firstAvailable) firstAvailable.selected = true;
    }
    if (!$("factoryTranscriptModel").value && models.length) {
      const firstAvailable = [...$("factoryTranscriptModel").options].find(
        (option) => !option.disabled,
      );
      if (firstAvailable) firstAvailable.selected = true;
    }
    const hasAvailableModel = [...$("factoryModel").options].some(
      (option) => !option.disabled,
    );
    $("factoryPreviewBtn").disabled = !ready || !hasAvailableModel;
    $("factoryTranscriptPreviewBtn").disabled = !ready || !hasAvailableModel;

    const limits = payload?.limits || {},
      maxCount = Math.min(5, Math.max(1, Number(limits.max_items_per_job || 5))),
      defaultCount = Math.min(
        maxCount,
        Math.max(1, Number(limits.default_items_per_job || 3)),
      );
    $("factoryCount").max = String(maxCount);
    $("factoryCount").value = String(defaultCount);
    if (Number(limits.source_max_chars || 0) > 0) {
      $("factoryPasteContent").maxLength = Number(limits.source_max_chars);
    }
    if (Number(limits.source_note_max_chars || 0) > 0) {
      $("factoryPasteSourceNote").maxLength = Number(
        limits.source_note_max_chars,
      );
    }
    if (Number(limits.transcript_max_chars || 0) > 0) {
      $("factoryTranscriptContent").maxLength = Number(
        limits.transcript_max_chars,
      );
    }

    const tags = Array.isArray(payload?.topic_tags) ? payload.topic_tags : [];
    factoryClear($("factoryTopicTags"));
    if (!tags.length) {
      $("factoryTopicTags").appendChild(
        factoryEl("span", "caption", "現時沒有已批准自訂主題標籤。"),
      );
    }
    tags.forEach((tag) => {
      const label = factoryEl("label", "factory-tag-choice"),
        input = document.createElement("input"),
        tagId = String(tag.tag_id ?? tag.id ?? tag.slug ?? tag);
      input.type = "checkbox";
      input.value = tagId;
      input.dataset.factoryTopicTag = "true";
      label.append(input, document.createTextNode(String(tag.label || tag.name || tagId)));
      $("factoryTopicTags").appendChild(label);
    });
  }

  async function loadFactoryBootstrap(force = false) {
    if (factoryState.bootstrap && !force) return factoryState.bootstrap;
    const generation = factoryRequest("bootstrap");
    try {
      const payload = await api("/api/ai-training/factory/bootstrap");
      if (!factoryRequestIsCurrent("bootstrap", generation)) return null;
      renderFactoryBootstrap(payload);
      return payload;
    } catch (error) {
      if (factoryRequestIsCurrent("bootstrap", generation)) {
        $("factoryReadiness").textContent =
          `資料工廠暫時未能載入：${error.message}`;
        $("factoryReadiness").classList.add("warn");
        $("factoryPreviewBtn").disabled = true;
        $("factoryTranscriptPreviewBtn").disabled = true;
      }
      throw error;
    }
  }

  function setFactoryRetryMode(jobId = "") {
    factoryState.retryJobId = String(jobId || "");
    const retrying = Boolean(factoryState.retryJobId);
    $("factoryRecipe").disabled = retrying;
    document
      .querySelectorAll('input[name="factoryProduct"]')
      .forEach((input) => (input.disabled = retrying));
    $("factoryCount").disabled = retrying;
    $("factoryManagerInstruction").disabled = retrying;
    $("factoryCancelRetry").classList.toggle("hidden", !retrying);
    document
      .querySelectorAll('input[name="factorySource"]')
      .forEach((input) => (input.disabled = retrying));
  }

  function selectFactorySource(source, card) {
    if (factoryState.retryJobId) setFactoryRetryMode();
    factoryState.selectedSource = source;
    document
      .querySelectorAll(".factory-source-card")
      .forEach((node) => node.classList.remove("selected"));
    card?.classList.add("selected");
    const title = source?.title || source?.topic_text || `來源 #${factoryId(source)}`,
      kind = factorySourceKind(source) === "paste" ? "貼上資料" : "已批准提交";
    $("factorySelectedSource").textContent = `已選：${title}｜${kind}`;
    $("factorySourceChooser").open = false;
  }

  function restoreFactoryRetryJob(job) {
    const jobId = factoryId(job),
      sourceId = String(job.source_id || ""),
      recipeId = String(job.recipe_id || job.recipe_key || ""),
      requestedCount = Number(job.requested_count ?? job.item_count ?? 0),
      modelLabel = String(job.model_label || ""),
      managerInstruction = String(
        job.instruction_text ?? job.manager_instruction ?? "",
      );
    if (!jobId || !sourceId || !recipeId || !requestedCount) {
      toast("⚠️ 工作紀錄缺少重試所需資料。");
      return;
    }
    if (![...$("factoryRecipe").options].some((option) => option.value === recipeId)) {
      toast("⚠️ 這份舊工作的產品已不可用。");
      return;
    }
    factoryState.selectedSource = {
      source_id: sourceId,
      title: job.source_title || `來源 #${sourceId}`,
      source_kind: job.source_kind || "",
    };
    selectFactoryProduct(recipeId, { load: false });
    $("factoryCount").value = String(requestedCount);
    $("factoryManagerInstruction").value = managerInstruction;
    if (
      modelLabel &&
      [...$("factoryModel").options].some(
        (option) => option.value === modelLabel && !option.disabled,
      )
    ) {
      $("factoryModel").value = modelLabel;
    }
    setFactoryRetryMode(jobId);
    $("factorySelectedSource").textContent =
      `手動重試工作 #${jobId}｜${factoryState.selectedSource.title}。來源、產品、數量及補充指示已鎖定；可選另一個已設定模型。`;
    $("factoryJobForm").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function withdrawFactorySource(source, button) {
    const sourceId = factoryId(source);
    if (!sourceId || sourceId.startsWith("submission:")) return;
    if (factoryState.loading["source-withdraw"]) return;
    factorySetLoading("source-withdraw", true, button);
    try {
      if (
        !(await confirmAsk(
          "撤回來源",
          "來源、相關候選資料及已發佈版本會標記為無效。確定繼續？",
        ))
      )
        return;
      const entered = window.prompt(
        "請填寫撤回原因（必填，最多 1000 字）：",
        "",
      );
      if (entered === null) return;
      const reason = entered.trim();
      if (!reason) return toast("⚠️ 請填寫撤回原因。");
      if (reason.length > 1000)
        return toast("⚠️ 撤回原因不可超過 1000 字。");
      await api(
        `/api/ai-training/factory/sources/${encodeURIComponent(sourceId)}/withdraw`,
        {
          method: "POST",
          body: JSON.stringify({ reason }),
        },
      );
      if (factoryId(factoryState.selectedSource) === sourceId) {
        factoryState.selectedSource = null;
        setFactoryRetryMode();
        $("factoryManagerInstruction").value = "";
        $("factorySelectedSource").textContent = "尚未選擇來源。";
      }
      factoryState.selectedApproved.clear();
      toast("✅ 已撤回來源並更新所有衍生資料狀態。");
      await Promise.all([
        loadFactorySources(true),
        loadFactoryJobs(true),
        loadFactoryReviewItems(true),
        loadFactoryApprovedItems(true),
        loadFactoryReleases(true),
      ]);
    } catch (error) {
      toast("⚠️ 未能撤回來源：" + error.message);
    } finally {
      factorySetLoading("source-withdraw", false, button);
    }
  }

  function renderFactorySources(payload) {
    const rows = factoryRows(payload),
      target = $("factorySources");
    factoryClear(target);
    if (!rows.length) {
      target.appendChild(factoryEl("p", "caption", "找不到符合條件的來源。"));
    }
    rows.forEach((source) => {
      const sourceId = factoryId(source),
        card = factoryEl("article", "factory-source-card"),
        label = factoryEl("label"),
        radio = document.createElement("input"),
        body = factoryEl("div"),
        title = factoryEl(
          "strong",
          "",
          source.title || source.topic_text || `來源 #${factoryId(source)}`,
        ),
        kind = factorySourceKind(source) === "paste" ? "貼上資料" : "已批准提交",
        meta = factoryEl(
          "div",
          "factory-meta",
          `${kind}｜${source.topic_text || "未設辯題"}｜${source.side || "not_applicable"}｜${source.language_code || source.language || FACTORY_LANGUAGE}`,
        ),
        snippet = factoryEl("p", "", factorySourceText(source).slice(0, 220));
      radio.type = "radio";
      radio.name = "factorySource";
      radio.value = factoryId(source);
      radio.disabled = Boolean(factoryState.retryJobId);
      radio.checked = factoryId(factoryState.selectedSource) === factoryId(source);
      if (radio.checked) card.classList.add("selected");
      radio.addEventListener("change", () => selectFactorySource(source, card));
      body.append(title, meta, snippet);
      label.append(radio, body);
      card.appendChild(label);
      if (sourceId && !sourceId.startsWith("submission:")) {
        const actions = factoryEl("div", "actions"),
          withdraw = factoryEl("button", "danger", "撤回來源");
        withdraw.type = "button";
        withdraw.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          void withdrawFactorySource(source, withdraw);
        });
        actions.appendChild(withdraw);
        card.appendChild(actions);
      }
      target.appendChild(card);
    });
    factoryState.sourcePages = factoryPageCount(payload, factoryState.sourcePage);
    $("factorySourcePage").textContent = `第 ${factoryState.sourcePage} / ${factoryState.sourcePages} 頁`;
    $("factorySourcePrev").disabled = factoryState.sourcePage <= 1;
    $("factorySourceNext").disabled = factoryState.sourcePage >= factoryState.sourcePages;
  }

  async function loadFactorySources(reset = false) {
    if (reset) factoryState.sourcePage = 1;
    const generation = factoryRequest("sources"),
      kind = $("factorySourceKind").value,
      search = $("factorySourceSearch").value.trim(),
      url =
        `/api/ai-training/factory/sources?page=${factoryState.sourcePage}` +
        `&kind=${encodeURIComponent(kind)}&search=${encodeURIComponent(search)}`;
    try {
      const payload = await api(url);
      if (factoryRequestIsCurrent("sources", generation)) renderFactorySources(payload);
    } catch (error) {
      if (factoryRequestIsCurrent("sources", generation))
        toast("⚠️ 來源載入失敗：" + error.message);
    }
  }

  function factorySelectedTags() {
    return [...document.querySelectorAll("[data-factory-topic-tag]:checked")].map(
      (input) => input.value,
    );
  }

  function factoryPreviewRequest() {
    const sourceId = factoryId(factoryState.selectedSource);
    if (!sourceId) throw Error("請先選擇一份來源。");
    const count = Number($("factoryCount").value),
      max = Number($("factoryCount").max || 5),
      topicTagIds = factorySelectedTags(),
      topicTagMax = Number(factoryState.bootstrap?.limits?.topic_tag_max || 10);
    if (!Number.isInteger(count) || count < 1 || count > max)
      throw Error(`生成數量只可為 1 至 ${max}。`);
    if (topicTagIds.length > topicTagMax)
      throw Error(`每份工作最多選 ${topicTagMax} 個主題標籤。`);
    const request = {
      source_id: sourceId,
      recipe_id: $("factoryRecipe").value,
      model_label: $("factoryModel").value,
      item_count: count,
      topic_tag_ids: topicTagIds,
      output_language: FACTORY_LANGUAGE,
      manager_instruction: $("factoryManagerInstruction").value.trim(),
    };
    if (factoryState.retryJobId) request.job_id = factoryState.retryJobId;
    return request;
  }

  function factoryPreviewWarnings(preview) {
    const warnings = preview?.pii_warnings || preview?.pii?.warnings || [];
    return Array.isArray(warnings) ? warnings : [];
  }

  function showFactoryPreview(preview) {
    factoryState.preview = preview;
    const model = preview.model_label || $("factoryModel").value,
      costValue =
        preview.estimated_cost_hkd ?? preview.cost?.hkd ?? preview.estimate_hkd,
      costHkd = Number(costValue),
      costText =
        costValue !== undefined && Number.isFinite(costHkd) && costHkd >= 0
          ? `HK$${costHkd.toFixed(4)}`
          : "以 provider 實際計價為準",
      outputTokenLimit = Number(
        preview.provider_payload?.max_output_tokens || 0,
      ),
      outputLimitText = Number.isInteger(outputTokenLimit) && outputTokenLimit > 0
        ? `｜輸出上限：${outputTokenLimit} tokens`
        : "",
      sourceSha = preview.source_sha256 || "未提供",
      previewSha = preview.preview_sha256 || "未提供",
      expires = preview.expires_at || "短期有效";
    $("factoryPreviewMeta").textContent =
      `模型：${model}｜估算成本：${costText}${outputLimitText}｜來源 SHA-256：${sourceSha}｜預覽 SHA-256：${previewSha}｜預覽到期：${expires}`;
    $("factorySystemPrompt").textContent = String(
      preview.system_prompt ||
        preview.provider_payload?.system_prompt ||
        preview.provider_payload?.system ||
        "",
    );
    $("factoryUserPrompt").textContent = String(
      preview.user_prompt ||
        preview.provider_payload?.user_prompt ||
        preview.provider_payload?.user ||
        "",
    );
    const warnings = factoryPreviewWarnings(preview),
      list = $("factoryPiiWarnings");
    factoryClear(list);
    warnings.forEach((warning) => {
      const textValue =
        typeof warning === "string"
          ? warning
          : warning.message || warning.label || JSON.stringify(warning);
      list.appendChild(factoryEl("li", "", textValue));
    });
    $("factoryPiiWarning").classList.toggle("hidden", !warnings.length);
    $("factoryPiiOverrideReason").value = "";
    $("factoryThirdPartyWarningText").textContent = String(
      preview.third_party_warning ||
        factoryState.bootstrap?.third_party_warning ||
        "我明白以上完整文字會傳給所選第三方 AI provider。",
    );
    $("factoryRightsConfirmed").checked = false;
    $("factoryAnonymizedConfirmed").checked = false;
    $("factoryThirdPartyConfirmed").checked = false;
    $("factoryGenerateBtn").disabled = false;
    $("factoryPreviewDialog").showModal();
  }

  async function previewFactoryJob() {
    if (factoryState.loading.preview) return;
    let request;
    try {
      request = factoryPreviewRequest();
    } catch (error) {
      toast("⚠️ " + error.message);
      return;
    }
    factorySetLoading("preview", true, $("factoryPreviewBtn"));
    try {
      const preview = await api("/api/ai-training/factory/jobs/preview", {
        method: "POST",
        body: JSON.stringify(request),
      });
      setFactoryRetryMode();
      showFactoryPreview(preview);
    } catch (error) {
      toast("⚠️ 未能建立預覽：" + error.message);
    } finally {
      factorySetLoading("preview", false, $("factoryPreviewBtn"));
    }
  }

  function showTranscriptPreview(preview) {
    const windows = Array.isArray(preview.windows) ? preview.windows : [],
      first = windows[0] || {},
      userPrompts = windows
        .map(
          (item) =>
            `===== 視窗 ${item.ordinal} / ${windows.length}｜核心 ${item.core_start}-${item.core_end}｜連同前後文 ${item.context_start}-${item.context_end} =====\n${item.user_prompt || ""}`,
        )
        .join("\n\n");
    showFactoryPreview({
      ...preview,
      recipe_id: FACTORY_TRANSCRIPT_RECIPE,
      estimated_cost_hkd: preview.estimate?.estimated_cost_hkd,
      source_sha256: preview.content_sha256,
      preview_sha256: preview.manifest_sha256,
      system_prompt: first.system_prompt || "",
      user_prompt: userPrompts,
      provider_payload: first.provider_payload || {},
    });
    $("factoryPreviewMeta").textContent +=
      `｜共 ${windows.length} 個視窗，確認後會逐窗處理`;
  }

  async function previewTranscript() {
    if (factoryState.loading["transcript-preview"]) return;
    if (!$("factoryTranscriptForm").reportValidity()) return;
    const submit = $("factoryTranscriptPreviewBtn");
    factorySetLoading("transcript-preview", true, submit);
    try {
      const preview = await api("/api/ai-training/factory/transcripts/preview", {
        method: "POST",
        body: JSON.stringify({
          title: $("factoryTranscriptTitle").value.trim(),
          topic_text: $("factoryTranscriptTopic").value.trim(),
          source_note: $("factoryTranscriptSourceNote").value.trim(),
          language: $("factoryTranscriptLanguage").value,
          rights_basis: $("factoryTranscriptRightsBasis").value,
          rights_for_storage_confirmed: $("factoryTranscriptStorageRights").checked,
          content_text: $("factoryTranscriptContent").value,
          model_label: $("factoryTranscriptModel").value,
          manager_instruction: $("factoryTranscriptInstruction").value.trim(),
        }),
      });
      showTranscriptPreview(preview);
      await loadTranscriptRuns(true);
    } catch (error) {
      toast("⚠️ 未能建立逐字稿預覽：" + error.message);
    } finally {
      factorySetLoading("transcript-preview", false, submit);
    }
  }

  async function processTranscriptRun(runId) {
    if (factoryState.loading["transcript-run"]) return;
    factorySetLoading("transcript-run", true, $("factoryGenerateBtn"));
    try {
      let done = false,
        guard = 0;
      while (!done && guard < 41) {
        const result = await api(
          `/api/ai-training/factory/transcript-runs/${encodeURIComponent(runId)}/next`,
          { method: "POST" },
        );
        done = Boolean(result.done);
        guard += 1;
        await loadTranscriptRuns();
      }
      if (!done) throw Error("逐字稿視窗數量超出保護範圍");
      toast("✅ 已完成逐字稿結構拆分，段落已放入人工審核。");
      factoryState.reviewKind = "transcript";
      syncFactoryReviewKindButtons();
      await loadFactoryReviewItems(true);
    } catch (error) {
      toast(
        "⚠️ 逐字稿處理已停止，系統沒有自動重試。請在工作紀錄按「繼續處理」：" +
          error.message,
      );
      await loadTranscriptRuns(true);
    } finally {
      factorySetLoading("transcript-run", false, $("factoryGenerateBtn"));
    }
  }

  async function confirmAndProcessTranscript() {
    const preview = factoryState.preview,
      warnings = factoryPreviewWarnings(preview),
      piiReason = $("factoryPiiOverrideReason").value.trim(),
      runId = String(preview?.run_id || "");
    if (!runId) return toast("⚠️ 逐字稿預覽缺少工作識別碼。");
    if (!$("factoryRightsConfirmed").checked)
      return toast("⚠️ 請先確認你有權使用內容。");
    if (!$("factoryAnonymizedConfirmed").checked)
      return toast("⚠️ 請先確認內容已匿名化。");
    if (!$("factoryThirdPartyConfirmed").checked)
      return toast("⚠️ 請先確認第三方 AI 傳送警告。");
    if (warnings.length && !piiReason)
      return toast("⚠️ 預覽有個人資料警告，請填寫覆寫理由。");
    try {
      await api(
        `/api/ai-training/factory/transcript-runs/${encodeURIComponent(runId)}/confirm`,
        {
          method: "POST",
          body: JSON.stringify({
            preview_token: preview.preview_token || "",
            rights_confirmed: true,
            anonymized_confirmed: true,
            third_party_confirmed: true,
            pii_override_reason: piiReason,
          }),
        },
      );
      $("factoryPreviewDialog").close();
      factoryState.preview = null;
      await processTranscriptRun(runId);
    } catch (error) {
      toast("⚠️ 未能確認逐字稿處理：" + error.message);
    }
  }

  async function generateFactoryJob() {
    if (factoryState.loading.generate || !factoryState.preview) return;
    if (factoryState.preview.recipe_id === FACTORY_TRANSCRIPT_RECIPE) {
      await confirmAndProcessTranscript();
      return;
    }
    if (!$("factoryRightsConfirmed").checked)
      return toast("⚠️ 請先確認你有權使用內容。");
    if (!$("factoryAnonymizedConfirmed").checked)
      return toast("⚠️ 請先確認內容已匿名化。");
    if (!$("factoryThirdPartyConfirmed").checked)
      return toast("⚠️ 請先確認第三方 AI 傳送警告。");
    const warnings = factoryPreviewWarnings(factoryState.preview),
      piiReason = $("factoryPiiOverrideReason").value.trim();
    if (warnings.length && !piiReason)
      return toast("⚠️ 預覽有個人資料警告，請填寫覆寫理由。");
    const jobId = String(
      factoryState.preview.job_id || factoryState.preview.id || "",
    );
    if (!jobId) return toast("⚠️ 預覽缺少工作識別碼，請重新預覽。");
    factorySetLoading("generate", true, $("factoryGenerateBtn"));
    try {
      await api(
        `/api/ai-training/factory/jobs/${encodeURIComponent(jobId)}/generate`,
        {
          method: "POST",
          body: JSON.stringify({
            preview_token:
              factoryState.preview.preview_token || factoryState.preview.token || "",
            rights_confirmed: true,
            anonymized_confirmed: true,
            third_party_confirmed: true,
            pii_override_reason: piiReason,
          }),
        },
      );
      $("factoryPreviewDialog").close();
      factoryState.preview = null;
      toast("✅ 已完成生成，候選資料已放入人工審核。");
      await Promise.all([loadFactoryJobs(true), loadFactoryReviewItems(true)]);
    } catch (error) {
      factoryState.preview = null;
      $("factoryPreviewDialog").close();
      $("factoryRightsConfirmed").checked = false;
      $("factoryAnonymizedConfirmed").checked = false;
      $("factoryThirdPartyConfirmed").checked = false;
      $("factoryPiiOverrideReason").value = "";
      toast(
        "⚠️ 生成失敗；系統不會自動重試。請查看工作紀錄後重新預覽：" +
          error.message,
      );
      await loadFactoryJobs(true);
    } finally {
      factorySetLoading("generate", false, $("factoryGenerateBtn"));
    }
  }

  function renderFactoryJobs(payload) {
    const target = $("factoryJobs"),
      rows = factoryRows(payload);
    factoryClear(target);
    if (!rows.length) target.appendChild(factoryEl("p", "caption", "尚未有工作紀錄。"));
    rows.forEach((job) => {
      const card = factoryEl("article", "factory-item-card"),
        title = factoryEl(
          "strong",
          "",
          `${factoryRecipeLabel(job, factoryArtifactKind(job))} #${factoryId(job)}`,
        ),
        status = factoryEl(
          "p",
          "",
          `狀態：${job.status || "unknown"}｜模型：${job.model_label || "未提供"}｜數量：${job.item_count ?? job.requested_count ?? "-"}`,
        ),
        meta = factoryEl(
          "div",
          "factory-meta",
          `${job.created_at || ""}${job.source_title ? `｜${job.source_title}` : ""}`,
        );
      card.append(title, status, meta);
      if (job.error_message || job.error) {
        const warning = factoryEl(
          "p",
          "notice warn",
          `失敗說明：${job.error_message || job.error}。系統未自動重試。`,
        );
        card.appendChild(warning);
      }
      if (String(job.status || "").toLowerCase() === "failed") {
        const attempts = Number(
            job.attempt_count ?? job.latest_attempt_no ?? job.attempt_no ?? 0,
          ),
          actions = factoryEl("div", "actions"),
          retry = factoryEl(
            "button",
            "",
            attempts >= 3 ? "已達 3 次重試上限" : "重新預覽",
          );
        retry.type = "button";
        retry.disabled = attempts >= 3;
        retry.addEventListener("click", () => restoreFactoryRetryJob(job));
        actions.appendChild(retry);
        card.appendChild(actions);
      }
      target.appendChild(card);
    });
    factoryState.jobPages = factoryPageCount(payload, factoryState.jobPage);
    syncFactoryJobPagination();
  }

  async function loadFactoryJobs(reset = false) {
    if (reset) factoryState.jobPage = 1;
    const generation = factoryRequest("jobs");
    try {
      const payload = await api(
        `/api/ai-training/factory/jobs?page=${factoryState.jobPage}`,
      );
      if (factoryRequestIsCurrent("jobs", generation)) renderFactoryJobs(payload);
    } catch (error) {
      if (factoryRequestIsCurrent("jobs", generation))
        toast("⚠️ 工作紀錄載入失敗：" + error.message);
    }
  }

  async function withdrawTranscript(run, button) {
    const transcriptId = String(run?.transcript_id || "");
    if (!transcriptId || factoryState.loading["transcript-withdraw"]) return;
    factorySetLoading("transcript-withdraw", true, button);
    try {
      if (
        !(await confirmAsk(
          "撤回完整逐字稿",
          "所有處理工作、審核段落、衍生來源及發布版本都會失效。原文只保留作內部 audit，撤回後不會再由 API 顯示；已送到第三方 provider 的進行中請求無法取消，完成後只會結算用量並丟棄回覆。確定繼續？",
        ))
      )
        return;
      const entered = window.prompt(
        "請填寫撤回原因（必填，最多 1000 字）：",
        "",
      );
      if (entered === null) return;
      const reason = entered.trim();
      if (!reason) return toast("⚠️ 請填寫撤回原因。");
      if (reason.length > 1000)
        return toast("⚠️ 撤回原因不可超過 1000 字。");
      await api(
        `/api/ai-training/factory/transcripts/${encodeURIComponent(transcriptId)}/withdraw`,
        {
          method: "POST",
          body: JSON.stringify({ reason }),
        },
      );
      factoryState.selectedApproved.clear();
      toast("✅ 已撤回逐字稿並令所有衍生資料失效；原文只保留作 audit。");
      await Promise.all([
        loadTranscriptRuns(true),
        loadFactorySources(true),
        loadFactoryReviewItems(true),
        loadFactoryApprovedItems(true),
        loadFactoryReleases(true),
      ]);
    } catch (error) {
      toast("⚠️ 未能撤回逐字稿：" + error.message);
    } finally {
      factorySetLoading("transcript-withdraw", false, button);
    }
  }

  function renderTranscriptRuns(payload) {
    const rows = factoryRows(payload),
      target = $("factoryTranscriptRuns");
    factoryClear(target);
    if (!rows.length) {
      target.appendChild(factoryEl("p", "caption", "尚未有完整逐字稿工作。"));
    }
    rows.forEach((run) => {
      const runId = factoryId(run),
        statusValue = String(run.status || "unknown"),
        card = factoryEl("article", "factory-item-card"),
        title = factoryEl("strong", "", run.title || `逐字稿工作 #${runId}`),
        progress = factoryEl(
          "p",
          "",
          `狀態：${statusValue}｜模型：${run.model_label || "未提供"}｜視窗：${run.succeeded_windows || 0} / ${run.window_count || 0}｜段落：${run.segment_count || 0}`,
        ),
        meta = factoryEl(
          "div",
          "factory-meta",
          `${run.created_at || ""}${run.topic_text ? `｜${run.topic_text}` : ""}`,
        ),
        actions = factoryEl("div", "actions");
      card.append(title, progress, meta);
      if (["processing", "failed"].includes(statusValue)) {
        const resume = factoryEl("button", "", "繼續處理");
        resume.type = "button";
        resume.addEventListener("click", () => void processTranscriptRun(runId));
        actions.appendChild(resume);
      } else if (statusValue === "awaiting_review") {
        const review = factoryEl("button", "primary", "前往逐段審核");
        review.type = "button";
        review.addEventListener("click", () => {
          factoryState.reviewKind = "transcript";
          syncFactoryReviewKindButtons();
          document.querySelector('[data-factory-pane="factory-review"]').click();
        });
        actions.appendChild(review);
      }
      if (!run.withdrawn_at) {
        const withdraw = factoryEl("button", "danger", "撤回逐字稿");
        withdraw.type = "button";
        withdraw.addEventListener("click", () => {
          void withdrawTranscript(run, withdraw);
        });
        actions.appendChild(withdraw);
      } else {
        card.appendChild(factoryEl(
          "p",
          "notice warn",
          "此逐字稿已撤回；原文及 lineage 只保留作內部 audit，不能再處理或檢視。",
        ));
      }
      if (actions.children.length) card.appendChild(actions);
      target.appendChild(card);
    });
    factoryState.transcriptRunPages = factoryPageCount(
      payload, factoryState.transcriptRunPage,
    );
    syncFactoryJobPagination();
  }

  function syncFactoryJobPagination() {
    const transcript = factoryState.selectedRecipe === FACTORY_TRANSCRIPT_RECIPE,
      page = transcript ? factoryState.transcriptRunPage : factoryState.jobPage,
      pages = transcript ? factoryState.transcriptRunPages : factoryState.jobPages;
    $("factoryJobsPage").textContent = `第 ${page} / ${pages} 頁`;
    $("factoryJobsPrev").disabled = page <= 1;
    $("factoryJobsNext").disabled = page >= pages;
  }

  async function loadTranscriptRuns(reset = false) {
    if (reset) factoryState.transcriptRunPage = 1;
    const generation = factoryRequest("transcript-runs");
    try {
      const value = await api(
        `/api/ai-training/factory/transcript-runs?page=${factoryState.transcriptRunPage}`,
      );
      if (factoryRequestIsCurrent("transcript-runs", generation)) {
        renderTranscriptRuns(value);
      }
    } catch (error) {
      if (factoryRequestIsCurrent("transcript-runs", generation)) {
        toast("⚠️ 完整逐字稿工作載入失敗：" + error.message);
      }
    }
  }

  function renderFactoryReviewItem() {
    const rows = factoryState.reviewItems,
      item = rows[factoryState.reviewIndex],
      hasItem = Boolean(item);
    $("factoryReviewEmpty").classList.toggle("hidden", hasItem);
    $("factoryReviewWorkspace").classList.toggle("hidden", !hasItem);
    $("factoryReviewPrevItem").disabled = !hasItem || factoryState.reviewIndex <= 0;
    $("factoryReviewNextItem").disabled =
      !hasItem || factoryState.reviewIndex >= rows.length - 1;
    $("factoryReviewPrevPage").disabled = factoryState.reviewPage <= 1;
    $("factoryReviewNextPage").disabled =
      factoryState.reviewPage >= factoryState.reviewPages;
    $("factoryReviewPosition").textContent = hasItem
      ? `本頁第 ${factoryState.reviewIndex + 1} / ${rows.length} 項｜第 ${factoryState.reviewPage} / ${factoryState.reviewPages} 頁`
      : `第 ${factoryState.reviewPage} / ${factoryState.reviewPages} 頁`;
    factoryState.reviewContext = null;
    if (!hasItem) return;
    if (factoryState.reviewKind === "transcript") {
      const payload = factoryCandidatePayload(item),
        confidence = Number(payload.confidence),
        confidenceText = Number.isFinite(confidence) ? `${confidence}%` : "未提供";
      $("factoryReviewSourceMeta").textContent =
        `${item.transcript_title || "完整逐字稿"}｜第 ${item.sequence_no || "-"} / ${item.run_segment_count || "-"} 段｜原文位置 ${item.start_offset}-${item.end_offset}`;
      $("factoryReviewSource").textContent = "正在載入原文前後文…";
      $("factoryReviewCandidateMeta").textContent =
        `完整逐字稿結構拆分｜信心：${confidenceText}｜請核對辯位、立場及環節；原文邊界只供檢視`;
      $("factoryTranscriptReviewFields").classList.remove("hidden");
      $("factoryReviewPayloadLabel").classList.add("hidden");
      $("factoryTranscriptSequence").value = String(
        payload.sequence_no || item.sequence_no || "",
      );
      $("factoryTranscriptStart").value = String(payload.start_offset ?? "");
      $("factoryTranscriptEnd").value = String(payload.end_offset ?? "");
      $("factoryTranscriptSpeaker").value = String(payload.speaker_label || "");
      $("factoryTranscriptSide").value = String(payload.side || "unknown");
      $("factoryTranscriptStage").value = String(payload.stage || "unknown");
      $("factoryTranscriptConfidence").value = String(payload.confidence ?? "");
      $("factoryTranscriptReviewItems").value = Array.isArray(payload.review_items)
        ? payload.review_items.join("\n")
        : "";
      $("factoryTranscriptQuote").textContent = String(
        payload.full_text || payload.quote || "",
      );
      $("factoryReviewPayload").value = factoryPrettyJson(payload);
      $("factoryReviewNote").value = "";
      void loadTranscriptSegmentContext(item);
      return;
    }
    const source = item.source && typeof item.source === "object" ? item.source : item,
      sourceTitle =
        source.title || item.source_title || item.topic_text || `來源 #${item.source_id || "-"}`,
      artifact = factoryArtifactKind(item),
      creator =
        item.job_created_by || item.created_by || item.creator_user_id || "未提供";
    $("factoryTranscriptReviewFields").classList.add("hidden");
    $("factoryReviewPayloadLabel").classList.remove("hidden");
    $("factoryReviewSourceMeta").textContent =
      `${sourceTitle}｜${source.topic_text || item.topic_text || "未設辯題"}｜${source.side || item.source_side || item.side || "neutral"}`;
    $("factoryReviewSource").textContent = factorySourceText(item);
    $("factoryReviewCandidateMeta").textContent =
      `${FACTORY_RECIPE_LABELS[artifact] || artifact}｜建立者：${creator}｜版本：${item.revision ?? 0}`;
    $("factoryReviewPayload").value = factoryPrettyJson(factoryCandidatePayload(item));
    $("factoryReviewNote").value = "";
  }

  async function loadTranscriptSegmentContext(item) {
    const itemId = factoryId(item),
      generation = factoryRequest("transcript-segment-context");
    try {
      const context = await api(
        `/api/ai-training/factory/transcript-segments/${encodeURIComponent(itemId)}/context`,
      );
      const current = factoryState.reviewItems[factoryState.reviewIndex];
      if (
        !factoryRequestIsCurrent("transcript-segment-context", generation) ||
        factoryId(current) !== itemId
      )
        return;
      const contextText = String(context.context_text || ""),
        contextCodePoints = Array.from(contextText),
        localStart = Number(context.start_offset) - Number(context.context_start),
        localEnd = Number(context.end_offset) - Number(context.context_start),
        before = contextCodePoints.slice(0, Math.max(0, localStart)).join(""),
        selected = contextCodePoints
          .slice(Math.max(0, localStart), Math.max(0, localEnd))
          .join(""),
        after = contextCodePoints.slice(Math.max(0, localEnd)).join("");
      factoryState.reviewContext = context;
      $("factoryTranscriptStart").min = String(context.context_start);
      $("factoryTranscriptStart").max = String(context.context_end - 1);
      $("factoryTranscriptEnd").min = String(context.context_start + 1);
      $("factoryTranscriptEnd").max = String(context.context_end);
      $("factoryReviewSource").textContent =
        `${before}\n\n【本段開始】\n${selected}\n【本段結束】\n\n${after}`;
      updateTranscriptReviewQuote();
    } catch (error) {
      if (factoryRequestIsCurrent("transcript-segment-context", generation)) {
        $("factoryReviewSource").textContent =
          `原文前後文載入失敗：${error.message}`;
      }
    }
  }

  function transcriptReviewPayloadFromFields() {
    const context = factoryState.reviewContext;
    if (!context) throw Error("原文前後文尚未載入");
    const start = Number(context.start_offset),
      end = Number(context.end_offset),
      contextStart = Number(context.context_start),
      contextEnd = Number(context.context_end);
    if (!Number.isInteger(start) || !Number.isInteger(end) || end <= start) {
      throw Error("原文起止位置不正確");
    }
    if (start < contextStart || end > contextEnd) {
      throw Error("原文邊界超出目前顯示範圍");
    }
    const quote = Array.from(String(context.context_text || ""))
      .slice(start - contextStart, end - contextStart)
      .join("");
    if (!quote) throw Error("完整發言不可留空");
    const reviewItems = $("factoryTranscriptReviewItems").value
      .split("\n")
      .map((value) => value.trim())
      .filter(Boolean);
    if (reviewItems.length > 8) throw Error("需人工確認項目最多八項");
    if (reviewItems.some((value) => value.length > 300)) {
      throw Error("每項人工確認內容最多三百字");
    }
    const sequence = Number($("factoryTranscriptSequence").value),
      confidence = Number($("factoryTranscriptConfidence").value),
      speaker = $("factoryTranscriptSpeaker").value.trim();
    if (!Number.isInteger(sequence) || sequence < 1) throw Error("發言次序不正確");
    if (!Number.isInteger(confidence) || confidence < 0 || confidence > 100) {
      throw Error("判斷信心必須為零至一百的整數");
    }
    if (!speaker) throw Error("辯位不可留空");
    return {
      sequence_no: sequence,
      start_offset: start,
      end_offset: end,
      quote,
      speaker_label: speaker,
      side: $("factoryTranscriptSide").value,
      stage: $("factoryTranscriptStage").value,
      full_text: quote,
      confidence,
      review_items: reviewItems,
    };
  }

  function updateTranscriptReviewQuote() {
    if (!factoryState.reviewContext) return;
    try {
      const payload = transcriptReviewPayloadFromFields();
      $("factoryTranscriptQuote").textContent = payload.full_text;
    } catch (error) {
      $("factoryTranscriptQuote").textContent = `未能抽取完整發言：${error.message}`;
    }
  }

  function syncFactoryReviewKindButtons() {
    document.querySelectorAll("[data-factory-review-kind]").forEach((button) => {
      button.classList.toggle(
        "active", button.dataset.factoryReviewKind === factoryState.reviewKind,
      );
    });
    $("factoryReviewHelp").textContent = factoryState.reviewKind === "transcript"
      ? "每次只審核一段。請按原文核對及修正發言資料，系統會保留 AI 原稿。"
      : "每次只審核一項。你可以修正結構化 JSON，系統仍會保留 AI 原稿。";
  }

  function renderFactoryReviewItems(payload) {
    factoryState.reviewItems = factoryRows(payload);
    factoryState.reviewIndex = 0;
    factoryState.reviewPages = factoryPageCount(payload, factoryState.reviewPage);
    renderFactoryReviewItem();
  }

  async function loadFactoryReviewItems(reset = false) {
    if (reset) factoryState.reviewPage = 1;
    const generation = factoryRequest("review-items");
    try {
      const endpoint = factoryState.reviewKind === "transcript"
        ? "/api/ai-training/factory/transcript-segments"
        : "/api/ai-training/factory/items";
      const payload = await api(
        `${endpoint}?status=pending&page=${factoryState.reviewPage}`,
      );
      if (factoryRequestIsCurrent("review-items", generation))
        renderFactoryReviewItems(payload);
    } catch (error) {
      if (factoryRequestIsCurrent("review-items", generation))
        toast("⚠️ 待審核資料載入失敗：" + error.message);
    }
  }

  async function reviewFactoryItem(status) {
    if (factoryState.loading.review) return;
    const item = factoryState.reviewItems[factoryState.reviewIndex];
    if (!item) return;
    let reviewedPayload;
    try {
      reviewedPayload = factoryState.reviewKind === "transcript" && status === "approved"
        ? transcriptReviewPayloadFromFields()
        : JSON.parse($("factoryReviewPayload").value);
    } catch (error) {
      return toast(`⚠️ 候選資料未能通過檢查：${error.message}`);
    }
    const note = $("factoryReviewNote").value.trim();
    if (status === "rejected" && !note)
      return toast("⚠️ 拒絕資料時請填寫審核備註。");
    const approveButton = $("factoryReviewApprove"),
      rejectButton = $("factoryReviewReject");
    factorySetLoading("review", true, approveButton, rejectButton);
    try {
      const endpoint = factoryState.reviewKind === "transcript"
        ? `/api/ai-training/factory/transcript-segments/${encodeURIComponent(factoryId(item))}/review`
        : `/api/ai-training/factory/items/${encodeURIComponent(factoryId(item))}/review`;
      await api(
        endpoint,
        {
          method: "POST",
          body: JSON.stringify({
            reviewed_payload: reviewedPayload,
            status,
            note,
            expected_revision: Number(item.revision ?? item.expected_revision ?? 0),
          }),
        },
      );
      toast(
        status === "approved"
          ? factoryState.reviewKind === "transcript"
            ? "✅ 已批准段落並建立可供其他產品使用的來源。"
            : "✅ 已批准資料。"
          : "✅ 已拒絕資料。",
      );
      await Promise.all([
        loadFactoryReviewItems(),
        loadFactoryApprovedItems(true),
        loadFactorySources(true),
      ]);
    } catch (error) {
      toast("⚠️ 審核未能儲存：" + error.message);
    } finally {
      factorySetLoading("review", false, approveButton, rejectButton);
    }
  }

  function updateFactoryReleaseSelection() {
    const kind = $("factoryReleaseKind").value,
      selected = [...factoryState.selectedApproved.values()].filter(
        (item) => factoryDatasetKind(item) === kind,
      );
    $("factoryReleaseSelection").textContent = selected.length
      ? `已選 ${selected.length} 項 ${kind.toUpperCase()} 資料。發佈後內容及順序不可改寫。`
      : `尚未選擇 ${kind.toUpperCase()} 資料。`;
    $("factoryReleaseSubmit").disabled = !selected.length;
  }

  async function withdrawFactoryItem(item) {
    if (
      !(await confirmAsk(
        "撤回已批准資料",
        "資料及審計紀錄會保留，已發佈的衍生版本會標記為無效。確定繼續？",
      ))
    )
      return;
    try {
      await api(
        `/api/ai-training/factory/items/${encodeURIComponent(factoryId(item))}/withdraw`,
        { method: "POST" },
      );
      factoryState.selectedApproved.delete(factoryId(item));
      toast("✅ 已撤回資料並更新衍生版本狀態。");
      await Promise.all([loadFactoryApprovedItems(), loadFactoryReleases(true)]);
    } catch (error) {
      toast("⚠️ 未能撤回資料：" + error.message);
    }
  }

  function renderFactoryApprovedItems(payload) {
    const rows = factoryRows(payload),
      target = $("factoryApprovedItems"),
      selectedKind = $("factoryReleaseKind").value;
    factoryState.approvedItems = rows;
    factoryClear(target);
    if (!rows.length)
      target.appendChild(factoryEl("p", "caption", "現時沒有已批准資料。"));
    rows.forEach((item) => {
      const itemId = factoryId(item),
        itemKind = factoryDatasetKind(item),
        card = factoryEl("article", "factory-item-card"),
        choice = factoryEl("label", "factory-release-choice"),
        checkbox = document.createElement("input"),
        body = factoryEl("div"),
        artifact = factoryArtifactKind(item),
        title = factoryEl(
          "strong",
          "",
          `${FACTORY_RECIPE_LABELS[artifact] || artifact || itemKind.toUpperCase()} #${itemId}`,
        ),
        meta = factoryEl(
          "div",
          "factory-meta",
          `${itemKind.toUpperCase()}｜審核者：${item.reviewed_by || "未提供"}｜${item.reviewed_at || item.updated_at || ""}`,
        ),
        preview = factoryEl(
          "pre",
          "json",
          factoryPrettyJson(factoryCandidatePayload(item)).slice(0, 1200),
        ),
        actions = factoryEl("div", "actions"),
        withdraw = factoryEl("button", "danger", "撤回資料");
      checkbox.type = "checkbox";
      checkbox.checked = factoryState.selectedApproved.has(itemId);
      checkbox.disabled = itemKind !== selectedKind;
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) factoryState.selectedApproved.set(itemId, item);
        else factoryState.selectedApproved.delete(itemId);
        updateFactoryReleaseSelection();
      });
      withdraw.type = "button";
      withdraw.addEventListener("click", () => withdrawFactoryItem(item));
      body.append(title, meta, preview);
      choice.append(checkbox, body);
      actions.appendChild(withdraw);
      card.append(choice, actions);
      target.appendChild(card);
    });
    factoryState.approvedPages = factoryPageCount(payload, factoryState.approvedPage);
    $("factoryApprovedPage").textContent =
      `第 ${factoryState.approvedPage} / ${factoryState.approvedPages} 頁`;
    $("factoryApprovedPrev").disabled = factoryState.approvedPage <= 1;
    $("factoryApprovedNext").disabled =
      factoryState.approvedPage >= factoryState.approvedPages;
    updateFactoryReleaseSelection();
  }

  async function loadFactoryApprovedItems(reset = false) {
    if (reset) factoryState.approvedPage = 1;
    const generation = factoryRequest("approved-items");
    try {
      const payload = await api(
        `/api/ai-training/factory/items?status=approved&page=${factoryState.approvedPage}`,
      );
      if (factoryRequestIsCurrent("approved-items", generation))
        renderFactoryApprovedItems(payload);
    } catch (error) {
      if (factoryRequestIsCurrent("approved-items", generation))
        toast("⚠️ 已批准資料載入失敗：" + error.message);
    }
  }

  function renderFactoryReleases(payload) {
    const rows = factoryRows(payload),
      target = $("factoryReleases");
    factoryClear(target);
    if (!rows.length) target.appendChild(factoryEl("p", "caption", "尚未有已發佈版本。"));
    rows.forEach((release) => {
      const releaseId = factoryId(release),
        invalid =
          Boolean(release.invalidated_at) ||
          ["invalidated", "withdrawn"].includes(
            String(release.status || "").toLowerCase(),
          ),
        status = String(
          release.status || (invalid ? "invalidated" : "published"),
        ).toLowerCase(),
        card = factoryEl("article", "factory-item-card"),
        title = factoryEl(
          "strong",
          "",
          `${String(release.dataset_kind || release.release_kind || release.kind || "").toUpperCase()} 版本 ${release.version || release.version_no || releaseId}`,
        ),
        meta = factoryEl(
          "p",
          "factory-meta",
          `狀態：${status}｜${release.item_count ?? "-"} 項｜${release.published_at || release.created_at || ""}｜SHA-256：${release.manifest_sha256 || release.sha256 || "-"}`,
        );
      card.append(title, meta);
      if (invalid) {
        card.appendChild(
          factoryEl(
            "p",
            "notice warn",
            `⚠️ 此版本已${status === "withdrawn" ? "撤回" : "失效"}，不可再下載或用作訓練／RAG。`,
          ),
        );
      } else {
        const actions = factoryEl("div", "actions"),
          jsonl = factoryEl("a", "button", "下載 JSONL"),
          manifest = factoryEl("a", "button", "下載 Manifest");
        jsonl.href = `/api/ai-training/factory/releases/${encodeURIComponent(releaseId)}/export.jsonl`;
        manifest.href = `/api/ai-training/factory/releases/${encodeURIComponent(releaseId)}/manifest.json`;
        actions.append(jsonl, manifest);
        card.appendChild(actions);
      }
      target.appendChild(card);
    });
    factoryState.releasePages = factoryPageCount(payload, factoryState.releasePage);
    $("factoryReleasesPage").textContent =
      `第 ${factoryState.releasePage} / ${factoryState.releasePages} 頁`;
    $("factoryReleasesPrev").disabled = factoryState.releasePage <= 1;
    $("factoryReleasesNext").disabled =
      factoryState.releasePage >= factoryState.releasePages;
  }

  async function loadFactoryReleases(reset = false) {
    if (reset) factoryState.releasePage = 1;
    const generation = factoryRequest("releases");
    try {
      const payload = await api(
        `/api/ai-training/factory/releases?page=${factoryState.releasePage}`,
      );
      if (factoryRequestIsCurrent("releases", generation))
        renderFactoryReleases(payload);
    } catch (error) {
      if (factoryRequestIsCurrent("releases", generation))
        toast("⚠️ 版本紀錄載入失敗：" + error.message);
    }
  }

  async function publishFactoryRelease() {
    if (factoryState.loading.release) return;
    const kind = $("factoryReleaseKind").value,
      selected = [...factoryState.selectedApproved.entries()].filter(
        ([, item]) => factoryDatasetKind(item) === kind,
      ),
      itemIds = selected.map(([itemId]) => itemId);
    if (!itemIds.length) return toast("⚠️ 請先勾選同一類型的已批准資料。");
    const releaseMax = Number(
      factoryState.bootstrap?.limits?.release_max_items || 500,
    );
    if (itemIds.length > releaseMax)
      return toast(`⚠️ 每個版本最多收錄 ${releaseMax} 項資料。`);
    if (
      !(await confirmAsk(
        `發佈 ${kind.toUpperCase()} 版本`,
        `將發佈 ${itemIds.length} 項資料。版本一經發佈就不可改寫，確定繼續？`,
      ))
    )
      return;
    factorySetLoading("release", true, $("factoryReleaseSubmit"));
    try {
      await api("/api/ai-training/factory/releases", {
        method: "POST",
        body: JSON.stringify({
          dataset_kind: kind,
          item_ids: itemIds,
          note: $("factoryReleaseNote").value.trim(),
        }),
      });
      itemIds.forEach((itemId) => factoryState.selectedApproved.delete(itemId));
      $("factoryReleaseNote").value = "";
      toast("✅ 已發佈不可改寫版本。");
      await Promise.all([loadFactoryApprovedItems(true), loadFactoryReleases(true)]);
    } catch (error) {
      toast("⚠️ 版本未能發佈：" + error.message);
    } finally {
      factorySetLoading("release", false, $("factoryReleaseSubmit"));
      updateFactoryReleaseSelection();
    }
  }

  async function createFactoryPasteSource() {
    if (factoryState.loading.paste) return;
    const submit = $("factoryPasteSubmit");
    factorySetLoading("paste", true, submit);
    try {
      const result = await api("/api/ai-training/factory/sources", {
        method: "POST",
        body: JSON.stringify({
          kind: "paste",
          title: $("factoryPasteTitle").value.trim(),
          topic_text: $("factoryPasteTopic").value.trim(),
          side: $("factoryPasteSide").value,
          source_note: $("factoryPasteSourceNote").value.trim(),
          rights_basis: $("factoryPasteRightsBasis").value.trim(),
          content_text: $("factoryPasteContent").value.trim(),
          language: $("factoryPasteLanguage").value,
        }),
      });
      $("factoryPasteForm").reset();
      const source = result.source || result.item || result;
      setFactoryRetryMode();
      if (factoryId(source)) {
        factoryState.selectedSource = source;
        $("factorySelectedSource").textContent =
          `已選：${source.title || `來源 #${factoryId(source)}`}｜貼上資料`;
      }
      $("factorySourceKind").value = "paste";
      toast("✅ 已儲存不可原位改寫的來源版本。");
      await loadFactorySources(true);
    } catch (error) {
      toast("⚠️ 來源未能儲存：" + error.message);
    } finally {
      factorySetLoading("paste", false, submit);
    }
  }

  async function loadFactoryWorkspace() {
    try {
      await loadFactoryBootstrap();
    } catch (_error) {
      return;
    }
    await Promise.all([
      loadFactorySources(),
      loadFactoryJobs(),
      loadTranscriptRuns(),
      loadFactoryReviewItems(),
      loadFactoryApprovedItems(),
      loadFactoryReleases(),
    ]);
  }
  function chooseScript() {
    const mode = $("scriptType").value,
      all = data.scripts.filter((s) => s.script_type === mode),
      done = new Set(
        data.my_recordings
          .filter((r) => ["pending", "accepted"].includes(r.status))
          .map((r) => r.script_id),
      );
    current = all.find((s) => !done.has(s.id) && !skipped.has(s.id));
    const n = all.filter((s) => done.has(s.id)).length;
    $("modeHelp").textContent =
      mode === "short"
        ? "系統會依次顯示你尚未錄製的練習短句，錄好一句便自動跳至下一句。"
        : "系統會依次顯示完整稿的一段段內容，逐段錄製、逐段提交。";
    $("recordProgress").innerHTML =
      `<p class="caption">已錄 ${n} / ${all.length}</p><div class="progress"><i style="width:${all.length ? (100 * n) / all.length : 0}%"></i></div>`;
    let meta = "";
    if (current && mode === "full") {
      const segments = all.filter(
          (s) => s.manuscript_id === current.manuscript_id,
        ),
        pos = segments.findIndex((s) => s.id === current.id) + 1;
      meta = `<div class="caption">稿件：${esc(current.manuscript_title || current.manuscript_id)}　·　第 ${pos} / ${segments.length} 段</div>`;
    }
    $("script").innerHTML = current
      ? `${meta}<div class="script"><b>請照讀</b><p>${esc(current.text)}</p><span class="caption">${esc(current.id)}</span></div>`
      : `<p class="caption">${skipped.size ? `其餘內容已全部錄畢；你本次跳過了 ${skipped.size} 句。` : "此模式的內容你已全部錄畢，感謝參與。"}</p>`;
    $("recordBtn").disabled = !current;
    $("skipBtn").disabled = !current;
    $("resetSkipped").classList.toggle("hidden", !skipped.size);
  }
  async function load() {
    const fallback = $("loadFallback");
    fallback?.classList.remove("hidden");
    if ($("loadFallbackTitle"))
      $("loadFallbackTitle").textContent = "正在載入 AI 訓練頁面…";
    if ($("loadFallbackMessage"))
      $("loadFallbackMessage").textContent = "正在核對登入及訓練資料。";
    busy(true);
    try {
      data = await api("/api/ai-training/data");
      $("login").classList.add("hidden");
      $("app").classList.remove("hidden");
      fallback?.classList.add("hidden");
      $("consentText").textContent = data.consent_text;
      $("ttsBlocked").classList.toggle("hidden", data.is_allowed);
      $("ttsBlocked").textContent = data.is_admin
        ? "你並非 TTS 錄音收集名單成員；你仍可使用管理員分頁。"
        : "你暫時未獲加入 TTS 錄音收集名單；仍可於「LLM 文字資料提交」分頁提交辯論文字資料。";
      $("consent").classList.toggle(
        "hidden",
        !data.is_allowed || data.consented,
      );
      $("recorder").classList.toggle(
        "hidden",
        !data.is_allowed || !data.consented,
      );
      $("adminTab").classList.toggle("hidden", !data.is_admin);
      const budget = data.bandwidth_budget || {},
        storage = data.storage_budget || {},
        budgetEl = $("trainingBandwidthUsage"),
        stopGb = Number(storage.stop_bytes || 0) / 1e9,
        warnGb = Number(storage.warn_bytes || 0) / 1e9,
        fmtLimit = (value) =>
          value.toFixed(2).replace(/\.00$/, "").replace(/0$/, "");
      if (budgetEl)
        budgetEl.textContent = `本月系統已記錄約 ${(Number(budget.total_bytes || 0) / 1e9).toFixed(2)}GB；目前保護階段：${budget.stage || 0}。R2 約 ${(Number(storage.total_bytes || 0) / 1e9).toFixed(2)}GB / ${stopGb ? fmtLimit(stopGb) + "GB" : "未設定"}${storage.warning ? `，已進入${fmtLimit(warnGb)}GB警告區` : ""}。`;
      chooseScript();
      loadCollections();
      if (data.is_admin) loadAdmin();
    } catch (e) {
      $("app").classList.add("hidden");
      if (e.message === "未登入") {
        fallback?.classList.add("hidden");
        $("login").classList.remove("hidden");
      } else {
        $("login").classList.add("hidden");
        fallback?.classList.remove("hidden");
        if ($("loadFallbackTitle"))
          $("loadFallbackTitle").textContent = "AI 訓練頁面暫時未能載入";
        if ($("loadFallbackMessage"))
          $("loadFallbackMessage").textContent =
            e.message + "。請重新載入；如問題持續，請通知 developer。";
        toast("⚠️ " + e.message);
      }
    } finally {
      busy(false);
    }
  }
  function renderScriptInventory() {
    const rows = (window.inventory?.scripts || []).filter(
        (x) => x.script_type !== "full",
      ),
      pages = Math.max(1, Math.ceil(rows.length / 5));
    scriptPage = Math.min(Math.max(1, scriptPage), pages);
    const shown = rows.slice((scriptPage - 1) * 5, scriptPage * 5);
    const cards =
      shown
        .map(
          (x) => `<div class="script">
            <p>${esc(x.text)}</p>
            <span class="caption">
              ${esc(x.id)}｜${esc(x.category)}｜${x.is_active ? "🟢 啟用中" : "⚪ 已停用"}
            </span>
            <div class="actions">
              <button data-active-type="scripts" data-active-id="${esc(x.id)}" data-active="${!x.is_active}">
                ${x.is_active ? "停用" : "重新啟用"}
              </button>
              <button data-edit-script="${esc(x.id)}">編輯</button>
            </div>
          </div>`,
        )
        .join("") || '<p class="caption">句庫為空。</p>';
    $("scriptInventory").innerHTML = `<details>
      <summary>✏️ 編輯 / 停用現有句子</summary>
      <p class="caption">
        共 ${rows.length} 句，每頁顯示 5 句；現為第 ${scriptPage} / ${pages} 頁。
      </p>
      <div class="review-list">${cards}</div>
      <nav class="pager">
        <button data-script-page="${scriptPage - 1}" ${scriptPage <= 1 ? "disabled" : ""}>上一頁</button>
        <span>第 ${scriptPage} / ${pages} 頁</span>
        <button data-script-page="${scriptPage + 1}" ${scriptPage >= pages ? "disabled" : ""}>下一頁</button>
      </nav>
    </details>`;
    document.querySelectorAll("[data-script-page]").forEach(
      (b) =>
        (b.onclick = () => {
          scriptPage = +b.dataset.scriptPage;
          renderScriptInventory();
        }),
    );
  }
  async function loadAdmin() {
    const [stats, inv, ready] = await Promise.all([
      api("/api/ai-training/admin/stats"),
      api("/api/ai-training/inventory"),
      api("/api/ai-training/readiness"),
    ]);
    $("recordStats").textContent =
      stats.recordings.map((x) => `${x.status}: ${x.count}`).join(" ｜ ") ||
      "暫無錄音";
    $("llmStats").textContent =
      stats.llm.map((x) => `${x.status}: ${x.count}`).join(" ｜ ") ||
      "暫無資料";
    const speakerStatus = (ready.speakers || [])
      .map(
        (speaker) =>
          `<p><b>${esc(speaker.speaker_user_id)}</b>：accepted ${speaker.accepted_minutes || 0}分鐘（${speaker.accepted_clips || 0}段）｜現行授權 eligible ${speaker.eligible_clips || 0}段｜pending ${speaker.pending_minutes || 0}分鐘</p>`,
      )
      .join("");
    const speakerFilter = $("speakerFilter"),
      selectedSpeaker = speakerFilter.value,
      speakers = (ready.speakers || []).map((speaker) =>
        String(speaker.speaker_user_id || "").trim(),
      ),
      speakerOptions = speakers
        .filter(Boolean)
        .map((speaker) => `<option value="${esc(speaker)}">${esc(speaker)}</option>`)
        .join("");
    speakerFilter.innerHTML =
      '<option value="">全部錄音者</option>' + speakerOptions;
    speakerFilter.value = speakers.includes(selectedSpeaker)
      ? selectedSpeaker
      : "";
    syncRecordingExport();
    const llmSubmitterFilter = $("llmSubmitterFilter"),
      selectedSubmitter = llmSubmitterFilter.value,
      submitters = (stats.llm_submitters || []).map((row) =>
        String(row.submitted_by || "").trim(),
      ),
      submitterOptions = submitters
        .filter(Boolean)
        .map(
          (submitter) =>
            `<option value="${esc(submitter)}">${esc(submitter)}</option>`,
        )
        .join("");
    llmSubmitterFilter.innerHTML =
      '<option value="">全部提交者</option>' + submitterOptions;
    llmSubmitterFilter.value = submitters.includes(selectedSubmitter)
      ? selectedSubmitter
      : "";
    syncLlmExport();
    $("readinessSummary").innerHTML =
      `<p>Consent：${esc(ready.consent_version)}｜生效讀音字典：${ready.active_lexicon} / ${ready.gates.tts_min_lexicon}</p>${speakerStatus || "<p>暫無聲線資料。</p>"}`;
    window.inventory = inv;
    renderScriptInventory();
    $("manuscriptInventory").innerHTML =
      inv.manuscripts
        .map(
          (m) =>
            `<details><summary>${esc(m.title)}（${m.segments} 段）</summary><button data-active-type="manuscripts" data-active-id="${esc(m.id)}" data-active="${!m.is_active}">${m.is_active ? "停用整份" : "重新啟用整份"}</button></details>`,
        )
        .join("") || '<p class="caption">暫無完整稿。</p>';
  }
  function resetRecording() {
    if (recordStopTimer) {
      clearTimeout(recordStopTimer);
      recordStopTimer = null;
    }
    if (rec) {
      rec.onstop = null;
      try {
        rec.stop();
      } catch {}
      rec.stream?.getTracks().forEach((t) => t.stop());
      rec = null;
    }
    blob = null;
    pendingAudio = null;
    pendingUpload = null;
    recordedSeconds = 0;
    const preview = $("preview");
    if (preview.src.startsWith("blob:")) URL.revokeObjectURL(preview.src);
    preview.removeAttribute("src");
    preview.classList.add("hidden");
    $("submitRecord").disabled = true;
    $("manualAudio").classList.add("hidden");
    $("manualAudioConfirm").checked = false;
    $("recordBtn").textContent = "開始錄音";
    $("recordState").textContent = "選擇句子後開始錄音。";
  }
  const hexSha = async (b) =>
      [
        ...new Uint8Array(
          await crypto.subtle.digest("SHA-256", await b.arrayBuffer()),
        ),
      ]
        .map((x) => x.toString(16).padStart(2, "0"))
        .join(""),
    recordPayload = (manual) => ({
      script_id: current.id,
      mime_type: blob.type,
      duration_seconds: recordedSeconds,
      manual_review: manual,
    }),
    uploadRecordingR2 = async (p) => {
      const sha = await hexSha(blob),
        intent = await api("/api/ai-training/recordings/upload-intent", {
          method: "POST",
          body: JSON.stringify({
            script_id: p.script_id,
            mime_type: p.mime_type,
            byte_size: blob.size,
            sha256: sha,
          }),
        }),
        r = await fetch(intent.url, {
          method: "PUT",
          headers: intent.headers,
          body: blob,
        });
      if (!r.ok) throw Error(`R2 錄音上載失敗（HTTP ${r.status}）`);
      pendingUpload = { r2_upload_token: intent.upload_token };
      Object.assign(p, pendingUpload);
    };
  async function submitAudio(manual = false) {
    busy(true);
    try {
      const p = recordPayload(manual);
      if (pendingUpload) Object.assign(p, pendingUpload);
      else await uploadRecordingR2(p);
      if (!manual) {
        let check;
        try {
          check = await api("/api/ai-training/recordings/quality-check", {
            method: "POST",
            body: JSON.stringify(p),
          });
        } catch (e) {
          pendingAudio = { error: e.message };
          $("manualAudio").classList.remove("hidden");
          toast("⚠️ " + e.message);
          return;
        }
        if (!check.ok) {
          pendingAudio = check;
          toast("⚠️ " + check.message);
          return;
        }
        p.review_token = check.review_token;
      }
      await api("/api/ai-training/recordings", {
        method: "POST",
        body: JSON.stringify(p),
      });
      toast("✅ 錄音已提交，等待人工審核。");
      resetRecording();
      await load();
    } catch (e) {
      toast("⚠️ " + e.message);
    } finally {
      busy(false);
    }
  }
  document.querySelectorAll("[data-pane]").forEach(
    (b) =>
      (b.onclick = () => {
        document
          .querySelectorAll(".pane,.tabs>button[data-pane]")
          .forEach((x) => x.classList.remove("active"));
        $(b.dataset.pane).classList.add("active");
        b.classList.add("active");
      }),
  );
  document.querySelectorAll("[data-admin]").forEach(
    (b) =>
      (b.onclick = () => {
        document
          .querySelectorAll(".admin-pane,#adminTabs button")
          .forEach((x) => x.classList.remove("active"));
        $(b.dataset.admin).classList.add("active");
        b.classList.add("active");
        if (b.dataset.admin === "factory") void loadFactoryWorkspace();
      }),
  );
  document.querySelectorAll("[data-factory-pane]").forEach(
    (button) =>
      (button.onclick = () => {
        document
          .querySelectorAll(".factory-subpane,#factoryTabs button")
          .forEach((node) => node.classList.remove("active"));
        $(button.dataset.factoryPane).classList.add("active");
        button.classList.add("active");
        if (button.dataset.factoryPane === "factory-create") {
          if (factoryState.selectedRecipe === FACTORY_TRANSCRIPT_RECIPE) {
            void loadTranscriptRuns();
          } else {
            void Promise.all([loadFactorySources(), loadFactoryJobs()]);
          }
        } else if (button.dataset.factoryPane === "factory-review") {
          void loadFactoryReviewItems(true);
        } else if (button.dataset.factoryPane === "factory-approved") {
          void Promise.all([
            loadFactoryApprovedItems(true),
            loadFactoryReleases(true),
          ]);
        }
      }),
  );
  $("factoryPasteForm").onsubmit = (event) => {
    event.preventDefault();
    void createFactoryPasteSource();
  };
  $("factoryJobForm").onsubmit = (event) => {
    event.preventDefault();
    void previewFactoryJob();
  };
  $("factoryTranscriptForm").onsubmit = (event) => {
    event.preventDefault();
    void previewTranscript();
  };
  $("factoryTranscriptContent").addEventListener("input", () => {
    const maximum = Number(
      $("factoryTranscriptContent").maxLength ||
        factoryState.bootstrap?.limits?.transcript_max_chars ||
        200000,
    );
    $("factoryTranscriptCharCount").textContent =
      `${$("factoryTranscriptContent").value.length.toLocaleString()} / ${maximum.toLocaleString()} 字`;
  });
  $("factoryPreviewCancel").onclick = () => {
    factoryState.preview = null;
    $("factoryPreviewDialog").close();
  };
  $("factoryCancelRetry").onclick = () => {
    setFactoryRetryMode();
    factoryState.selectedSource = null;
    $("factoryManagerInstruction").value = "";
    $("factorySelectedSource").textContent = "尚未選擇來源。";
    void loadFactorySources();
  };
  $("factoryGenerateBtn").onclick = () => void generateFactoryJob();
  $("factorySourceSearchBtn").onclick = () => void loadFactorySources(true);
  $("factorySourceKind").onchange = () => void loadFactorySources(true);
  $("factorySourceSearch").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void loadFactorySources(true);
    }
  });
  $("factorySourcePrev").onclick = () => {
    if (factoryState.sourcePage <= 1) return;
    factoryState.sourcePage -= 1;
    void loadFactorySources();
  };
  $("factorySourceNext").onclick = () => {
    if (factoryState.sourcePage >= factoryState.sourcePages) return;
    factoryState.sourcePage += 1;
    void loadFactorySources();
  };
  $("factoryJobsPrev").onclick = () => {
    if (factoryState.selectedRecipe === FACTORY_TRANSCRIPT_RECIPE) {
      if (factoryState.transcriptRunPage <= 1) return;
      factoryState.transcriptRunPage -= 1;
      void loadTranscriptRuns();
      return;
    }
    if (factoryState.jobPage <= 1) return;
    factoryState.jobPage -= 1;
    void loadFactoryJobs();
  };
  $("factoryJobsNext").onclick = () => {
    if (factoryState.selectedRecipe === FACTORY_TRANSCRIPT_RECIPE) {
      if (factoryState.transcriptRunPage >= factoryState.transcriptRunPages) return;
      factoryState.transcriptRunPage += 1;
      void loadTranscriptRuns();
      return;
    }
    if (factoryState.jobPage >= factoryState.jobPages) return;
    factoryState.jobPage += 1;
    void loadFactoryJobs();
  };
  $("factoryReviewPrevItem").onclick = () => {
    if (factoryState.reviewIndex <= 0) return;
    factoryState.reviewIndex -= 1;
    renderFactoryReviewItem();
  };
  $("factoryReviewNextItem").onclick = () => {
    if (factoryState.reviewIndex >= factoryState.reviewItems.length - 1) return;
    factoryState.reviewIndex += 1;
    renderFactoryReviewItem();
  };
  $("factoryReviewPrevPage").onclick = () => {
    if (factoryState.reviewPage <= 1) return;
    factoryState.reviewPage -= 1;
    void loadFactoryReviewItems();
  };
  $("factoryReviewNextPage").onclick = () => {
    if (factoryState.reviewPage >= factoryState.reviewPages) return;
    factoryState.reviewPage += 1;
    void loadFactoryReviewItems();
  };
  $("factoryReviewApprove").onclick = () => void reviewFactoryItem("approved");
  $("factoryReviewReject").onclick = () => void reviewFactoryItem("rejected");
  document.querySelectorAll("[data-factory-review-kind]").forEach((button) => {
    button.onclick = () => {
      factoryState.reviewKind = button.dataset.factoryReviewKind || "standard";
      syncFactoryReviewKindButtons();
      void loadFactoryReviewItems(true);
    };
  });
  $("factoryApprovedPrev").onclick = () => {
    if (factoryState.approvedPage <= 1) return;
    factoryState.approvedPage -= 1;
    void loadFactoryApprovedItems();
  };
  $("factoryApprovedNext").onclick = () => {
    if (factoryState.approvedPage >= factoryState.approvedPages) return;
    factoryState.approvedPage += 1;
    void loadFactoryApprovedItems();
  };
  $("factoryReleaseKind").onchange = () => {
    if (factoryState.approvedItems)
      renderFactoryApprovedItems({
        items: factoryState.approvedItems,
        pages: factoryState.approvedPages,
      });
    else updateFactoryReleaseSelection();
  };
  $("factoryReleaseForm").onsubmit = (event) => {
    event.preventDefault();
    void publishFactoryRelease();
  };
  $("factoryReleasesPrev").onclick = () => {
    if (factoryState.releasePage <= 1) return;
    factoryState.releasePage -= 1;
    void loadFactoryReleases();
  };
  $("factoryReleasesNext").onclick = () => {
    if (factoryState.releasePage >= factoryState.releasePages) return;
    factoryState.releasePage += 1;
    void loadFactoryReleases();
  };
  $("loginForm").onsubmit = async (e) => {
    e.preventDefault();
    busy(true);
    try {
      await api("/api/committee/login", {
        method: "POST",
        body: JSON.stringify({
          user_id: $("user").value,
          password: $("password").value,
        }),
      });
      await load();
      toast("✅ 已登入。");
    } catch (x) {
      toast("⚠️ " + x.message);
    } finally {
      busy(false);
    }
  };
  $("consentBtn").onclick = async () => {
    if (!$("agree").checked) return toast("⚠️ 請先確認整體授權安排。");
    if (!$("voiceCloningConfirmed").checked)
      return toast("⚠️ 請明確確認聲線模型用途。");
    if (!$("cloudProcessingConfirmed").checked)
      return toast("⚠️ 請明確確認受控雲端處理用途。");
    await api("/api/ai-training/consent", {
      method: "POST",
      body: JSON.stringify({
        agreed: true,
        voice_cloning_confirmed: true,
        cloud_processing_confirmed: true,
      }),
    });
    load();
  };
  $("scriptType").onchange = () => {
    resetRecording();
    skipped.clear();
    chooseScript();
  };
  $("skipBtn").onclick = () => {
    if (current) {
      resetRecording();
      skipped.add(current.id);
      chooseScript();
    }
  };
  $("resetSkipped").onclick = () => {
    resetRecording();
    skipped.clear();
    chooseScript();
  };
  $("recordBtn").onclick = async () => {
    try {
      if (rec) {
        rec.stop();
        return;
      }
      resetRecording();
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true }),
        chunks = [],
        maxSeconds = Number(data.limits.max_duration_seconds || 60),
        maxBytes = Number(data.limits.max_audio_bytes || 2 * 1024 * 1024);
      let chunkBytes = 0;
      const activeRecorder = new MediaRecorder(stream);
      rec = activeRecorder;
      activeRecorder.ondataavailable = (e) => {
        if (!e.data?.size) return;
        chunks.push(e.data);
        chunkBytes += e.data.size;
        if (chunkBytes > maxBytes && activeRecorder.state === "recording") {
          activeRecorder.stop();
        }
      };
      activeRecorder.onstop = () => {
        if (recordStopTimer) {
          clearTimeout(recordStopTimer);
          recordStopTimer = null;
        }
        recordedSeconds = Math.max(
          1,
          Math.round((Date.now() - started) / 1000),
        );
        blob = new Blob(chunks, { type: activeRecorder.mimeType });
        $("preview").src = URL.createObjectURL(blob);
        $("preview").classList.remove("hidden");
        const tooLong = recordedSeconds > maxSeconds;
        const tooLarge = blob.size > maxBytes;
        $("submitRecord").disabled = tooLong || tooLarge;
        $("recordState").textContent = tooLong
          ? `錄音太長，請控制在 ${maxSeconds} 秒內。`
          : tooLarge
            ? `錄音檔案太大，請縮短錄音至 ${(maxBytes / 1024 / 1024).toFixed(2)}MB 以內。`
            : `已錄音（${recordedSeconds} 秒｜約 ${Math.round(blob.size / 1024)} KB），請先試聽。`;
        stream.getTracks().forEach((t) => t.stop());
        if (rec === activeRecorder) rec = null;
        $("recordBtn").textContent = "重新錄音";
      };
      started = Date.now();
      activeRecorder.start(1000);
      recordStopTimer = setTimeout(() => {
        if (activeRecorder.state === "recording") activeRecorder.stop();
      }, maxSeconds * 1000);
      $("recordBtn").textContent = "停止錄音";
      $("recordState").textContent = "錄音中…";
    } catch (e) {
      toast("⚠️ 未能使用咪高峰：" + e.message);
    }
  };
  $("submitRecord").onclick = () => submitAudio(false);
  $("manualAudioSubmit").onclick = () =>
    $("manualAudioConfirm").checked
      ? submitAudio(true)
      : toast("⚠️ 請先自行試聽及確認。");
  $("withdraw").onclick = async () => {
    if (
      await confirmAsk("撤回錄音同意", "既有錄音會標記為 withdrawn，確定？")
    ) {
      await api("/api/ai-training/consent", { method: "DELETE" });
      load();
    }
  };
  const llmPayload = (manual) => ({
    data_type: $("dataType").value,
    side: $("llmSide").value,
    title: $("llmTitle").value,
    topic_text: $("llmTopic").value,
    content_text: $("llmContent").value,
    source_note: $("llmSource").value,
    anonymized: $("anonymized").checked,
    permission_confirmed: $("permission").checked,
    manual_review: manual,
  });
  async function submitLlm(manual = false) {
    busy(true);
    try {
      const result = await api("/api/ai-training/llm", {
        method: "POST",
        body: JSON.stringify(llmPayload(manual)),
      });
      if (result.ok === false) {
        toast("⚠️ " + result.message);
        return;
      }
      toast("✅ " + result.message);
      $("llmForm").reset();
      $("manualLlm").classList.add("hidden");
      loadCollections();
    } catch (e) {
      if (e.status === 503) {
        pendingLlm = true;
        $("manualLlm").classList.remove("hidden");
      }
      toast("⚠️ " + e.message);
    } finally {
      busy(false);
    }
  }
  $("llmForm").onsubmit = (e) => {
    e.preventDefault();
    submitLlm(false);
  };
  $("clearLlm").onclick = () => {
    $("llmForm").reset();
    $("manualLlm").classList.add("hidden");
  };
  $("manualLlmSubmit").onclick = () =>
    $("manualLlmConfirm").checked
      ? submitLlm(true)
      : toast("⚠️ 請先確認資料適合提交。");
  $("recordFilter").onchange = () => loadRecordings(true);
  $("speakerFilter").onchange = () => {
    syncRecordingExport();
    loadRecordings(true);
  };
  $("recordExport").onclick = () => {
    const url = new URL($("recordExport").href, location.origin);
    url.searchParams.set("_fresh", String(Date.now()));
    $("recordExport").href = url.pathname + url.search;
  };
  $("llmFilter").onchange = () => loadLlmSubmissions(true);
  $("llmSubmitterFilter").onchange = () => {
    syncLlmExport();
    loadLlmSubmissions(true);
  };
  $("llmExport").onclick = () => {
    const url = new URL($("llmExport").href, location.origin);
    url.searchParams.set("_fresh", String(Date.now()));
    $("llmExport").href = url.pathname + url.search;
  };
  $("lexiconForm").onsubmit = (e) =>
    saveForm(e, "/api/ai-training/lexicon", {
      lexicon_id: $("lexiconId").value,
      term: $("term").value,
      reading: $("reading").value,
      jyutping: $("jyutping").value,
      example: $("example").value,
      note: $("lexNote").value,
      category: $("lexCategory").value,
    });
  $("scriptForm").onsubmit = (e) =>
    saveForm(e, "/api/ai-training/scripts", {
      script_id: $("scriptId").value,
      category: $("scriptCategory").value,
      text: $("scriptText").value,
      sort_order: +$("sortOrder").value,
    });
  $("manuscriptText").oninput = () =>
    ($("segmentPreview").textContent =
      `預計約 ${Math.ceil($("manuscriptText").value.length / 35)} 段；實際按細標點及每段最多 35 字切分。`);
  $("manuscriptForm").onsubmit = (e) =>
    saveForm(e, "/api/ai-training/manuscripts", {
      title: $("manuscriptTitle").value,
      text: $("manuscriptText").value,
      category: "完整稿",
      active: true,
    });
  async function saveForm(e, url, p) {
    e.preventDefault();
    busy(true);
    try {
      await api(url, { method: "POST", body: JSON.stringify(p) });
      toast("✅ 已儲存。");
      e.target.reset();
      load();
    } catch (x) {
      toast("⚠️ " + x.message);
    } finally {
      busy(false);
    }
  }
  $("coverageBtn").onclick = async () => {
    busy(true);
    try {
      const x = await api("/api/ai-training/coverage/ai", { method: "POST" }),
        a = x.analysis || {},
        gaps = a.gaps || [],
        items = a.suggested_scripts || [];
      suggestions = { items, deactivate_ids: [] };
      const gapMarkup = gaps
        .map(
          (gap) =>
            `<li>⚠️ <b>${esc(gap.area || "")}</b>：${esc(gap.why || "")}</li>`,
        )
        .join("");
      const itemMarkup = items
        .map(
          (item, index) => `<label class="check">
            <input type="checkbox" data-suggestion="new" data-index="${index}">
            [${esc(item.category || "AI 建議")}] ${esc(item.text || "")}
          </label>`,
        )
        .join("");
      $("coverageResult").innerHTML = [
        a.overall ? `<div class="notice">${esc(a.overall)}</div>` : "",
        a.well_covered?.length
          ? `<p><b>已足夠覆蓋：</b>${esc(a.well_covered.join("、"))}</p>`
          : "",
        gaps.length ? `<ul>${gapMarkup}</ul>` : "",
        items.length ? `<p><b>建議新增句子：</b></p>${itemMarkup}` : "",
      ].join("");
      $("applySuggestions").classList.toggle("hidden", !items.length);
    } catch (e) {
      toast("⚠️ " + e.message);
    } finally {
      busy(false);
    }
  };
  $("regenerateBtn").onclick = async () => {
    busy(true);
    try {
      const x = await api("/api/ai-training/regenerate-suggestions", {
          method: "POST",
        }),
        p = x.plan || {},
        items = p.new_scripts || [],
        deactivate = p.deactivate_candidates || [];
      suggestions = {
        items,
        deactivate_ids: deactivate.map((x) => String(x.script_id || "")),
      };
      const itemMarkup = items
        .map(
          (item, index) => `<label class="check">
            <input type="checkbox" checked data-suggestion="new" data-index="${index}">
            [${esc(item.category || "AI 建議")}] ${esc(item.text || "")}
          </label>`,
        )
        .join("");
      const deactivateMarkup = deactivate
        .map(
          (item, index) => `<label class="check">
            <input type="checkbox" data-suggestion="deactivate" data-index="${index}">
            ${esc(item.script_id || "")} ${item.reason ? `｜${esc(item.reason)}` : ""}
          </label>`,
        )
        .join("");
      $("coverageResult").innerHTML = [
        p.overall ? `<div class="notice">${esc(p.overall)}</div>` : "",
        items.length
          ? `<p><b>建議新增句子（預設全選）：</b></p>${itemMarkup}`
          : "",
        deactivate.length
          ? `<p><b>建議停用（只限未錄音句子）：</b></p>${deactivateMarkup}`
          : "",
      ].join("");
      $("applySuggestions").classList.toggle(
        "hidden",
        !(items.length || deactivate.length),
      );
    } catch (e) {
      toast("⚠️ " + e.message);
    } finally {
      busy(false);
    }
  };
  $("applySuggestions").onclick = async () => {
    const items = [
        ...document.querySelectorAll('[data-suggestion="new"]:checked'),
      ]
        .map((x) => suggestions.items[+x.dataset.index])
        .filter(Boolean),
      deactivate_ids = [
        ...document.querySelectorAll('[data-suggestion="deactivate"]:checked'),
      ]
        .map((x) => suggestions.deactivate_ids[+x.dataset.index])
        .filter(Boolean);
    if (!items.length && !deactivate_ids.length)
      return toast("⚠️ 請先選擇要套用的變更。");
    const x = await api("/api/ai-training/suggestions/apply", {
      method: "POST",
      body: JSON.stringify({ items, deactivate_ids }),
    });
    toast(`✅ 已新增 ${x.added} 句、停用 ${x.deactivated} 句。`);
    load();
  };
  $("deactivateComplete").onclick = async () => {
    if (
      await confirmAsk(
        "停用已完成內容",
        "短句逐句判斷；完整稿只會在整份稿所有啟用段落均由全體指定錄音者完成後一併停用。確定繼續？",
      )
    ) {
      const x = await api("/api/ai-training/scripts/deactivate-complete", {
        method: "POST",
      });
      toast(`✅ 已停用 ${x.deactivated} 段內容。`);
      load();
    }
  };
  document.body.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.review) {
      const note =
        document.querySelector(
          `[data-note="${b.dataset.review}-${b.dataset.id}"]`,
        )?.value || "";
      await api(`/api/ai-training/${b.dataset.review}/${b.dataset.id}/review`, {
        method: "POST",
        body: JSON.stringify({ status: b.dataset.status, note }),
      });
      toast("✅ 已更新審核結果。");
      loadCollections();
    } else if (b.dataset.withdrawLlm) {
      if (
        await confirmAsk(
          "撤回提交",
          "確定撤回這份文字資料？如已用於資料工廠，相關內容會一併失效。",
        )
      ) {
        await api(`/api/ai-training/llm/${b.dataset.withdrawLlm}`, {
          method: "DELETE",
        });
        loadCollections();
      }
    } else if (b.dataset.activeType) {
      await api(
        `/api/ai-training/${b.dataset.activeType}/${encodeURIComponent(b.dataset.activeId)}/active`,
        {
          method: "PATCH",
          body: JSON.stringify({ active: b.dataset.active === "true" }),
        },
      );
      load();
    } else if (b.dataset.editLex) {
      const x = window.lexPage[b.dataset.editLex];
      [
        ["lexiconId", "id"],
        ["term", "term"],
        ["reading", "reading"],
        ["jyutping", "jyutping"],
        ["example", "example"],
        ["lexNote", "note"],
        ["lexCategory", "category"],
      ].forEach(([a, k]) => ($(a).value = x[k] || ""));
    } else if (b.dataset.editScript) {
      const x = window.inventory.scripts.find(
        (s) => s.id === b.dataset.editScript,
      );
      $("scriptId").value = x.id;
      $("scriptCategory").value = x.category;
      $("scriptText").value = x.text;
      $("sortOrder").value = x.sort_order || 0;
      $("scriptText").focus();
    }
  });
  load();
})();
