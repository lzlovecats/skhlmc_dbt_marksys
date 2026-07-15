/* Mode A: STUN-only two-member WebRTC media; Render carries control text only. */
(() => {
  "use strict";
  const boot = window.ROOM_P2P_BOOTSTRAP;
  if (!boot) return;
  const $ = (id) => document.getElementById(id);
  const code = boot.code;
  const send = (value) => {
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(value));
  };
  const log = (text) => {
    const line = `[${new Date().toLocaleTimeString("zh-HK")}] ${text}`;
    $("log").textContent = (line + "\n" + $("log").textContent).slice(0, 5000);
  };

  let ws;
  let myUid = "";
  let isHost = false;
  let myRole = "";
  let phase = "lobby";
  let roster = [];
  let rosterGeneration = 0;
  let state = {};
  let pc;
  let localStream;
  let remoteStream;
  let remoteAudio;
  let dataChannel;
  let dataPingOk = false;
  let localTest = null;
  let activeCheckId = "";
  let restartTimer = 0;
  let restartAttempted = false;
  let localRtcReady = false;
  let readyPeers = new Set();
  let offerInFlight = false;
  let transcriptItems = [];
  let recognition;
  let recognitionStopping = false;
  let transcriptSequence = 0;
  let pendingTranscriptChunks = [];
  let activeTurnId = "";
  let turnStarting = false;
  let serverClockOffset = 0;
  let lastBellSegment = -1;
  let firedBells = new Set();

  function ring(count = 1) {
    if (!boot.bellSrc) return;
    for (let index = 0; index < Math.max(1, Number(count)); index += 1) {
      setTimeout(() => {
        const audio = new Audio(boot.bellSrc);
        audio.play().catch(() => {});
      }, index * 650);
    }
  }

  function wsUrl() {
    if (boot.wsBase) return `${boot.wsBase.replace(/\/$/, "")}/room/${code}`;
    return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/room/${code}`;
  }

  function ensureRemoteAudio() {
    if (remoteAudio) return remoteAudio;
    remoteAudio = document.createElement("audio");
    remoteAudio.autoplay = true;
    remoteAudio.playsInline = true;
    remoteAudio.setAttribute("aria-label", "對方即時聲音");
    document.body.appendChild(remoteAudio);
    return remoteAudio;
  }

  async function ensureLocalStream() {
    if (localStream?.active) return localStream;
    localStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      video: false,
    });
    return localStream;
  }

  function setupDataChannel(channel) {
    dataChannel = channel;
    dataChannel.onopen = () => {
      dataChannel.send("ping:" + Date.now());
    };
    dataChannel.onmessage = (event) => {
      const value = String(event.data || "");
      if (value.startsWith("ping:")) dataChannel.send("pong:" + value.slice(5));
      if (value.startsWith("pong:")) dataPingOk = true;
    };
  }

  async function ensurePeer() {
    if (pc && pc.signalingState !== "closed") return pc;
    const stream = await ensureLocalStream();
    pc = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.cloudflare.com:3478" }],
      iceTransportPolicy: "all",
    });
    stream.getAudioTracks().forEach((track) => pc.addTrack(track, stream));
    pc.ontrack = (event) => {
      remoteStream = event.streams[0] || new MediaStream([event.track]);
      const audio = ensureRemoteAudio();
      audio.srcObject = remoteStream;
      audio.play().catch(() => {});
    };
    pc.onicecandidate = (event) => {
      if (event.candidate) send({
        type: "rtc_ice", candidate: event.candidate.toJSON(), roster_generation: rosterGeneration,
      });
    };
    pc.ondatachannel = (event) => setupDataChannel(event.channel);
    pc.onconnectionstatechange = handleConnectionState;
    if (isHost) setupDataChannel(pc.createDataChannel("preflight", { ordered: true }));
    return pc;
  }

  async function makeOffer(options = {}) {
    if (
      !isHost || !localRtcReady || readyPeers.size < 1 || offerInFlight
      || roster.filter((item) => item.connected).length !== 2
    ) return;
    offerInFlight = true;
    try {
      await ensurePeer();
      if (pc.signalingState !== "stable") return;
      const description = await pc.createOffer(options);
      await pc.setLocalDescription(description);
      send({
        type: "rtc_offer", description: pc.localDescription.toJSON(),
        roster_generation: rosterGeneration,
      });
    } finally {
      offerInFlight = false;
    }
  }

  async function onSignal(message) {
    if (Number(message.roster_generation) !== rosterGeneration) return;
    await ensurePeer();
    if (message.type === "rtc_offer") {
      await pc.setRemoteDescription(message.description);
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      send({
        type: "rtc_answer", description: pc.localDescription.toJSON(),
        roster_generation: rosterGeneration,
      });
    } else if (message.type === "rtc_answer") {
      await pc.setRemoteDescription(message.description);
    } else if (message.type === "rtc_ice") {
      await pc.addIceCandidate(message.candidate).catch(() => {});
    }
  }

  function handleConnectionState() {
    const status = pc?.connectionState || "new";
    $("connStatus").textContent = `P2P：${status}`;
    if (status === "connected") {
      clearTimeout(restartTimer);
      restartTimer = 0;
      restartAttempted = false;
      send({ type: "rtc_status", status: "connected" });
      return;
    }
    if (phase !== "active" || !["disconnected", "failed"].includes(status)) return;
    send({ type: "rtc_status", status: "disconnected" });
    if (!restartAttempted) {
      restartAttempted = true;
      if (isHost) makeOffer({ iceRestart: true }).catch(() => {});
      restartTimer = setTimeout(() => {
        if (pc?.connectionState !== "connected") {
          send({ type: "rtc_status", status: "failed" });
          log("P2P ICE restart 10 秒內未能恢復，房間安全結束。");
        }
      }, 10000);
    }
  }

  async function waitFor(condition, timeoutMs, step = 100) {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      if (await condition()) return true;
      await new Promise((resolve) => setTimeout(resolve, step));
    }
    return false;
  }

  async function remoteMediaEvidence() {
    if (!pc || !remoteStream) return false;
    let packets = false;
    const stats = await pc.getStats();
    stats.forEach((report) => {
      if (report.type === "inbound-rtp" && report.kind === "audio") {
        packets ||= Number(report.packetsReceived || 0) > 0 && Number(report.bytesReceived || 0) > 0;
      }
    });
    if (!packets) return false;
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return false;
    const context = new AudioCtx();
    await context.resume();
    const analyser = context.createAnalyser();
    analyser.fftSize = 512;
    context.createMediaStreamSource(remoteStream).connect(analyser);
    const values = new Uint8Array(analyser.fftSize);
    let energy = 0;
    for (let index = 0; index < 20; index += 1) {
      analyser.getByteTimeDomainData(values);
      energy = Math.max(energy, values.reduce((sum, item) => sum + Math.abs(item - 128), 0));
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    await context.close();
    return energy > 20;
  }

  function cantoneseTranscriptTest() {
    return new Promise((resolve) => {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) return resolve({ ok: false, text: "", reason: "browser 不支援 SpeechRecognition" });
      const test = new SpeechRecognition();
      let text = "";
      test.lang = "zh-HK";
      test.continuous = true;
      test.interimResults = false;
      test.onresult = (event) => {
        for (let index = event.resultIndex; index < event.results.length; index += 1) {
          if (event.results[index].isFinal) text += event.results[index][0].transcript || "";
        }
      };
      test.onerror = () => {};
      test.onend = () => {
        const cjk = (text.match(/[\u3400-\u9fff]/g) || []).length;
        const confirmed = cjk >= 2 && confirm(`粵語測試逐字稿：\n「${text.trim()}」\n\n內容正確？`);
        resolve({ ok: confirmed, text: text.trim(), reason: confirmed ? "" : "逐字稿不足兩個中文字或本人未確認" });
      };
      try {
        test.start();
        setTimeout(() => { try { test.stop(); } catch (_) {} }, 8000);
      } catch (error) {
        resolve({ ok: false, text: "", reason: error.message });
      }
    });
  }

  async function runDeviceTest() {
    $("deviceTestBtn").disabled = true;
    $("networkTestResult").textContent = "請兩部裝置同時對咪講粵語約 8 秒；正在測試 STUN-only P2P…";
    try {
      dataPingOk = false;
      await ensureLocalStream();
      await ensurePeer();
      localRtcReady = true;
      send({
        type: "rtc_status", status: "preflight_ready",
        roster_generation: rosterGeneration,
      });
      if (isHost) await makeOffer();
      const connected = await waitFor(() => pc?.connectionState === "connected", 12000);
      if (!connected) throw new Error("P2P ICE 未能連接；請轉 Wi-Fi／流動數據再試（系統沒有 TURN fallback）");
      ensureRemoteAudio();
      await remoteAudio.play();
      if (dataChannel?.readyState === "open") dataChannel.send("ping:" + Date.now());
      const ping = await waitFor(() => dataPingOk, 3000);
      const media = ping && await remoteMediaEvidence();
      if (!media) throw new Error("未收到對方 data-channel ping 及 remote audio packets／energy");
      const transcript = await cantoneseTranscriptTest();
      localTest = {
        media_ok: true, transcript_ok: transcript.ok,
        message: transcript.ok
          ? "P2P、remote audio、粵語逐字稿均通過"
          : `P2P media 通過；逐字稿失敗（${transcript.reason}），AI 評判會停用`,
        testedAt: Date.now(),
      };
      $("networkTestResult").textContent = localTest.message;
      if (activeCheckId) submitPrecheck(activeCheckId);
    } catch (error) {
      localTest = { media_ok: false, transcript_ok: false, message: error.message, testedAt: Date.now() };
      $("networkTestResult").textContent = `未通過：${error.message}`;
      if (activeCheckId) submitPrecheck(activeCheckId);
    } finally {
      $("deviceTestBtn").disabled = false;
    }
  }

  function submitPrecheck(checkId) {
    if (!localTest || Date.now() - localTest.testedAt > 5 * 60 * 1000) {
      $("networkTestResult").textContent = "主持已要求開始；請先按本機測試按鈕。";
      return;
    }
    send({ type: "precheck_result", check_id: checkId, ...localTest });
  }

  function renderRoster(message) {
    const previousGeneration = rosterGeneration;
    roster = message.roster || [];
    rosterGeneration = Number(message.roster_generation ?? rosterGeneration);
    if (
      previousGeneration && previousGeneration !== rosterGeneration
      && phase === "lobby"
    ) {
      localRtcReady = false;
      readyPeers = new Set();
      localTest = null;
      dataPingOk = false;
      try { pc?.close(); } catch (_) {}
      pc = null;
      dataChannel = null;
      remoteStream = null;
      if (remoteAudio) remoteAudio.srcObject = null;
      $("networkTestResult").textContent = "成員／辯方有變，請兩部裝置重新按本機測試。";
    }
    myUid = message.you || myUid;
    isHost = message.is_host ?? isHost;
    myRole = roster.find((item) => item.user_id === myUid)?.role || myRole;
    $("roster").innerHTML = roster.map((item) =>
      `<li><span class="dot ${item.connected ? "" : "off"}"></span><b>${escapeHtml(item.user_id)}</b>` +
      `${item.role ? `<span class="badge">${item.role}</span>` : ""}${item.is_host ? '<span class="badge">主持</span>' : ""}</li>`,
    ).join("");
    $("rolePickButtons").innerHTML = ["正方", "反方"].map((side) =>
      `<button class="pick ${myRole === side ? "active" : ""}" data-side="${side}">${side}</button>`,
    ).join("");
    $("rolePickButtons").querySelectorAll("button").forEach((button) => {
      button.onclick = () => send({ type: "claim_role", side: button.dataset.side });
    });
    updateButtons();
  }

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);

  function renderTranscript() {
    $("transcript").textContent = transcriptItems.length
      ? transcriptItems.map((item) => `${item.side || ""} ${item.speaker}：${item.text}`).join("\n\n")
      : "暫未有已 commit 逐字稿。";
  }

  function renderState(message) {
    state = message;
    phase = message.phase || phase;
    if (Number.isFinite(Number(message.server_now_ms))) {
      serverClockOffset = Number(message.server_now_ms) - Date.now();
    }
    if (Number(message.seg_index) !== lastBellSegment) {
      lastBellSegment = Number(message.seg_index);
      firedBells = new Set();
    }
    activeTurnId = message.active_turn_id || "";
    if (activeTurnId && pendingTranscriptChunks.length) flushTranscriptChunks();
    $("segLabel").textContent = message.seg_label || (phase === "lobby" ? "等待開始…" : "");
    $("segSub").textContent = message.rtc_paused ? "P2P 中斷，計時已暫停，正在 ICE restart…" : (message.side || "");
    if (message.judge_enabled === false) {
      $("judgement").textContent = `AI 評判已停用：${message.judge_disabled_reason || "逐字稿不可用"}`;
    }
    updateButtons();
  }

  function formatSeconds(value) {
    const seconds = Math.max(0, Math.floor(value));
    return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function tickTimer() {
    if (phase !== "active") return;
    const now = state.rtc_paused
      ? Number(state.server_now_ms || Date.now() + serverClockOffset)
      : Date.now() + serverClockOffset;
    const elapsed = state.seg_started_ms ? Math.max(0, (now - state.seg_started_ms) / 1000) : 0;
    const isFree = state.side === "雙方";
    let sideUsed = {};
    if (isFree) {
      $("sideTimers").classList.remove("hidden");
      const currentExtra = !state.rtc_paused && state.active_turn_started_ms
        ? Math.max(0, now - Number(state.server_now_ms || now)) : 0;
      sideUsed = { ...(state.side_elapsed_ms || {}) };
      if (state.active_turn_side) sideUsed[state.active_turn_side] = Number(sideUsed[state.active_turn_side] || 0) + currentExtra;
      $("proTimer").textContent = formatSeconds(Math.max(0, Number(state.seconds || 0) - Number(sideUsed["正方"] || 0) / 1000));
      $("conTimer").textContent = formatSeconds(Math.max(0, Number(state.seconds || 0) - Number(sideUsed["反方"] || 0) / 1000));
      $("timer").textContent = "雙方獨立計時";
    } else {
      $("sideTimers").classList.add("hidden");
      $("timer").textContent = formatSeconds(Number(state.seconds || 0) - elapsed);
    }
    if (state.rtc_paused) return;
    for (const [index, bell] of (state.bells || []).entries()) {
      const key = isFree ? `${state.active_turn_side || "none"}:${index}` : String(index);
      const basis = isFree
        ? Number(sideUsed[state.active_turn_side] || 0) / 1000
        : elapsed;
      if ((!isFree || state.active_turn_side) && basis >= Number(bell.t || 0) && !firedBells.has(key)) {
        firedBells.add(key);
        ring(bell.rings);
      }
    }
  }

  function updateButtons() {
    $("hostControls").classList.toggle("hidden", !isHost);
    $("startBtn").disabled = !isHost || phase !== "lobby" || roster.filter((x) => x.connected).length !== 2;
    $("prevBtn").disabled = !isHost || phase !== "active";
    $("nextBtn").disabled = !isHost || phase !== "active";
    $("judgeBtn").disabled = !isHost || state.judge_enabled === false;
    const mine = state.active_speaker == null || state.active_speaker === myUid;
    const sideOk = !state.expected_turn_side || state.expected_turn_side === myRole;
    $("talkBtn").disabled = phase !== "active" || !mine || !sideOk || state.rtc_paused;
    $("talkBtn").classList.toggle("live", state.active_turn_user === myUid);
    $("talkBtn").textContent = state.active_turn_user === myUid ? "停止發言" : "按一下開始發言";
  }

  function flushTranscriptChunks() {
    while (activeTurnId && pendingTranscriptChunks.length) {
      send({
        type: "transcript_chunk", turn_id: activeTurnId,
        sequence: transcriptSequence++, text: pendingTranscriptChunks.shift(),
      });
    }
  }

  function startRecognition() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition || state.judge_enabled === false) return;
    recognitionStopping = false;
    recognition = new SpeechRecognition();
    recognition.lang = "zh-HK";
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.onresult = (event) => {
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        if (event.results[index].isFinal) {
          const text = String(event.results[index][0].transcript || "").trim();
          if (text) pendingTranscriptChunks.push(text);
        }
      }
      flushTranscriptChunks();
    };
    recognition.onerror = (event) => {
      if (!recognitionStopping && !["aborted", "no-speech"].includes(event.error)) {
        send({ type: "judge_disabled", reason: `SpeechRecognition terminal error：${event.error}` });
      }
    };
    recognition.onend = () => {
      if (!recognitionStopping) {
        send({
          type: "judge_disabled",
          reason: "SpeechRecognition 意外停止；逐字稿可能缺漏。",
        });
      }
      recognition = null;
    };
    try {
      recognition.start();
    } catch (error) {
      send({
        type: "judge_disabled",
        reason: `SpeechRecognition 未能開始：${error.message}`,
      });
    }
  }

  async function startTalk() {
    if (turnStarting) return;
    turnStarting = true;
    transcriptSequence = 0;
    pendingTranscriptChunks = [];
    activeTurnId = "";
    send({ type: "turn_begin" });
    const started = await waitFor(() => Boolean(activeTurnId), 2000);
    turnStarting = false;
    if (!started) {
      log("Server未確認發言turn，請再按一次開始發言。");
      return;
    }
    startRecognition();
  }

  async function stopTalk() {
    recognitionStopping = true;
    try { recognition?.stop(); } catch (_) {}
    await new Promise((resolve) => setTimeout(resolve, 500));
    flushTranscriptChunks();
    send({ type: "transcript_commit", turn_id: activeTurnId, final_sequence: transcriptSequence });
    send({ type: "turn_end" });
    recognition = null;
  }

  function haltRecognition() {
    recognitionStopping = true;
    try { recognition?.abort(); } catch (_) {}
    recognition = null;
    pendingTranscriptChunks = [];
  }

  function onMessage(message) {
    switch (message.type) {
      case "roster":
        if (message.topic) $("topicLine").textContent = `${message.debate_format}｜${message.topic}`;
        if (message.transcript) transcriptItems = message.transcript;
        if (message.judgement) $("judgement").textContent = message.judgement;
        renderTranscript();
        renderRoster(message);
        break;
      case "state": renderState(message); break;
      case "rtc_offer": case "rtc_answer": case "rtc_ice":
        onSignal(message).catch((error) => log(`RTC signaling：${error.message}`)); break;
      case "rtc_status":
        if (
          message.status === "preflight_ready"
          && Number(message.roster_generation) === rosterGeneration
          && message.from && message.from !== myUid
        ) {
          readyPeers.add(message.from);
          if (isHost && localRtcReady) makeOffer().catch((error) => log(error.message));
        } else if (
          message.status === "restart"
          && Number(message.roster_generation) === rosterGeneration
          && isHost
        ) {
          restartAttempted = true;
          makeOffer({ iceRestart: true }).catch((error) => log(error.message));
        }
        break;
      case "precheck_request": case "precheck_status":
        activeCheckId = message.check_id || activeCheckId;
        if (message.type === "precheck_request") submitPrecheck(activeCheckId);
        $("networkTestResult").textContent = (message.members || []).map((uid) => {
          const result = message.results?.[uid];
          return `${uid}：${result ? (result.ok ? "media通過" : "media未通過") : "等待測試"}${result?.transcript_ok === false ? "；逐字稿失敗" : ""}`;
        }).join("\n") || $("networkTestResult").textContent;
        break;
      case "precheck_failed": log("開始前 P2P media 測試未全部通過。"); break;
      case "speaking":
        if (!message.speaking && message.user_id === myUid) haltRecognition();
        renderState({ ...state, active_turn_user: message.speaking ? message.user_id : null });
        break;
      case "transcript": transcriptItems.push(message.item); renderTranscript(); break;
      case "judge_disabled":
        haltRecognition();
        state.judge_enabled = false;
        state.judge_disabled_reason = message.reason;
        $("judgement").textContent = `AI 評判已停用：${message.reason}`;
        updateButtons();
        break;
      case "judgement_pending": $("judgement").textContent = "AI 評判分析中…"; break;
      case "judgement": $("judgement").textContent = message.text; break;
      case "ended":
        phase = "ended";
        haltRecognition();
        localStream?.getTracks().forEach((track) => track.stop());
        try { pc?.close(); } catch (_) {}
        log("房間已結束。");
        updateButtons();
        break;
      case "error": log(message.message || "房間錯誤"); break;
    }
  }

  function connect() {
    ws = new WebSocket(wsUrl());
    ws.onopen = () => { $("connStatus").textContent = "控制連線已建立；等待 P2P"; };
    ws.onmessage = (event) => {
      try { onMessage(JSON.parse(event.data)); } catch (error) { log(error.message); }
    };
    ws.onclose = () => {
      $("connStatus").textContent = "控制連線中斷";
      if (phase !== "ended") setTimeout(connect, 1500);
    };
  }

  $("codeText").textContent = code;
  $("modeLabel").textContent = "Mode A · STUN-only P2P";
  $("deviceTestBtn").onclick = runDeviceTest;
  $("startBtn").onclick = () => send({ type: "start" });
  $("nextBtn").onclick = () => send({ type: "next_segment" });
  $("prevBtn").onclick = () => send({ type: "set_segment", index: Math.max(0, Number(state.seg_index || 0) - 1) });
  $("judgeBtn").onclick = () => send({ type: "request_judgement" });
  $("endBtn").onclick = () => confirm("結束呢節練習？") && send({ type: "end" });
  $("talkBtn").onclick = () => state.active_turn_user === myUid ? stopTalk() : startTalk();
  setInterval(() => {
    tickTimer();
    send({ type: "heartbeat" });
  }, 1000);
  connect();
})();
