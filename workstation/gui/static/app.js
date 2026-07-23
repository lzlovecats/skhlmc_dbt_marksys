(() => {
  "use strict";
  const csrf = document.querySelector('meta[name="lmc-csrf"]').content;
  const $ = (id) => document.getElementById(id);
  let current = null;
  let inspectedArtifactCatalog = null;

  async function request(url, options = {}) {
    const response = await fetch(url, { cache: "no-store", ...options });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "操作失敗");
    return payload;
  }

  async function action(body) {
    return request("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-LMC-CSRF": csrf },
      body: JSON.stringify(body),
    });
  }

  function show(message, bad = false) {
    $("notice").textContent = message || "";
    $("notice").className = bad ? "bad" : "ok";
  }

  function render(payload) {
    current = payload;
    const manager = payload.manager || {};
    const wss = payload.health?.checks?.wss || {};
    $("summary").replaceChildren();
    const lines = [
      `網站配對：${wss.ok ? "已連線" : "未連線／等待心跳"}`,
      `模式：${manager.mode || "unknown"}`,
      `Drain：${manager.draining ? "是" : "否"}`,
      `Sleep inhibitor：${manager.sleep_inhibited ? "持有" : "沒有"}`,
      `Voice session：${manager.voice_session_active ? "進行中" : manager.voice_session_pending ? "等候文字工作完成" : "沒有"}`,
      `Active job：${manager.active_operation?.stage || "沒有"}`,
      "Manager queue：0（v1 busy 直接回覆，不設長 queue）",
    ];
    lines.forEach((value) => { const p = document.createElement("p"); p.textContent = value; $("summary").append(p); });
    $("health").replaceChildren();
    Object.entries(payload.health?.checks || {}).forEach(([name, check]) => {
      const p = document.createElement("p");
      p.className = check.ok ? "ok" : "bad";
      p.textContent = `${name}：${check.ok ? "OK" : check.code || "未通過"}`;
      $("health").append(p);
    });
    $("inventory").textContent = JSON.stringify({
      required_models: payload.models?.required || [],
      installed_models: payload.models?.installed || [],
      missing_models: payload.models?.missing || [],
      ...(payload.inventory || {}),
    }, null, 2);
    const release = payload.release || {};
    $("releaseStatus").replaceChildren();
    [
      `目前版本：${release.current || "unknown"}`,
      `上一版本：${release.previous || "沒有"}`,
      `最後狀態：${release.pending_health ? "等待 health gate" : release.last_action || "unknown"}`,
      `自動更新：${release.automatic ? "已啟用" : "未啟用"}`,
    ].forEach((value) => {
      const p = document.createElement("p");
      p.textContent = value;
      $("releaseStatus").append(p);
    });
    $("updateChannel").value = release.channel || "stable";
    $("rollbackPrevious").disabled = !release.previous;
    $("nodeName").value = payload.config?.node?.name || "";
    $("serverUrl").value = String(payload.config?.node?.server_url || "").replace(/^wss:/, "https:").replace(/\/api\/lmc-ai\/nodes\/connect$/, "");
    $("powerEnabled").checked = Boolean(payload.config?.power?.enabled);
    $("suspendAt").value = payload.config?.power?.suspend_at || "00:00";
    $("wakeAt").value = payload.config?.power?.wake_at || "08:00";
    const power = payload.power_status || {};
    $("powerStatus").textContent = `下一動作：${power.action || "none"}（${power.reason || "unknown"}）${power.next_check_epoch ? `；下次檢查 ${new Date(power.next_check_epoch * 1000).toLocaleString("zh-HK")}` : ""}`;
    $("powerOverrideUntil").value = power.override_until_epoch
      ? new Date(power.override_until_epoch * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16)
      : "";
    $("details").textContent = JSON.stringify({ manager, health: payload.health }, null, 2);
  }

  async function load() {
    try { render(await request("/api/status")); show(""); }
    catch (error) { show(error.message, true); }
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action],button[data-service]");
    if (!button || button.closest("form")) return;
    button.disabled = true;
    try {
      if (button.dataset.service) await action({ action: "restart_service", service: button.dataset.service });
      else await action({ action: button.dataset.action });
      show("操作已完成。");
      await load();
    } catch (error) { show(error.message, true); }
    finally { button.disabled = false; }
  });
  $("refresh").addEventListener("click", load);
  $("pairForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await action({ action: "pair_node", name: $("nodeName").value, server_url: $("serverUrl").value, token: $("nodeToken").value });
      $("nodeToken").value = "";
      show("配對資料已安全保存，網站亦已接受新 Node 連線。");
    } catch (error) { show(error.message, true); }
  });
  $("powerForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await action({ action: "set_power_schedule", enabled: $("powerEnabled").checked, suspend_at: $("suspendAt").value, wake_at: $("wakeAt").value });
      show("排程已保存；下一分鐘電源檢查會使用新設定。");
    } catch (error) { show(error.message, true); }
  });
  $("powerOverrideForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = $("powerOverrideUntil").value;
    const untilEpoch = value ? Math.floor(new Date(value).getTime() / 1000) : 0;
    if (!untilEpoch) { show("請選擇臨時保持喚醒截止時間。", true); return; }
    try {
      await action({ action: "set_power_override", until_epoch: untilEpoch });
      show("臨時保持喚醒時間已設定。");
      await load();
    } catch (error) { show(error.message, true); }
  });
  $("clearPowerOverride").addEventListener("click", async () => {
    try {
      await action({ action: "set_power_override", until_epoch: 0 });
      show("臨時保持喚醒已取消。");
      await load();
    } catch (error) { show(error.message, true); }
  });
  $("datasetForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const response = await action({
        action: "dataset_prepare",
        dataset_id: $("datasetId").value,
        speaker: $("datasetSpeaker").value,
      });
      show(`資料準備已開始：${response.result?.operation_id || ""}`);
      await load();
    } catch (error) { show(error.message, true); }
  });
  $("trainingForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const response = await action({
        action: "training_start",
        dataset_id: $("trainingDatasetId").value,
      });
      show(`受控訓練已開始：${response.result?.operation_id || ""}`);
      await load();
    } catch (error) { show(error.message, true); }
  });
  $("cancelActive").addEventListener("click", async () => {
    const operationId = current?.manager?.active_operation?.operation_id || "";
    if (!operationId) { show("目前冇可停止嘅長工作。", true); return; }
    try {
      await action({ action: "cancel_operation", operation_id: operationId });
      show("已要求安全停止目前工作。");
    } catch (error) { show(error.message, true); }
  });
  $("checkUpdate").addEventListener("click", async () => {
    try {
      await action({ action: "update_check" });
      show("已開始背景安全更新檢查；可稍後重新整理查看結果。");
    } catch (error) { show(error.message, true); }
  });
  $("updateChannelForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await action({ action: "set_update_channel", channel: $("updateChannel").value });
      show("更新頻道已保存。下一次檢查會使用新頻道。");
    } catch (error) { show(error.message, true); }
  });
  $("rollbackPrevious").addEventListener("click", async () => {
    try {
      await action({ action: "rollback_previous" });
      show("已開始安全 rollback：系統會先 Drain、等工作完成，再切回上一版及執行完整 health gate。");
    } catch (error) { show(error.message, true); }
  });
  $("inspectArtifacts").addEventListener("click", async () => {
    try {
      const response = await action({ action: "artifact_inspect" });
      inspectedArtifactCatalog = response.result?.components || null;
      $("artifactCatalog").textContent = JSON.stringify(inspectedArtifactCatalog || {}, null, 2);
      show("已核對簽章。請細閱模型及 RAG bundle 的 ID、bytes、digest 與 SHA-256，確認後才安裝。");
    } catch (error) { show(error.message, true); }
  });
  for (const [id, requestedAction, label] of [
    ["approveModels", "model_approve", "模型核對／批准"],
    ["installRag", "rag_install", "RAG 安裝"],
    ["rollbackRag", "rag_rollback", "RAG rollback"],
  ]) {
    $(id).addEventListener("click", async () => {
      try {
        if (requestedAction === "model_approve") {
          const modelBundle = inspectedArtifactCatalog?.model_bundle;
          if (!modelBundle?.models?.length) {
            show("請先按『查看已簽署模型大小／雜湊』。", true);
            return;
          }
          const gib = (Number(modelBundle.model_bytes || 0) / (1024 ** 3)).toFixed(2);
          if (!confirm(`確認只安裝上述 ${modelBundle.models.length} 個已簽署模型（合計約 ${gib} GiB）？`)) return;
        } else if (requestedAction === "rag_install") {
          const ragBundle = inspectedArtifactCatalog?.rag_bundle;
          if (!ragBundle?.id || !ragBundle?.sha256 || !ragBundle?.bytes) {
            show("請先按『查看已簽署模型大小／雜湊』並核對 RAG bundle。", true);
            return;
          }
          if (!confirm(
            `確認安裝 RAG bundle ID ${ragBundle.id}（${ragBundle.bytes} bytes，SHA-256 ${ragBundle.sha256}）？`
          )) return;
        }
        const response = await action({ action: requestedAction });
        show(`${label}已開始：${response.result?.operation_id || ""}`);
        await load();
      } catch (error) { show(error.message, true); }
    });
  }
  load();
})();
