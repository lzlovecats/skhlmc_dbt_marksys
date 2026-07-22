(() => {
  if (location.pathname !== "/ai-coach/local-practice") return;

  const $ = (id) => document.getElementById(id);
  const sessionId = new URLSearchParams(location.search).get("session") || "";
  let session = null;
  let capabilities = null;
  let turnStartedAt = 0;
  let requestActive = false;
  let localTtsDisabled = false;
  let localTtsWarningShown = false;
  let activeAudio = null;
  const messageMaximum = $("turnText").maxLength;
  function playedKeys() {
    try {
      const value = JSON.parse(
        sessionStorage.getItem(`local-ai-practice-played:${sessionId}`) || "[]",
      );
      return Array.isArray(value) ? value : [];
    } catch (_error) {
      return [];
    }
  }
  const played = new Set(playedKeys());

  async function api(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      ...options,
    });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) {
      location.href = "/ai-coach";
      throw new Error("未登入");
    }
    if (!response.ok) {
      const error = new Error(payload.detail || "操作失敗");
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function showNotice(message, warning = true) {
    $("notice").textContent = message || "";
    $("notice").classList.toggle("show", Boolean(message));
    $("notice").classList.toggle("warn", Boolean(message) && warning);
  }

  function showSpeechNotice(message) {
    $("speechNotice").textContent = message || "";
    $("speechNotice").classList.toggle("show", Boolean(message));
    $("speechNotice").classList.toggle("warn", Boolean(message));
  }

  function formatSeconds(value) {
    const seconds = Math.max(0, Math.round(Number(value) || 0));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function usedFor(side) {
    let used = Number(session?.used_seconds?.[side] || 0);
    if (session?.state === "user_speaking" && session.user_side === side && turnStartedAt) {
      used += Math.max(0, (Date.now() - turnStartedAt) / 1000);
    }
    return Math.min(Number(session?.seconds_per_side || 0), used);
  }

  function renderTimers() {
    const limit = Number(session?.seconds_per_side || 0);
    $("proTime").textContent = `${formatSeconds(usedFor("正方"))} / ${formatSeconds(limit)}`;
    $("conTime").textContent = `${formatSeconds(usedFor("反方"))} / ${formatSeconds(limit)}`;
  }

  function stateLabel() {
    if (!session) return "正在讀取回合…";
    return {
      user_ready: `輪到你（${session.user_side}）準備發言`,
      user_speaking: `輪到你（${session.user_side}）發言`,
      generating_ai: `自家AI（${session.ai_side}）正在思考`,
      generating_feedback: "自家AI正在整理完場評語",
      ended: "練習已完成",
      failed: "練習已中止",
    }[session.state] || "回合狀態已更新";
  }

  function transcriptKey(item, index) {
    return `${item.turn}:${item.speaker}:${index}`;
  }

  function renderTranscript() {
    const host = $("transcript");
    host.replaceChildren();
    if (!session?.transcript?.length) {
      const empty = document.createElement("p");
      empty.className = "caption";
      empty.textContent = session?.user_side === "正方"
        ? "你係正方，請開始第一輪發言。"
        : "自家AI會以正方身份先開局。";
      host.append(empty);
      return;
    }
    session.transcript.forEach((item, index) => {
      const card = document.createElement("article");
      card.className = `turn ${item.speaker === "user" ? "user" : "ai"}`;
      const head = document.createElement("div");
      head.className = "turn-head";
      const who = document.createElement("strong");
      who.textContent = `${item.side}・${item.speaker === "user" ? "你" : "自家AI"}`;
      const timing = document.createElement("span");
      timing.textContent = `約 ${formatSeconds(item.seconds)}`;
      head.append(who, timing);
      const text = document.createElement("div");
      text.textContent = item.text;
      card.append(head, text);
      if (item.speaker === "ai") {
        const play = document.createElement("button");
        play.type = "button";
        play.textContent = "播放讀音";
        play.addEventListener("click", () => speak(item, index, true));
        card.append(play);
      }
      host.append(card);
    });
  }

  function render() {
    if (!session) return;
    $("topic").textContent = session.topic;
    $("meta").textContent = `${session.debate_format}｜你係${session.user_side}｜自家AI係${session.ai_side}｜每方 ${formatSeconds(session.seconds_per_side)}`;
    $("turnStatus").textContent = stateLabel();
    $("stage").textContent = session.state === "user_speaking"
      ? "server 正在計算今輪用時"
      : session.state.startsWith("generating")
        ? "請等候目前工作完成"
        : "";
    renderTimers();
    renderTranscript();

    const ready = session.state === "user_ready" && !requestActive;
    const speaking = session.state === "user_speaking" && !requestActive;
    const terminal = ["ended", "failed"].includes(session.state);
    $("startTurn").disabled = !ready;
    $("turnText").disabled = !speaking;
    $("sendTurn").disabled = !speaking || !$("turnText").value.trim();
    $("recordTurn").disabled = !speaking || !capabilities?.asr;
    $("recordTurn").title = capabilities?.asr
      ? "開始錄音"
      : "自家語音辨識暫時未能使用";
    $("stopPractice").disabled = requestActive || terminal || session.state.startsWith("generating");
    $("composer").classList.toggle("hidden", terminal);
    $("feedbackCard").classList.toggle("hidden", !session.feedback);
    $("feedback").textContent = session.feedback || "";
    if (terminal && sessionStorage.localAiPracticeSession === sessionId) {
      sessionStorage.removeItem("localAiPracticeSession");
    }
    if (session.error) showNotice(session.error);
    if (!capabilities?.asr && !terminal) {
      $("inputHelp").textContent = "自家語音辨識暫時未能使用，請先輸入文字。開始回合後 server 會計時。";
    }
  }

  async function playAudioSource(source, revoke = false) {
    if (activeAudio) {
      activeAudio.pause();
      activeAudio = null;
    }
    const audio = new Audio(source);
    activeAudio = audio;
    try {
      await audio.play();
      await new Promise((resolve, reject) => {
        audio.onended = resolve;
        audio.onerror = () => reject(new Error("audio playback failed"));
      });
      return true;
    } finally {
      if (revoke) URL.revokeObjectURL(source);
      if (activeAudio === audio) activeAudio = null;
    }
  }

  async function tryLocalTts(item) {
    const response = await fetch("/api/ai-coach/local-practice/tts/local", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, turn_index: item.turn }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "local TTS unavailable");
    const url = new URL(String(payload.audio_url || ""), location.origin);
    if (url.protocol !== "https:") throw new Error("invalid local TTS URL");
    return playAudioSource(url.href);
  }

  async function tryAzureTts(item) {
    const response = await fetch("/api/tts/azure", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: item.text,
        operation_id: `local-practice-tts:${sessionId}:${item.turn}`,
      }),
    });
    if (!response.ok) throw new Error("azure TTS unavailable");
    const blob = await response.blob();
    if (!blob.size) throw new Error("empty Azure TTS audio");
    return playAudioSource(URL.createObjectURL(blob), true);
  }

  async function speak(item, index, force = false) {
    const key = transcriptKey(item, index);
    if (!force && played.has(key)) return;
    played.add(key);
    sessionStorage.setItem(
      `local-ai-practice-played:${sessionId}`,
      JSON.stringify(Array.from(played).slice(-100)),
    );
    if (capabilities?.local_tts && !localTtsDisabled) {
      try {
        await tryLocalTts(item);
        showSpeechNotice("");
        return;
      } catch (_error) {
        localTtsDisabled = true;
      }
    }
    if (!localTtsWarningShown) {
      localTtsWarningShown = true;
      showSpeechNotice("自家讀音模型暫時未能使用。");
    }
    if (capabilities?.azure_tts) {
      try {
        await tryAzureTts(item);
        return;
      } catch (_error) {
        // Text is already rendered; do not hide or regenerate it.
      }
    }
    showSpeechNotice("未能播放讀音，請閱讀畫面文字。");
  }

  async function speakNewestAi(previousLength) {
    const items = session?.transcript || [];
    for (let index = previousLength; index < items.length; index += 1) {
      if (items[index].speaker === "ai") await speak(items[index], index);
    }
  }

  async function load() {
    if (!/^[0-9a-f]{32,64}$/.test(sessionId)) {
      showNotice("練習連結無效，請返回 AI 辯論易重新開始。");
      $("composer").classList.add("hidden");
      $("stopPractice").disabled = true;
      return;
    }
    try {
      const payload = await api(`/api/ai-coach/local-practice/session/${encodeURIComponent(sessionId)}`);
      session = payload.session;
      capabilities = payload.capabilities;
      if (session.state === "user_speaking") {
        turnStartedAt = Date.now() - Number(session.turn_elapsed_seconds || 0) * 1000;
      }
      render();
      await speakNewestAi(0);
    } catch (error) {
      showNotice(error.message || "未能載入練習。請返回重新開始。");
      $("composer").classList.add("hidden");
      $("stopPractice").disabled = true;
    }
  }

  async function startTurn() {
    requestActive = true;
    render();
    try {
      const payload = await api("/api/ai-coach/local-practice/turn/start", {
        method: "POST",
        body: JSON.stringify({
          session_id: sessionId,
          expected_turn: session.turn_index,
        }),
      });
      session = payload.session;
      turnStartedAt = Date.now();
      showNotice("");
      $("turnText").focus();
    } catch (error) {
      showNotice(error.message);
    } finally {
      requestActive = false;
      render();
    }
  }

  async function sendTurn() {
    const text = $("turnText").value.trim();
    if (!text) return;
    const previousLength = session.transcript.length;
    requestActive = true;
    $("stage").textContent = "自家AI正在回應…";
    render();
    try {
      const payload = await api("/api/ai-coach/local-practice/turn", {
        method: "POST",
        body: JSON.stringify({
          session_id: sessionId,
          expected_turn: session.turn_index,
          text,
        }),
      });
      session = payload.session;
      turnStartedAt = 0;
      $("turnText").value = "";
      $("charCount").textContent = `0 / ${messageMaximum}`;
      showNotice("");
      render();
      await speakNewestAi(previousLength);
    } catch (error) {
      showNotice(error.message);
      if (error.message !== "未登入") await load();
    } finally {
      requestActive = false;
      render();
    }
  }

  async function stopPractice() {
    if (!confirm("停止練習並由自家AI整理文字評語？")) return;
    requestActive = true;
    render();
    try {
      const payload = await api("/api/ai-coach/local-practice/stop", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId }),
      });
      session = payload.session;
      turnStartedAt = 0;
      showNotice("");
    } catch (error) {
      showNotice(error.message);
      if (error.message !== "未登入") await load();
    } finally {
      requestActive = false;
      render();
    }
  }

  $("turnText").addEventListener("input", () => {
    $("charCount").textContent = `${$("turnText").value.length} / ${messageMaximum}`;
    render();
  });
  $("startTurn").addEventListener("click", startTurn);
  $("sendTurn").addEventListener("click", sendTurn);
  $("stopPractice").addEventListener("click", stopPractice);
  $("recordTurn").addEventListener("click", () => {
    if (!capabilities?.asr) showNotice("自家語音辨識暫時未能使用，請先輸入文字。");
  });
  $("downloadFeedback").addEventListener("click", () => {
    if (!session?.feedback) return;
    const url = URL.createObjectURL(new Blob([session.feedback], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "自家AI練習評語.txt";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  setInterval(renderTimers, 250);
  load();
})();
