/* Collaborative Competition Prep UI embedded in AI 辯論易. */
(() => {
  if (location.pathname !== "/ai-coach") return;

  const $ = (id) => document.getElementById(id);
  const toast = (message) => VoteUI.toast($("toast"), message);
  const busy = (value) => VoteUI.setBusy($("busy"), value);
  const mobileLayout = window.matchMedia("(max-width: 800px)");
  const state = {
    data: null,
    bundle: null,
    projectId: 0,
    loadGeneration: 0,
    loadedAfterLogin: false,
    pendingAiOperations: new Map(),
    activeManuscriptSlot: "",
    manuscriptFormBaseline: "",
  };
  const roleLabels = { owner: "擁有者", editor: "編輯者", viewer: "檢視者" };
  const manuscriptStatusLabels = {
    draft: "草稿", reviewed: "已審查", final: "定稿",
  };
  const weaknessCategoryLabels = {
    logic: "邏輯", evidence: "證據", definition: "定義",
    response: "回應", delivery: "表達", coordination: "全隊協作",
  };
  const weaknessStatusLabels = {
    open: "待處理", practicing: "練習中", passed: "已通過",
  };
  const slotLabels = {
    main: "主辯稿", dep1: "一副稿", dep2: "二副稿", dep3: "三副稿",
    closing: "結辯稿", interaction: "攻辯／自由辯論備忘",
    "other": "其他比賽有關資料",
  };
  const kindLabels = {
    mainline: "全隊主線", definition: "定義", standard: "評判標準",
    burden: "我方論證責任", argument: "我方論點",
    opponent_argument: "對方論點", attack: "我方攻擊",
    opponent_answer: "對方回應", rebuttal: "反駁",
    defence_floor: "最低防守線", concession: "可承認範圍", question: "追問",
  };
  const evidenceTypeLabels = {
    government: "政府", academic: "學術", news: "新聞", ngo: "非政府組織",
    industry: "業界", ai_research: "AI 搜尋摘要", other: "其他",
  };
  const evidenceScopeLabels = { our: "我方", opponent: "對方", both: "雙方" };
  const priorityLabels = { 1: "高", 2: "中", 3: "低" };

  async function api(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.detail || "操作失敗");
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function option(value, label) {
    const item = document.createElement("option");
    item.value = String(value ?? "");
    item.textContent = label;
    return item;
  }

  function button(label, action, className = "") {
    const item = document.createElement("button");
    item.type = "button";
    item.textContent = label;
    if (className) item.className = className;
    item.addEventListener("click", action);
    return item;
  }

  function row(title, note = "") {
    const wrapper = document.createElement("div");
    wrapper.className = "prep-list-row";
    const copy = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = title;
    copy.append(strong);
    if (note) {
      const caption = document.createElement("div");
      caption.className = "caption";
      caption.textContent = note;
      copy.append(caption);
    }
    const actions = document.createElement("div");
    actions.className = "actions";
    wrapper.append(copy, actions);
    return { wrapper, actions };
  }

  function filterText(...values) {
    return values.map((value) => String(value || "").toLocaleLowerCase("zh-HK")).join("\n");
  }

  function excerpt(value, limit = 180) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > limit ? `${text.slice(0, limit)}…` : text;
  }

  function assignmentMatches(value, filter) {
    if (!filter) return true;
    if (filter === "__unassigned__") return !value;
    return String(value || "") === filter;
  }

  function replaceFilterOptions(id, items) {
    const select = $(id);
    const current = select.value;
    select.replaceChildren(...items);
    if ([...select.options].some((item) => item.value === current))
      select.value = current;
  }

  function renderGrouped(host, sourceCount, items, groupFor, buildRow, emptyText) {
    host.classList.remove("notice", "warn");
    host.replaceChildren();
    host.classList.toggle("prep-empty", !items.length);
    if (!items.length) {
      host.textContent = sourceCount ? "沒有符合目前搜尋或篩選條件的內容。" : emptyText;
      return;
    }
    const groups = new Map();
    for (const item of items) {
      const [key, label] = groupFor(item);
      if (!groups.has(key)) groups.set(key, { label, items: [] });
      groups.get(key).items.push(item);
    }
    let index = 0;
    for (const group of groups.values()) {
      const details = document.createElement("details");
      details.className = "prep-group";
      details.open = index === 0;
      const summary = document.createElement("summary");
      summary.textContent = `${group.label}（${group.items.length}）`;
      const body = document.createElement("div");
      body.className = "prep-group-body";
      for (const item of group.items) body.append(buildRow(item));
      details.append(summary, body);
      host.append(details);
      index += 1;
    }
  }

  function canEdit() {
    return ["owner", "editor"].includes(state.bundle?.role);
  }

  function isOwner() {
    return state.bundle?.role === "owner";
  }

  function moveExistingTools() {
    const strategy = $("strategy")?.querySelector(".layout");
    const review = $("review")?.querySelector(".layout");
    if (strategy && !$("prepStrategyHost").contains(strategy))
      $("prepStrategyHost").append(strategy);
    if (review && !$("prepReviewHost").contains(review))
      $("prepReviewHost").append(review);
    $("strategy")?.remove();
    $("review")?.remove();
  }

  function setActiveTab(item, active) {
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
  }

  function scrollPrepContentIntoView(target) {
    if (!mobileLayout.matches || !target) return;
    requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
  }

  function switchPrepPane(name, { scroll = true } = {}) {
    let target = null;
    document.querySelectorAll(".prep-pane").forEach((pane) => {
      const active = pane.id === name;
      pane.classList.toggle("active", active);
      if (active) target = pane.querySelector(".prep-subpane.active") || pane;
    });
    document.querySelectorAll("[data-prep-pane]").forEach((item) =>
      setActiveTab(item, item.dataset.prepPane === name));
    if (scroll) scrollPrepContentIntoView(target);
  }

  function switchPrepSubPane(parentId, name, { scroll = true } = {}) {
    const parent = $(parentId);
    if (!parent) return;
    let target = null;
    parent.querySelectorAll(".prep-subpane").forEach((pane) => {
      const active = pane.id === name;
      pane.classList.toggle("active", active);
      if (active) target = pane;
    });
    parent.querySelectorAll("[data-prep-subpane]").forEach((item) =>
      setActiveTab(item, item.dataset.prepSubpane === name));
    if (scroll) scrollPrepContentIntoView(target);
  }

  function configureMobilePanels() {
    if (!mobileLayout.matches) return;
    ["bandwidthPanel", "modelPanel"].forEach((id) => {
      const panel = $(id);
      if (panel) panel.open = false;
    });
  }

  function renderWorkspaceOptions() {
    const current = String(state.projectId || sessionStorage.competitionPrepProject || "");
    const projectSelect = $("prepProjectSelect");
    projectSelect.replaceChildren(option("", "選擇項目…"));
    for (const project of state.data.projects) {
      const side = project.our_side === "pro" ? "正" : "反";
      projectSelect.append(option(project.id,
        `${project.match_date}｜${project.title}｜${side}方｜${roleLabels[project.role] || project.role}`));
    }
    if ([...projectSelect.options].some((item) => item.value === current))
      projectSelect.value = current;

    $("prepRecentMatch").replaceChildren(option("", "手動輸入"));
    for (const match of state.data.recent_matches) {
      const item = option(match.id,
        `${match.match_date}｜${match.competition_name}｜${match.opponent}`);
      item.dataset.match = JSON.stringify(match);
      $("prepRecentMatch").append(item);
    }
    $("prepFormat").replaceChildren(...state.data.formats.map((value) => option(value, value)));
    renderManuscriptSlots("");
    $("prepMemberUser").replaceChildren(...state.data.accounts.map((user) => option(user, user)));
  }

  function renderManuscriptSlots(debateFormat) {
    const current = $("prepManuscriptSlot").value;
    const slots = (state.data?.slots || []).filter((item) =>
      item.value !== "dep3" || debateFormat === "聯中");
    $("prepManuscriptSlot").replaceChildren(
      ...slots.map((item) => option(item.value, item.label)),
    );
    if (slots.some((item) => item.value === current))
      $("prepManuscriptSlot").value = current;
    replaceFilterOptions("prepStrategyFilterSlot", [
      option("", "全部辯位"), option("__unassigned__", "未分配"),
      ...slots.map((item) => option(item.value, item.label)),
    ]);
  }

  function showProjectSelectionWarnings() {
    ["prepManuscriptList", "prepStrategyList", "prepEvidenceList", "prepWeaknessList"]
      .forEach((id) => {
        const host = $(id);
        host.className = "notice warn";
        host.textContent = "⚠️ 請先選擇項目。";
      });
  }

  async function loadWorkspace({ select = true } = {}) {
    const generation = ++state.loadGeneration;
    try {
      const data = await api("/api/competition-prep/data");
      if (generation !== state.loadGeneration) return;
      state.data = data;
      state.loadedAfterLogin = true;
      renderWorkspaceOptions();
      const selected = Number($("prepProjectSelect").value || 0);
      if (select && selected) await loadProject(selected);
      else if (!selected) clearProject();
    } catch (error) {
      if (error.status !== 401) toast(`⚠️ 未能載入比賽準備：${error.message}`);
    }
  }

  function clearProject() {
    state.loadGeneration += 1;
    state.projectId = 0;
    state.bundle = null;
    sessionStorage.removeItem("competitionPrepProject");
    $("prepProjectId").value = "";
    resetManuscript();
    $("prepProjectMeta").className = "notice warn";
    $("prepProjectMeta").textContent = "請先選擇或建立項目。";
    $("prepExport").disabled = true;
    $("prepDeleteProject").disabled = true;
    $("prepMemberDetails").classList.add("hidden");
    $("prepMemberEmpty").classList.remove("hidden");
    $("prepSaveResearch").classList.add("hidden");
    $("prepSaveFact").classList.add("hidden");
    $("prepResearchTarget").classList.add("hidden");
    $("prepFactTarget").classList.add("hidden");
    $("prepAuditResult").classList.add("caption");
    $("prepAuditResult").textContent = "尚未執行全隊審查。";
    $("prepAttackResult").classList.add("caption");
    $("prepAttackResult").textContent = "尚未執行模擬攻擊。";
    showProjectSelectionWarnings();
    syncEmbeddedForms();
  }

  async function loadProject(projectId) {
    const previousManuscriptId = state.projectId === Number(projectId)
      ? Number($("prepManuscriptId").value || 0) : 0;
    const generation = ++state.loadGeneration;
    const data = await api(`/api/competition-prep/projects/${projectId}`);
    if (generation !== state.loadGeneration) return;
    state.projectId = Number(projectId);
    state.bundle = data;
    sessionStorage.competitionPrepProject = String(projectId);
    $("prepProjectSelect").value = String(projectId);
    $("prepProjectId").value = String(projectId);
    $("prepExport").disabled = false;
    renderProject(previousManuscriptId);
  }

  function renderProject(previousManuscriptId = 0) {
    const project = state.bundle.project;
    renderManuscriptSlots(project.debate_format);
    const expires = new Date(project.expires_at).toLocaleString("zh-HK", {
      timeZone: "Asia/Hong_Kong", dateStyle: "medium", timeStyle: "short",
    });
    $("prepProjectMeta").className = "caption prep-empty";
    $("prepProjectMeta").textContent =
      `${project.title}｜${project.match_date}｜${project.our_side === "pro" ? "正方" : "反方"}｜${project.debate_format}｜對手：${project.opponent || "未填"}｜到期：${expires}｜你的權限：${roleLabels[state.bundle.role]}`;
    $("prepMemberDetails").classList.remove("hidden");
    $("prepMemberEmpty").classList.add("hidden");
    $("prepDeleteProject").disabled = !isOwner();
    $("prepMemberForm").classList.toggle("hidden", !isOwner());
    renderMembers();
    renderManuscripts();
    renderStrategy();
    renderEvidence();
    renderWeaknesses();
    renderLatestAiRuns();
    const selected = state.bundle.manuscripts.find(
      (item) => Number(item.id) === previousManuscriptId,
    );
    if (selected) selectManuscript(selected, "", false);
    else resetManuscript();
  }

  function syncEmbeddedForms() {
    const project = state.bundle?.project;
    const enabled = Boolean(project && canEdit());
    const side = project?.our_side === "con" ? "反方" : "正方";
    if ($("strategyTopic")) {
      $("strategyTopic").value = project?.topic_text || "";
      $("strategySide").value = side;
      $("strategyFormat").value = project?.debate_format || "校園隨想";
      ["strategyTopic", "strategySide", "strategyFormat"].forEach((id) => $(id).disabled = true);
      $("strategyForm").querySelector("button.primary").disabled = !enabled;
    }
    const manuscriptId = Number($("prepManuscriptId")?.value || 0);
    const manuscript = state.bundle?.manuscripts.find((item) => Number(item.id) === manuscriptId);
    if ($("reviewTopic")) {
      $("reviewTopic").value = project?.topic_text || "";
      $("reviewSide").value = side;
      $("reviewFormat").value = project?.debate_format || "校園隨想";
      const thirdDeputy = $("reviewPosition").querySelector('option[value="5"]');
      const allowThirdDeputy = project?.debate_format === "聯中";
      thirdDeputy.hidden = !allowThirdDeputy;
      thirdDeputy.disabled = !allowThirdDeputy;
      if (!allowThirdDeputy && $("reviewPosition").value === "5")
        $("reviewPosition").value = "1";
      const positions = { main: "1", dep1: "2", dep2: "3", closing: "4", dep3: "5" };
      if (manuscript) {
        if (positions[manuscript.slot]) $("reviewPosition").value = positions[manuscript.slot];
        $("reviewText").value = manuscript.body || "";
      } else {
        $("reviewText").value = "";
      }
      ["reviewTopic", "reviewSide", "reviewPosition", "reviewFormat"]
        .forEach((id) => $(id).disabled = true);
      $("reviewSubmit").disabled = !(enabled && manuscript && positions[manuscript.slot]);
    }
    ["prepManuscriptForm", "prepStrategyCardForm", "prepEvidenceForm", "prepWeaknessForm"]
      .forEach((id) => $(id)?.querySelectorAll("input,textarea,select,button").forEach((item) => {
        item.disabled = !enabled;
      }));
    $("prepTeamAudit").disabled = !enabled;
    $("prepAttack").disabled = !enabled;
  }

  function renderMembers() {
    $("prepMemberList").replaceChildren();
    for (const member of state.bundle.members) {
      const item = row(member.user_id, roleLabels[member.role] || member.role);
      if (isOwner() && member.role !== "owner")
        item.actions.append(button("移除", () => removeMember(member.user_id), "danger"));
      $("prepMemberList").append(item.wrapper);
    }
    const projectUsers = new Set(state.bundle.members.map((member) => member.user_id));
    $("prepManuscriptAssignee").replaceChildren(option("", "未分配"));
    for (const user of projectUsers) $("prepManuscriptAssignee").append(option(user, user));
    const assigneeOptions = () => [
      option("", "全部隊員"), option("__unassigned__", "未分配"),
      ...[...projectUsers].map((user) => option(user, user)),
    ];
    replaceFilterOptions("prepManuscriptFilterAssignee", assigneeOptions());
    replaceFilterOptions("prepWeaknessFilterAssignee", assigneeOptions());
  }

  function renderManuscripts() {
    const host = $("prepManuscriptList");
    const search = $("prepManuscriptSearch").value.trim().toLocaleLowerCase("zh-HK");
    const status = $("prepManuscriptFilterStatus").value;
    const assignee = $("prepManuscriptFilterAssignee").value;
    const order = Object.keys(slotLabels);
    const manuscripts = state.bundle.manuscripts.filter((item) =>
      (!search || filterText(item.title, item.body, item.assigned_user_id).includes(search))
      && (!status || item.status === status)
      && assignmentMatches(item.assigned_user_id, assignee)
    ).sort((a, b) => order.indexOf(a.slot) - order.indexOf(b.slot));
    renderGrouped(host, state.bundle.manuscripts.length, manuscripts,
      (item) => [item.slot, slotLabels[item.slot] || item.slot], (manuscript) => {
      const item = row(manuscript.title,
        `${manuscriptStatusLabels[manuscript.status] || manuscript.status}｜負責：${manuscript.assigned_user_id || "未分配"}｜版本 ${manuscript.revision}｜${excerpt(manuscript.body) || "未填內容"}`);
      if (canEdit()) {
        item.actions.append(button("編輯", () => selectManuscript(manuscript, "edit")));
        if (["main", "dep1", "dep2", "dep3", "closing"].includes(manuscript.slot))
          item.actions.append(button("練習", () => selectManuscript(manuscript, "practice"), "primary"));
        item.actions.append(button("刪除", () => deleteItem("manuscripts", manuscript.id), "danger"));
      } else {
        item.actions.append(button("檢視", () => selectManuscript(manuscript, "edit")));
      }
      return item.wrapper;
    }, "尚未建立稿件。");
  }

  function manuscriptContentSnapshot() {
    return JSON.stringify({
      title: $("prepManuscriptTitle").value,
      body: $("prepManuscriptBody").value,
      assigned: $("prepManuscriptAssignee").value,
      status: $("prepManuscriptStatus").value,
    });
  }

  function selectManuscript(manuscript, target = "edit", scroll = true) {
    $("prepManuscriptId").value = manuscript.id;
    $("prepManuscriptRevision").value = manuscript.revision;
    $("prepManuscriptSlot").value = manuscript.slot;
    $("prepManuscriptTitle").value = manuscript.title;
    $("prepManuscriptBody").value = manuscript.body || "";
    $("prepManuscriptAssignee").value = manuscript.assigned_user_id || "";
    $("prepManuscriptStatus").value = manuscript.status;
    state.activeManuscriptSlot = manuscript.slot;
    state.manuscriptFormBaseline = manuscriptContentSnapshot();
    syncEmbeddedForms();
    if (scroll) {
      const subPaneId = target === "practice" ? "prepReviewPane" : "prepManuscriptEditPane";
      switchPrepPane("prepManuscripts", { scroll: false });
      switchPrepSubPane("prepManuscripts", subPaneId);
      if (!mobileLayout.matches)
        $(subPaneId).scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function resetManuscript(slot = $("prepManuscriptSlot").value) {
    $("prepManuscriptId").value = "";
    $("prepManuscriptRevision").value = "";
    $("prepManuscriptTitle").value = "";
    $("prepManuscriptBody").value = "";
    $("prepManuscriptAssignee").value = "";
    $("prepManuscriptStatus").value = "draft";
    if ([...$("prepManuscriptSlot").options].some((item) => item.value === slot))
      $("prepManuscriptSlot").value = slot;
    state.activeManuscriptSlot = $("prepManuscriptSlot").value;
    state.manuscriptFormBaseline = manuscriptContentSnapshot();
    syncEmbeddedForms();
  }

  function renderStrategy() {
    const host = $("prepStrategyList");
    const search = $("prepStrategySearch").value.trim().toLocaleLowerCase("zh-HK");
    const assignedSlot = $("prepStrategyFilterSlot").value;
    const priority = $("prepStrategyFilterPriority").value;
    const order = Object.keys(kindLabels);
    const cards = state.bundle.strategy_cards.filter((item) =>
      (!search || filterText(item.title, item.content).includes(search))
      && assignmentMatches(item.assigned_slot, assignedSlot)
      && (!priority || String(item.priority) === priority)
    ).sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind));
    renderGrouped(host, state.bundle.strategy_cards.length, cards,
      (item) => [item.kind, kindLabels[item.kind] || item.kind], (card) => {
      const item = row(card.title,
        `負責：${slotLabels[card.assigned_slot] || "未分配"}｜優先級：${priorityLabels[card.priority] || card.priority}｜${excerpt(card.content) || "未填內容"}`);
      if (canEdit()) item.actions.append(button("刪除", () => deleteItem("strategy-cards", card.id), "danger"));
      return item.wrapper;
    }, "尚未建立攻防策略卡。");
  }

  function renderEvidence() {
    const host = $("prepEvidenceList");
    const search = $("prepEvidenceSearch").value.trim().toLocaleLowerCase("zh-HK");
    const sourceType = $("prepEvidenceFilterType").value;
    const scope = $("prepEvidenceFilterScope").value;
    const order = Object.keys(evidenceTypeLabels);
    const cards = state.bundle.evidence_cards.filter((item) =>
      (!search || filterText(item.claim_text, item.excerpt, item.source_name, item.limitations).includes(search))
      && (!sourceType || item.source_type === sourceType)
      && (!scope || item.side_scope === scope)
    ).sort((a, b) => order.indexOf(a.source_type) - order.indexOf(b.source_type));
    renderGrouped(host, state.bundle.evidence_cards.length, cards,
      (item) => [item.source_type, evidenceTypeLabels[item.source_type] || item.source_type], (card) => {
      const item = row(card.claim_text,
        `${evidenceScopeLabels[card.side_scope] || card.side_scope}適用｜${card.source_name || "未填來源"}${card.limitations ? `｜限制：${excerpt(card.limitations)}` : ""}`);
      if (card.source_url) item.actions.append(button("檢視資料出處", () =>
        window.open(card.source_url, "_blank", "noopener,noreferrer")));
      if (canEdit()) item.actions.append(button("刪除", () => deleteItem("evidence-cards", card.id), "danger"));
      return item.wrapper;
    }, "尚未加入論據資源。");
    $("prepSaveResearch").classList.toggle("hidden", !canEdit());
    $("prepSaveFact").classList.toggle("hidden", !canEdit());
    const project = state.bundle.project;
    const targetText = `目前項目：「${project.title}」；你的權限：${roleLabels[state.bundle.role]}。只有項目擁有者或編輯者可以加入內容。`;
    $("prepResearchTarget").textContent = targetText;
    $("prepFactTarget").textContent = targetText;
    $("prepResearchTarget").classList.remove("hidden");
    $("prepFactTarget").classList.remove("hidden");
    if (canEdit()) {
      $("prepSaveResearch").textContent = `加入「${project.title}」論據資源庫`;
      $("prepSaveFact").textContent = `加入「${project.title}」論據資源庫`;
    }
  }

  function renderWeaknesses() {
    const host = $("prepWeaknessList");
    const search = $("prepWeaknessSearch").value.trim().toLocaleLowerCase("zh-HK");
    const status = $("prepWeaknessFilterStatus").value;
    const assignee = $("prepWeaknessFilterAssignee").value;
    const order = Object.keys(weaknessCategoryLabels);
    const weaknesses = state.bundle.weaknesses.filter((item) =>
      (!search || filterText(item.title, item.description, item.assigned_user_id).includes(search))
      && (!status || item.status === status)
      && assignmentMatches(item.assigned_user_id, assignee)
    ).sort((a, b) => order.indexOf(a.category) - order.indexOf(b.category));
    renderGrouped(host, state.bundle.weaknesses.length, weaknesses,
      (item) => [item.category, weaknessCategoryLabels[item.category] || item.category], (weakness) => {
      const item = row(weakness.title,
        `${weaknessStatusLabels[weakness.status] || weakness.status}｜負責：${weakness.assigned_user_id || "未分配"}｜優先級：${priorityLabels[weakness.priority] || weakness.priority}｜${excerpt(weakness.description) || "未填描述"}`);
      if (canEdit()) {
        item.actions.append(button("開始嚴格自由辯論", () => startWeakness(weakness.id), "primary"));
        const next = weakness.status === "passed" ? "open" : "passed";
        item.actions.append(button(next === "passed" ? "標記通過" : "重開", () =>
          updateWeakness(weakness, next)));
        item.actions.append(button("刪除", () => deleteItem("weaknesses", weakness.id), "danger"));
      }
      return item.wrapper;
    }, "尚未建立弱點訓練項目。");
  }

  function renderLatestAiRuns() {
    $("prepAuditResult").classList.add("caption");
    $("prepAuditResult").textContent = "尚未執行全隊審查。";
    $("prepAttackResult").classList.add("caption");
    $("prepAttackResult").textContent = "尚未執行模擬攻擊。";
    const audit = state.bundle.ai_runs.find((item) => item.run_type === "team_audit");
    const attack = state.bundle.ai_runs.find((item) => item.run_type === "strategy_attack");
    if (audit) {
      $("prepAuditResult").classList.remove("caption");
      $("prepAuditResult").innerHTML = SafeMarkdown.render(audit.output_markdown);
    }
    if (attack) {
      $("prepAttackResult").classList.remove("caption");
      $("prepAttackResult").innerHTML = SafeMarkdown.render(attack.output_markdown);
    }
  }

  async function mutate(action, success) {
    if (!state.projectId) return toast("⚠️ 請先選擇項目。");
    const projectId = state.projectId;
    busy(true);
    try {
      await action();
      if (state.projectId !== projectId) return;
      await loadProject(projectId);
      toast(`✅ ${success}`);
    } catch (error) {
      toast(`⚠️ ${error.message}`);
    } finally {
      busy(false);
    }
  }

  async function removeMember(userId) {
    if (!confirm(`確定移除協作者 ${userId}？`)) return;
    return mutate(() => api(
      `/api/competition-prep/projects/${state.projectId}/members/${encodeURIComponent(userId)}`,
      { method: "DELETE" },
    ), "已移除協作者。");
  }

  async function deleteItem(collection, id) {
    if (!confirm("確定刪除？此操作不能復原。")) return;
    return mutate(() => api(
      `/api/competition-prep/projects/${state.projectId}/${collection}/${id}`,
      { method: "DELETE" },
    ), "已刪除。");
  }

  async function runPrepAi(runType, outputId) {
    if (!state.projectId) return toast("⚠️ 請先選擇項目。");
    const projectId = state.projectId;
    const generation = state.loadGeneration;
    const operationKey = `${projectId}:${runType}`;
    const operationId = state.pendingAiOperations.get(operationKey)
      || `prep-${crypto.randomUUID()}`;
    state.pendingAiOperations.set(operationKey, operationId);
    busy(true);
    try {
      const data = await api(`/api/competition-prep/projects/${projectId}/ai-run`, {
        method: "POST",
        body: JSON.stringify({
          run_type: runType, model_label: $("globalModel").value,
          operation_id: operationId,
        }),
      });
      state.pendingAiOperations.delete(operationKey);
      if (state.projectId !== projectId || state.loadGeneration !== generation) return;
      $(outputId).classList.remove("caption");
      $(outputId).innerHTML = SafeMarkdown.render(data.markdown);
      await loadProject(projectId);
      if (state.projectId === projectId) toast("✅ AI 分析完成。");
    } catch (error) {
      toast(`⚠️ ${error.message}`);
    } finally {
      busy(false);
    }
  }

  async function startWeakness(weaknessId) {
    busy(true);
    try {
      const data = await api(
        `/api/competition-prep/projects/${state.projectId}/weaknesses/${weaknessId}/prepare-live`,
        { method: "POST", body: "{}" },
      );
      location.href = data.url;
    } catch (error) {
      toast(`⚠️ ${error.message}`);
      busy(false);
    }
  }

  function updateWeakness(weakness, status) {
    return mutate(() => api(
      `/api/competition-prep/projects/${state.projectId}/weaknesses/${weakness.id}`,
      { method: "PATCH", body: JSON.stringify({ status, revision: weakness.revision }) },
    ), "弱點狀態已更新。");
  }

  function prefillFromRecent() {
    const selected = $("prepRecentMatch").selectedOptions[0];
    if (!selected?.dataset.match) return;
    const match = JSON.parse(selected.dataset.match);
    $("prepTitle").value = match.competition_name || "";
    $("prepOpponent").value = match.opponent || "";
    $("prepMatchDate").value = match.match_date || "";
    $("prepMatchTime").value = String(match.match_time || "").slice(0, 5);
    $("prepTopic").value = match.topic_text || "";
    if (["pro", "con"].includes(match.our_side)) $("prepSide").value = match.our_side;
  }

  async function saveExternalResult(kind) {
    if (!state.projectId) return toast("⚠️ 請先選擇比賽準備項目。");
    const research = kind === "research";
    const claim = research ? $("researchNeed").value.trim() : $("factText").value.trim();
    const result = $(research ? "researchResult" : "factResult").textContent.trim();
    if (!claim || !result || result.startsWith("結果會") || result.startsWith("輸入一項"))
      return toast("⚠️ 請先完成一次查詢。");
    await mutate(() => api(`/api/competition-prep/projects/${state.projectId}/evidence-cards`, {
      method: "POST",
      body: JSON.stringify({
        claim_text: claim.slice(0, 500), excerpt: result.slice(0, 20000),
        source_name: research ? "搵料易結果" : "Fact Check易結果",
        source_type: "ai_research", side_scope: "both",
        limitations: "由 AI 搜尋結果匯入；引用前請開啟結果內原始來源再次核對。",
      }),
    }), `已加入「${state.bundle.project.title}」論據資源庫。`);
  }

  function wireEvents() {
    document.querySelectorAll("[data-prep-pane]").forEach((item) => {
      setActiveTab(item, item.classList.contains("active"));
      item.addEventListener("click", () => switchPrepPane(item.dataset.prepPane));
    });
    document.querySelectorAll("[data-prep-subpane]").forEach((item) => {
      setActiveTab(item, item.classList.contains("active"));
      item.addEventListener("click", () => {
        const parent = item.closest(".prep-pane");
        if (parent) switchPrepSubPane(parent.id, item.dataset.prepSubpane);
      });
    });
    [
      [["prepManuscriptSearch", "prepManuscriptFilterStatus", "prepManuscriptFilterAssignee"], renderManuscripts],
      [["prepStrategySearch", "prepStrategyFilterSlot", "prepStrategyFilterPriority"], renderStrategy],
      [["prepEvidenceSearch", "prepEvidenceFilterType", "prepEvidenceFilterScope"], renderEvidence],
      [["prepWeaknessSearch", "prepWeaknessFilterStatus", "prepWeaknessFilterAssignee"], renderWeaknesses],
    ].forEach(([ids, renderer]) => ids.forEach((id) => {
      $(id).addEventListener(id.endsWith("Search") ? "input" : "change", () => {
        if (state.bundle) renderer();
      });
    }));
    $("prepProjectSelect").addEventListener("change", async () => {
      const id = Number($("prepProjectSelect").value || 0);
      if (id) {
        try { await loadProject(id); } catch (error) { toast(`⚠️ ${error.message}`); }
      } else clearProject();
    });
    $("prepReload").addEventListener("click", () => loadWorkspace());
    $("prepExport").addEventListener("click", () => {
      if (state.projectId) location.href = `/api/competition-prep/projects/${state.projectId}/export`;
    });
    $("prepDeleteProject").addEventListener("click", async () => {
      if (!state.projectId || !confirm("確定刪除整個比賽準備項目？所有稿件、策略、資源、弱點及 AI 結果都會一併刪除，不能復原。")) return;
      busy(true);
      try {
        await api(`/api/competition-prep/projects/${state.projectId}`, { method: "DELETE" });
        clearProject();
        await loadWorkspace({ select: false });
        toast("✅ 項目已刪除。");
      } catch (error) { toast(`⚠️ ${error.message}`); }
      finally { busy(false); }
    });
    $("prepRecentMatch").addEventListener("change", prefillFromRecent);
    $("prepCreateForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      busy(true);
      try {
        const created = await api("/api/competition-prep/projects", {
          method: "POST",
          body: JSON.stringify({
            recent_match_id: Number($("prepRecentMatch").value) || null,
            title: $("prepTitle").value.trim(), opponent: $("prepOpponent").value.trim(),
            match_date: $("prepMatchDate").value, match_time: $("prepMatchTime").value,
            topic_text: $("prepTopic").value.trim(), our_side: $("prepSide").value,
            debate_format: $("prepFormat").value,
          }),
        });
        sessionStorage.competitionPrepProject = String(created.project_id);
        await loadWorkspace();
        switchPrepPane("prepProject", { scroll: false });
        switchPrepSubPane("prepProject", "prepProjectOverview");
        toast("✅ 已建立比賽準備項目。");
      } catch (error) { toast(`⚠️ ${error.message}`); }
      finally { busy(false); }
    });
    $("prepMemberForm").addEventListener("submit", (event) => {
      event.preventDefault();
      mutate(() => api(`/api/competition-prep/projects/${state.projectId}/members`, {
        method: "PUT",
        body: JSON.stringify({ user_id: $("prepMemberUser").value, role: $("prepMemberRole").value }),
      }), "協作者權限已更新。");
    });
    $("prepManuscriptForm").addEventListener("submit", (event) => {
      event.preventDefault();
      mutate(() => api(`/api/competition-prep/projects/${state.projectId}/manuscripts`, {
        method: "POST",
        body: JSON.stringify({
          id: Number($("prepManuscriptId").value) || null,
          revision: Number($("prepManuscriptRevision").value) || 0,
          slot: $("prepManuscriptSlot").value,
          title: $("prepManuscriptTitle").value.trim(),
          body: $("prepManuscriptBody").value,
          assigned_user_id: $("prepManuscriptAssignee").value,
          status: $("prepManuscriptStatus").value,
        }),
      }), "稿件已儲存。");
    });
    $("prepManuscriptReset").addEventListener("click", () => resetManuscript());
    $("prepManuscriptSlot").addEventListener("change", () => {
      const requestedSlot = $("prepManuscriptSlot").value;
      if (
        state.manuscriptFormBaseline
        && manuscriptContentSnapshot() !== state.manuscriptFormBaseline
        && !window.confirm("目前稿件有尚未儲存的修改。確定切換辯位並放棄這些修改嗎？")
      ) {
        $("prepManuscriptSlot").value = state.activeManuscriptSlot;
        return;
      }
      const existing = state.bundle?.manuscripts.find(
        (item) => item.slot === requestedSlot,
      );
      if (existing) selectManuscript(existing, "edit", false);
      else resetManuscript(requestedSlot);
    });
    $("prepStrategyCardForm").addEventListener("submit", (event) => {
      event.preventDefault();
      mutate(() => api(`/api/competition-prep/projects/${state.projectId}/strategy-cards`, {
        method: "POST",
        body: JSON.stringify({ kind: $("prepStrategyKind").value,
          title: $("prepStrategyTitle").value.trim(), content: $("prepStrategyContent").value }),
      }), "策略卡已加入。");
    });
    $("prepEvidenceForm").addEventListener("submit", (event) => {
      event.preventDefault();
      mutate(() => api(`/api/competition-prep/projects/${state.projectId}/evidence-cards`, {
        method: "POST",
        body: JSON.stringify({ claim_text: $("prepEvidenceClaim").value.trim(),
          excerpt: $("prepEvidenceExcerpt").value, source_url: $("prepEvidenceUrl").value.trim(),
          source_name: $("prepEvidenceSource").value.trim(), source_type: $("prepEvidenceType").value,
          side_scope: $("prepEvidenceScope").value, limitations: $("prepEvidenceLimits").value }),
      }), "論據資源已加入。");
    });
    $("prepWeaknessForm").addEventListener("submit", (event) => {
      event.preventDefault();
      mutate(() => api(`/api/competition-prep/projects/${state.projectId}/weaknesses`, {
        method: "POST",
        body: JSON.stringify({ title: $("prepWeaknessTitle").value.trim(),
          description: $("prepWeaknessDescription").value,
          category: $("prepWeaknessCategory").value }),
      }), "弱點已加入。");
    });
    $("prepTeamAudit").addEventListener("click", () => runPrepAi("team_audit", "prepAuditResult"));
    $("prepAttack").addEventListener("click", () => runPrepAi("strategy_attack", "prepAttackResult"));
    $("prepOpenResearch").addEventListener("click", () => {
      $("researchTopic").value = state.bundle?.project.topic_text || "";
      $("researchNeed").value = "請為我方主線尋找可引用數據、案例、反例及可靠來源。";
      document.querySelector('[data-pane="research"]').click();
    });
    $("prepOpenFact").addEventListener("click", () => {
      $("factText").value = $("prepEvidenceClaim").value.trim();
      document.querySelector('[data-pane="fact"]').click();
    });
    $("prepSaveResearch").addEventListener("click", () => saveExternalResult("research"));
    $("prepSaveFact").addEventListener("click", () => saveExternalResult("fact"));
    $("reviewMode")?.addEventListener("change", () => queueMicrotask(syncEmbeddedForms));
  }

  configureMobilePanels();
  moveExistingTools();
  wireEvents();
  syncEmbeddedForms();

  const observer = new MutationObserver(() => {
    if (!$("app").classList.contains("hidden") && !state.loadedAfterLogin)
      loadWorkspace();
  });
  observer.observe($("app"), { attributes: true, attributeFilter: ["class"] });
  loadWorkspace();
})();
