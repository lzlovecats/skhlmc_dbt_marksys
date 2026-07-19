/* Mode A: STUN-only two-member WebRTC media; Render carries control text only. */
(() => {
  "use strict";

  const boot = window.ROOM_P2P_BOOTSTRAP;
  if (!boot) return;

  const $ = (id) => document.getElementById(id);
  const code = String(boot.code || "").toUpperCase();
  const PARENT_CHANNEL = "ai-coach-room";
  const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 20000];
  const RECOGNITION_STOP_TIMEOUT_MS = 2500;
  const FORCED_RECOGNITION_STOP_MAX_MS = 700;
  const RESULT_POLL_INTERVAL_MS = 3000;
  const RESULT_POLL_WINDOW_MS = 15 * 60 * 1000;
  const RESULT_POLL_MAX_ATTEMPTS = 300; // Up to the 15-minute result window.
  const ENDED_SNAPSHOT_RETRY_DELAYS_MS = [1000, 3000, 8000, 15000, 30000];
  const TEST_MAX_AGE_MS = 5 * 60 * 1000;
  const MAX_PENDING_REMOTE_ICE = 100;

  let ws;
  let socketEpoch = 0;
  let controlConnected = false;
  let reconnectTimer = 0;
  let reconnectStableTimer = 0;
  let reconnectAttempt = 0;
  let terminalConnection = false;
  let lastServerError = "";
  let parentReadyPosted = false;
  let parentTerminalPosted = false;
  let peerLeftUser = "";
  let leaveToken = "";

  let myUid = "";
  let isHost = false;
  let myRole = "";
  let phase = "lobby";
  let roster = [];
  let rosterGeneration = 0;
  let state = {};
  let lastStateSequence = -1;
  let serverClockOffset = 0;

  let pc;
  let peerEpoch = 0;
  let peerPromise;
  let offerPromise;
  let signalQueue = Promise.resolve();
  let localSignalGeneration = 0;
  let pendingRemoteIce = [];
  let activeRecoveryPromise;
  let restartTimer = 0;
  let restartAttempted = false;
  let lastReportedRtc = "";

  let localStream;
  let localStreamEpoch = 0;
  let localStreamPromise;
  let remoteStream;
  let remoteAudio;
  let dataChannel;
  let dataPingOk = false;
  let preflightRunning = false;
  let micMode = "muted";

  let localTest = null;
  let activeCheckId = "";
  let localRtcReady = false;
  let readyPeers = new Set();

  let transcriptItems = [];
  let recognition;
  let recognitionWanted = false;
  let recognitionRestartTimer = 0;
  let recognitionStopPromise;
  let recognitionPausedForRtc = false;
  let recognitionUnsupportedNotified = false;
  let interimTranscript = "";
  let transcriptSequence = 0;
  let pendingTranscriptChunks = [];
  let ownedTurnId = "";
  let ownedTurnRequestId = "";
  let pendingTurnRequestId = "";
  let turnStarting = false;
  let turnEnding = false;

  const seenBellEvents = new Set();
  let resultPollTimer = 0;
  let resultPollAttempts = 0;
  let resultPollDeadline = 0;
  let endedSnapshotTimer = 0;
  let judgementPending = false;
  let judgementRequested = false;
  let canRequestJudgement = false;
  let judgementRequestInFlight = false;
  let transcriptRevision = 0;
  let judgementRevision = -1;
  let heartbeatTimer = 0;

  const escapeHtml = (value) => String(value ?? "").replace(
    /[&<>"']/g,
    (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char],
  );

  function newTurnRequestId() {
    const cryptoApi = globalThis.crypto;
    if (typeof cryptoApi?.randomUUID === "function") return cryptoApi.randomUUID();
    const bytes = new Uint8Array(16);
    if (typeof cryptoApi?.getRandomValues === "function") {
      cryptoApi.getRandomValues(bytes);
      return Array.from(
        bytes,
        (value) => value.toString(16).padStart(2, "0"),
      ).join("");
    }
    return `${Date.now()}-${Math.random().toString(36).slice(2, 18)}`;
  }

  const log = (text) => {
    const line = `[${new Date().toLocaleTimeString("zh-HK")}] ${text}`;
    $("log").textContent = (line + "\n" + $("log").textContent).slice(0, 5000);
  };

  function showRoomAlert(message) {
    const alert = $("roomAlert");
    alert.textContent = String(message || "");
    alert.classList.toggle("hidden", !message);
  }

  function postToParent(type, payload = {}) {
    if (window.parent === window) return;
    window.parent.postMessage({
      channel: PARENT_CHANNEL,
      type,
      code,
      ...payload,
    }, location.origin);
  }

  function postRoomPhase() {
    postToParent("room_phase", { phase });
  }

  function postRoomReady() {
    const firstReady = !parentReadyPosted;
    parentReadyPosted = true;
    if (firstReady) reconnectAttempt = 0;
    postToParent("room_ready", { phase, leave_token: leaveToken });
  }

  function postRoomTerminal(message, closeCode = 0, recoverable = false) {
    if (parentTerminalPosted) return;
    parentTerminalPosted = true;
    terminalConnection = true;
    clearInterval(heartbeatTimer);
    heartbeatTimer = 0;
    postToParent("room_terminal", {
      message: String(message || "房間控制連線無法建立。"),
      close_code: Number(closeCode || 0),
      recoverable: Boolean(recoverable),
    });
  }

  function send(value) {
    if (!controlConnected || ws?.readyState !== WebSocket.OPEN) return false;
    try {
      ws.send(JSON.stringify(value));
      return true;
    } catch (_) {
      return false;
    }
  }

  function wsUrl() {
    if (boot.wsBase) {
      return `${boot.wsBase.replace(/\/$/, "")}/room/${encodeURIComponent(code)}`;
    }
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${location.host}/room/${encodeURIComponent(code)}`;
  }

  function ring(count = 1) {
    if (!boot.bellSrc) return;
    for (let index = 0; index < Math.max(1, Number(count)); index += 1) {
      setTimeout(() => {
        const audio = new Audio(boot.bellSrc);
        audio.play().catch(() => {});
      }, index * 650);
    }
  }

  function renderMicStatus() {
    const indicator = $("micStatus");
    indicator.classList.remove("live", "testing");
    if (micMode === "live") {
      indicator.textContent = "🎙️ 麥克風：已開啟（你正在發言，聲音會傳送給對方）";
      indicator.classList.add("live");
    } else if (micMode === "testing") {
      indicator.textContent = "🧪 麥克風：測試期間暫時開啟";
      indicator.classList.add("testing");
    } else {
      indicator.textContent = "🔇 麥克風：已靜音（聲音不會傳送）";
    }
  }

  function applyRemoteAudioGate() {
    if (!remoteAudio) return;
    const peerIds = new Set(
      roster
        .filter((item) => item.connected && item.user_id !== myUid)
        .map((item) => item.user_id),
    );
    const testAudioAllowed = Boolean(
      controlConnected && phase === "lobby" && preflightRunning,
    );
    const turnAudioAllowed = Boolean(
      controlConnected
      && phase === "active"
      && !state.rtc_paused
      && !state.turn_stop_pending
      && peerIds.has(state.active_turn_user),
    );
    remoteAudio.muted = !(testAudioAllowed || turnAudioAllowed);
    if (!remoteAudio.muted && remoteAudio.paused) {
      remoteAudio.play().catch(() => {});
    }
  }

  function authoritativeMicAllowed() {
    return Boolean(
      controlConnected
      && phase === "active"
      && !state.rtc_paused
      && state.active_turn_user === myUid
      && state.active_turn_id
      && state.active_turn_id === ownedTurnId
      && localStream?.getAudioTracks().some(
        (track) => track.readyState === "live",
      )
      && !state.turn_stop_pending
      && !turnEnding
    );
  }

  function applyMicTracks() {
    const enabled = micMode === "testing"
      ? preflightRunning && phase === "lobby"
      : micMode === "live" && authoritativeMicAllowed();
    localStream?.getAudioTracks().forEach((track) => {
      track.enabled = enabled;
    });
    if (!enabled && micMode === "live") micMode = "muted";
    renderMicStatus();
  }

  function setMicMode(nextMode) {
    if (nextMode === "live" && !authoritativeMicAllowed()) nextMode = "muted";
    if (nextMode === "testing" && (!preflightRunning || phase !== "lobby")) {
      nextMode = "muted";
    }
    micMode = nextMode;
    applyMicTracks();
  }

  async function ensureLocalStream() {
    if (localStream?.active) {
      applyMicTracks();
      return localStream;
    }
    if (localStreamPromise) return localStreamPromise;
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("此瀏覽器不支援收音咪權限；請改用電腦版 Chrome");
    }
    const epoch = localStreamEpoch;
    const promise = navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      video: false,
    }).then((stream) => {
      if (epoch !== localStreamEpoch) {
        stream.getTracks().forEach((track) => track.stop());
        throw new Error("收音咪請求已取消");
      }
      localStream = stream;
      stream.getAudioTracks().forEach((track) => {
        track.addEventListener("ended", () => {
          if (localStream !== stream) return;
          setMicMode("muted");
          invalidateLocalTest("收音咪已停止；請重新授權並進行連線測試。");
          reportRtcStatus("disconnected", true);
          if (phase === "active" && controlConnected && !terminalConnection) {
            recoverActivePeer({ forceRestart: true });
          }
        }, { once: true });
      });
      applyMicTracks();
      return stream;
    });
    localStreamPromise = promise;
    try {
      return await promise;
    } finally {
      if (localStreamPromise === promise) localStreamPromise = null;
    }
  }

  function stopLocalStream() {
    setMicMode("muted");
    localStreamEpoch += 1;
    localStream?.getTracks().forEach((track) => track.stop());
    localStream = null;
    localStreamPromise = null;
  }

  function ensureRemoteAudio() {
    if (remoteAudio) return remoteAudio;
    remoteAudio = document.createElement("audio");
    remoteAudio.autoplay = true;
    remoteAudio.playsInline = true;
    remoteAudio.muted = true;
    remoteAudio.setAttribute("aria-label", "對方即時聲音");
    document.body.appendChild(remoteAudio);
    return remoteAudio;
  }

  function setupDataChannel(channel, expectedPeer) {
    dataChannel = channel;
    channel.onopen = () => {
      if (pc !== expectedPeer || dataChannel !== channel) return;
      try { channel.send("ping:" + Date.now()); } catch (_) {}
    };
    channel.onmessage = (event) => {
      if (pc !== expectedPeer || dataChannel !== channel) return;
      if (typeof event.data !== "string" || event.data.length > 100) {
        dataPingOk = false;
        try { channel.close(); } catch (_) {}
        return;
      }
      const value = event.data;
      if (!/^(?:ping|pong):\d{1,16}$/.test(value)) return;
      if (value.startsWith("ping:")) {
        try { channel.send("pong:" + value.slice(5)); } catch (_) {}
      }
      if (value.startsWith("pong:")) dataPingOk = true;
    };
  }

  function resetPeer({ keepLocalStream = true } = {}) {
    peerEpoch += 1;
    const oldPeer = pc;
    if (oldPeer) {
      oldPeer.onconnectionstatechange = null;
      oldPeer.onicecandidate = null;
      oldPeer.ontrack = null;
      oldPeer.ondatachannel = null;
    }
    try { oldPeer?.close(); } catch (_) {}
    pc = null;
    peerPromise = null;
    offerPromise = null;
    dataChannel = null;
    remoteStream = null;
    pendingRemoteIce = [];
    localSignalGeneration = 0;
    if (remoteAudio) remoteAudio.srcObject = null;
    if (!keepLocalStream) stopLocalStream();
  }

  function rtcOperationIsCurrent(activePeer, operationPeerEpoch, generation) {
    return Boolean(
      pc === activePeer
      && peerEpoch === operationPeerEpoch
      && Number(generation) === rosterGeneration
      && controlConnected
      && !terminalConnection
      && phase !== "ended"
    );
  }

  function resetExactStalePeer(activePeer, operationPeerEpoch) {
    // A newer peer may already have replaced the one whose async description
    // operation just completed.  Never let the stale continuation tear it down.
    if (pc !== activePeer || peerEpoch !== operationPeerEpoch) return false;
    resetPeer({ keepLocalStream: true });
    const recoveryPeerEpoch = peerEpoch;
    if (phase === "active" && controlConnected && !terminalConnection) {
      // Let any current single-flight recovery unwind before starting the
      // current-generation negotiation on a clean peer.
      setTimeout(() => {
        if (
          peerEpoch === recoveryPeerEpoch
          && pc === null
          && phase === "active"
          && controlConnected
          && !terminalConnection
        ) {
          recoverActivePeer({ forceRestart: true });
        }
      }, 0);
    }
    return true;
  }

  async function ensurePeer() {
    if (pc && pc.signalingState !== "closed") {
      const liveAudioSender = pc.getSenders().some(
        (sender) => sender.track?.kind === "audio" && sender.track.readyState === "live",
      );
      if (localStream?.active && liveAudioSender) return pc;
      resetPeer({ keepLocalStream: Boolean(localStream?.active) });
    }
    if (peerPromise) return peerPromise;
    const epoch = peerEpoch;
    const promise = (async () => {
      const stream = await ensureLocalStream();
      if (epoch !== peerEpoch) throw new Error("P2P 建立已取消");
      if (pc && pc.signalingState !== "closed") return pc;

      const createdPeer = new RTCPeerConnection({
        iceServers: [{ urls: "stun:stun.cloudflare.com:3478" }],
        iceTransportPolicy: "all",
      });
      if (epoch !== peerEpoch) {
        createdPeer.close();
        throw new Error("P2P 建立已取消");
      }
      pc = createdPeer;
      stream.getAudioTracks().forEach((track) => createdPeer.addTrack(track, stream));
      applyMicTracks();

      createdPeer.ontrack = (event) => {
        if (pc !== createdPeer || epoch !== peerEpoch) return;
        remoteStream = event.streams[0] || new MediaStream([event.track]);
        const audio = ensureRemoteAudio();
        audio.srcObject = remoteStream;
        applyRemoteAudioGate();
        event.track.addEventListener("ended", () => {
          if (pc !== createdPeer || epoch !== peerEpoch) return;
          setMicMode("muted");
          applyRemoteAudioGate();
          invalidateLocalTest("對方音軌已停止；P2P 連線需要重新建立。");
          reportRtcStatus("disconnected", true);
          resetPeer({ keepLocalStream: true });
          if (phase === "active" && controlConnected && !terminalConnection) {
            recoverActivePeer({ forceRestart: true });
          }
        }, { once: true });
      };
      createdPeer.onicecandidate = (event) => {
        if (pc !== createdPeer || epoch !== peerEpoch || !event.candidate) return;
        send({
          type: "rtc_ice",
          candidate: event.candidate.toJSON(),
          roster_generation: localSignalGeneration || rosterGeneration,
        });
      };
      createdPeer.ondatachannel = (event) => {
        if (pc === createdPeer && epoch === peerEpoch) {
          setupDataChannel(event.channel, createdPeer);
        }
      };
      createdPeer.onconnectionstatechange = () => {
        if (pc === createdPeer && epoch === peerEpoch) handleConnectionState();
      };
      if (isHost) {
        setupDataChannel(
          createdPeer.createDataChannel("preflight", { ordered: true }),
          createdPeer,
        );
      }
      return createdPeer;
    })();
    peerPromise = promise;
    try {
      return await promise;
    } finally {
      if (peerPromise === promise) peerPromise = null;
    }
  }

  async function makeOffer(options = {}) {
    const lobbyReady = localRtcReady && readyPeers.size >= 1;
    if (
      !controlConnected
      || !isHost
      || (phase !== "active" && !lobbyReady)
      || roster.filter((item) => item.connected).length !== 2
    ) return false;
    if (offerPromise) return offerPromise;

    const promise = (async () => {
      if (pc?.connectionState === "failed" || pc?.signalingState === "closed") {
        resetPeer({ keepLocalStream: true });
      }
      const activePeer = await ensurePeer();
      if (activePeer !== pc || activePeer.signalingState !== "stable") return false;
      const operationPeerEpoch = peerEpoch;
      const generation = rosterGeneration;
      localSignalGeneration = generation;
      const description = await activePeer.createOffer(options);
      if (!rtcOperationIsCurrent(activePeer, operationPeerEpoch, generation)) {
        return false;
      }
      await activePeer.setLocalDescription(description);
      if (!rtcOperationIsCurrent(activePeer, operationPeerEpoch, generation)) {
        resetExactStalePeer(activePeer, operationPeerEpoch);
        return false;
      }
      const sent = send({
        type: "rtc_offer",
        description: activePeer.localDescription.toJSON(),
        roster_generation: generation,
      });
      if (!sent) resetExactStalePeer(activePeer, operationPeerEpoch);
      return sent;
    })();
    offerPromise = promise;
    try {
      return await promise;
    } finally {
      if (offerPromise === promise) offerPromise = null;
    }
  }

  async function flushRemoteIce(generation, activePeer, operationPeerEpoch) {
    if (
      !rtcOperationIsCurrent(activePeer, operationPeerEpoch, generation)
      || !activePeer.remoteDescription
    ) return false;
    const ready = pendingRemoteIce.filter((item) => item.generation === generation);
    pendingRemoteIce = pendingRemoteIce.filter((item) => item.generation !== generation);
    for (const item of ready) {
      try {
        await activePeer.addIceCandidate(item.candidate);
      } catch (error) {
        log(`RTC ICE：${error.message}`);
      }
      if (!rtcOperationIsCurrent(activePeer, operationPeerEpoch, generation)) {
        return false;
      }
    }
    return true;
  }

  function queueRemoteIce(generation, candidate) {
    pendingRemoteIce.push({ generation, candidate });
    if (pendingRemoteIce.length > MAX_PENDING_REMOTE_ICE) {
      pendingRemoteIce.splice(0, pendingRemoteIce.length - MAX_PENDING_REMOTE_ICE);
    }
  }

  async function onSignal(message) {
    const generation = Number(message.roster_generation);
    if (
      generation !== rosterGeneration
      || !controlConnected
      || terminalConnection
      || phase === "ended"
      || (phase === "lobby" && !preflightRunning && !localRtcReady)
    ) return;

    if (message.type === "rtc_ice" && !pc) {
      queueRemoteIce(generation, message.candidate);
      await ensurePeer();
      return;
    }

    const activePeer = await ensurePeer();
    const operationPeerEpoch = peerEpoch;
    if (!rtcOperationIsCurrent(activePeer, operationPeerEpoch, generation)) return;
    if (message.type === "rtc_offer") {
      localSignalGeneration = generation;
      if (activePeer.signalingState !== "stable") {
        log(`忽略非 stable 狀態的 RTC offer（${activePeer.signalingState}）。`);
        return;
      }
      await activePeer.setRemoteDescription(message.description);
      if (!rtcOperationIsCurrent(activePeer, operationPeerEpoch, generation)) {
        resetExactStalePeer(activePeer, operationPeerEpoch);
        return;
      }
      if (!await flushRemoteIce(generation, activePeer, operationPeerEpoch)) {
        resetExactStalePeer(activePeer, operationPeerEpoch);
        return;
      }
      const answer = await activePeer.createAnswer();
      if (!rtcOperationIsCurrent(activePeer, operationPeerEpoch, generation)) {
        resetExactStalePeer(activePeer, operationPeerEpoch);
        return;
      }
      await activePeer.setLocalDescription(answer);
      if (!rtcOperationIsCurrent(activePeer, operationPeerEpoch, generation)) {
        resetExactStalePeer(activePeer, operationPeerEpoch);
        return;
      }
      const sent = send({
        type: "rtc_answer",
        description: activePeer.localDescription.toJSON(),
        roster_generation: generation,
      });
      if (!sent) resetExactStalePeer(activePeer, operationPeerEpoch);
    } else if (message.type === "rtc_answer") {
      if (activePeer.signalingState !== "have-local-offer") return;
      await activePeer.setRemoteDescription(message.description);
      if (!rtcOperationIsCurrent(activePeer, operationPeerEpoch, generation)) {
        resetExactStalePeer(activePeer, operationPeerEpoch);
        return;
      }
      if (!await flushRemoteIce(generation, activePeer, operationPeerEpoch)) {
        resetExactStalePeer(activePeer, operationPeerEpoch);
      }
    } else if (message.type === "rtc_ice") {
      if (!activePeer.remoteDescription) {
        queueRemoteIce(generation, message.candidate);
        return;
      }
      try {
        await activePeer.addIceCandidate(message.candidate);
      } catch (error) {
        log(`RTC ICE：${error.message}`);
      }
      if (!rtcOperationIsCurrent(activePeer, operationPeerEpoch, generation)) {
        resetExactStalePeer(activePeer, operationPeerEpoch);
      }
    }
  }

  function reportRtcStatus(status, force = false) {
    if (!force && lastReportedRtc === status) return false;
    const sent = send({
      type: "rtc_status",
      status,
      roster_generation: rosterGeneration,
    });
    if (sent) lastReportedRtc = status;
    return sent;
  }

  async function recoverActivePeer({ forceRestart = false } = {}) {
    if (phase !== "active" || !controlConnected) return;
    if (!forceRestart && pc?.connectionState === "connected") {
      reportRtcStatus("connected", true);
      return;
    }
    if (activeRecoveryPromise) return activeRecoveryPromise;
    const promise = (async () => {
      setMicMode("muted");
      if (pc?.connectionState === "failed" || pc?.signalingState === "closed") {
        resetPeer({ keepLocalStream: true });
      }
      await ensurePeer();
      if (isHost && roster.filter((item) => item.connected).length === 2) {
        await makeOffer({ iceRestart: true });
      }
    })();
    activeRecoveryPromise = promise;
    try {
      await promise;
    } catch (error) {
      showRoomAlert(`未能恢復 P2P：${error.message}`);
      log(`P2P 恢復：${error.message}`);
    } finally {
      if (activeRecoveryPromise === promise) activeRecoveryPromise = null;
    }
  }

  function invalidateLocalTest(message, { resetConnection = false } = {}) {
    localTest = null;
    localRtcReady = false;
    readyPeers = new Set();
    dataPingOk = false;
    activeCheckId = "";
    if (message) $("networkTestResult").textContent = message;
    if (resetConnection) resetPeer({ keepLocalStream: true });
    setMicMode("muted");
  }

  function handleConnectionState() {
    const status = pc?.connectionState || "new";
    $("connStatus").textContent = controlConnected
      ? `控制連線已建立；P2P：${status}`
      : `控制連線中斷；P2P：${status}`;
    if (status === "connected") {
      clearTimeout(restartTimer);
      restartTimer = 0;
      restartAttempted = false;
      reportRtcStatus("connected", true);
      return;
    }
    if (phase === "lobby" && ["disconnected", "failed", "closed"].includes(status)) {
      reportRtcStatus(status === "failed" ? "failed" : "disconnected", true);
      if (localTest) {
        invalidateLocalTest(
          "P2P 連線在測試後中斷；請兩部裝置重新按連線測試。",
        );
      }
      return;
    }
    if (phase !== "active" || !["disconnected", "failed"].includes(status)) return;

    setMicMode("muted");
    reportRtcStatus("disconnected", true);
    if (!restartAttempted) {
      restartAttempted = true;
      if (isHost) recoverActivePeer({ forceRestart: true });
      restartTimer = setTimeout(() => {
        if (pc?.connectionState !== "connected") {
          reportRtcStatus("failed", true);
          log("P2P ICE restart 10 秒內未能恢復，房間將安全結束。");
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
        packets ||= Number(report.packetsReceived || 0) > 0
          && Number(report.bytesReceived || 0) > 0;
      }
    });
    if (!packets) return false;

    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return false;
    const context = new AudioContextClass();
    try {
      await context.resume();
      const analyser = context.createAnalyser();
      analyser.fftSize = 512;
      context.createMediaStreamSource(remoteStream).connect(analyser);
      const values = new Uint8Array(analyser.fftSize);
      let energy = 0;
      for (let index = 0; index < 20; index += 1) {
        analyser.getByteTimeDomainData(values);
        energy = Math.max(
          energy,
          values.reduce((sum, item) => sum + Math.abs(item - 128), 0),
        );
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
      return energy > 20;
    } finally {
      await context.close().catch(() => {});
    }
  }

  async function runDeviceTest() {
    if (!controlConnected || phase !== "lobby" || preflightRunning) {
      showRoomAlert("只可在大堂控制連線正常時進行連線測試。");
      return;
    }
    preflightRunning = true;
    applyRemoteAudioGate();
    $("deviceTestBtn").disabled = true;
    $("networkTestResult").textContent =
      "請兩部裝置同時對咪講話；正在測試咪、播放、P2P ICE、data-channel 及遠端聲音…";
    try {
      dataPingOk = false;
      await ensureLocalStream();
      setMicMode("testing");
      await ensurePeer();
      localRtcReady = true;
      reportRtcStatus("preflight_ready", true);
      if (isHost) await makeOffer();
      const connected = await waitFor(() => pc?.connectionState === "connected", 12000);
      if (!connected) throw new Error("P2P ICE 未能連接；請轉 Wi-Fi／流動數據再試");
      ensureRemoteAudio();
      await remoteAudio.play();
      if (dataChannel?.readyState === "open") {
        dataChannel.send("ping:" + Date.now());
      }
      const ping = await waitFor(() => dataPingOk, 3000);
      const media = ping && await remoteMediaEvidence();
      if (!media) {
        throw new Error("未收到對方 data-channel ping 及遠端聲音；請雙方同時再試");
      }
      localTest = {
        media_ok: true,
        message: "連線測試通過",
        testedAt: Date.now(),
        rosterGeneration,
        testedPeer: pc,
      };
      $("networkTestResult").textContent = localTest.message;
      if (activeCheckId) submitPrecheck(activeCheckId);
    } catch (error) {
      localTest = {
        media_ok: false,
        message: error.message,
        testedAt: Date.now(),
        rosterGeneration,
        testedPeer: pc,
      };
      $("networkTestResult").textContent = `未通過：${error.message}`;
      if (activeCheckId) submitPrecheck(activeCheckId);
    } finally {
      preflightRunning = false;
      setMicMode("muted");
      applyRemoteAudioGate();
      updateButtons();
    }
  }

  function localTestIsCurrent() {
    return Boolean(
      localTest
      && Date.now() - localTest.testedAt <= TEST_MAX_AGE_MS
      && localTest.rosterGeneration === rosterGeneration
      && localTest.testedPeer === pc
      && localStream?.active
      && localStream.getAudioTracks().some((track) => track.readyState === "live")
      && pc?.connectionState === "connected"
    );
  }

  function submitPrecheck(checkId) {
    if (!checkId) return;
    if (!localTestIsCurrent()) {
      localTest = null;
      $("networkTestResult").textContent =
        "主持已要求開始，但本機測試已失效；請重新按「開始連線測試」。";
      return;
    }
    send({
      type: "precheck_result",
      check_id: checkId,
      media_ok: Boolean(localTest.media_ok),
      message: localTest.message,
    });
  }

  function renderPrecheck(message, { failed = false } = {}) {
    const lines = (message.members || []).map((uid) => {
      const result = message.results?.[uid];
      if (!result) return `${uid}：等待測試`;
      const detail = String(result.message || "").trim();
      return `${uid}：${result.ok ? "通過" : "未通過"}${detail ? `（${detail}）` : ""}`;
    });
    if (lines.length) $("networkTestResult").textContent = lines.join("\n");
    if (failed) {
      activeCheckId = "";
      localTest = null;
      localRtcReady = false;
      setMicMode("muted");
      showRoomAlert("開始前測試未全部通過；請按上方詳情修正後重新測試。");
    }
  }

  function renderRosterList(items) {
    const safeItems = Array.isArray(items) ? items : [];
    $("roster").innerHTML = safeItems.map((item) => (
      `<li><span class="dot ${item.connected ? "" : "off"}"></span>`
      + `<b>${escapeHtml(item.user_id)}</b>`
      + `${item.role ? `<span class="badge">${escapeHtml(item.role)}</span>` : ""}`
      + `${item.is_host ? '<span class="badge">主持</span>' : ""}</li>`
    )).join("");
  }

  function renderRoster(message) {
    const incomingGeneration = Number(message.roster_generation);
    let effectiveGeneration = incomingGeneration;
    if (
      Number.isFinite(incomingGeneration)
      && incomingGeneration < rosterGeneration
    ) {
      // A direct bootstrap includes identity fields that ordinary roster
      // broadcasts do not.  Preserve those fields, but never roll live roster
      // or signalling state back to an older generation.
      message = {
        ...message,
        roster,
        roster_generation: rosterGeneration,
      };
      effectiveGeneration = rosterGeneration;
    }
    const previousGeneration = rosterGeneration;
    roster = Array.isArray(message.roster) ? message.roster : [];
    rosterGeneration = Number.isFinite(effectiveGeneration)
      ? effectiveGeneration
      : rosterGeneration;
    myUid = message.you || myUid;
    isHost = message.is_host ?? isHost;
    myRole = roster.find((item) => item.user_id === myUid)?.role || myRole;

    if (previousGeneration && previousGeneration !== rosterGeneration) {
      pendingRemoteIce = pendingRemoteIce.filter(
        (item) => item.generation === rosterGeneration,
      );
      localSignalGeneration = rosterGeneration;
      if (phase === "lobby") {
        invalidateLocalTest(
          "成員／辯方有變，請兩部裝置重新按連線測試。",
          { resetConnection: true },
        );
      } else if (phase === "active" && pc?.signalingState !== "stable") {
        // The server can advance its generation before this roster frame reaches
        // us.  An offer sent in that window is rejected server-side, so discard
        // only that exact half-negotiated peer and restart with the new epoch.
        resetExactStalePeer(pc, peerEpoch);
      }
    }

    renderRosterList(roster);
    $("rolePickButtons").innerHTML = ["正方", "反方"].map((side) => (
      `<button class="pick ${myRole === side ? "active" : ""}" `
      + `data-side="${side}">${side}</button>`
    )).join("");
    $("rolePickButtons").querySelectorAll("button").forEach((button) => {
      button.onclick = () => send({ type: "claim_role", side: button.dataset.side });
    });

    if (message.you) postRoomReady();
    const disconnectedPeer = roster.find(
      (item) => item.user_id !== myUid && !item.connected,
    );
    if (phase === "active" && disconnectedPeer) {
      peerLeftUser = disconnectedPeer.user_id;
      setMicMode("muted");
      if (remoteAudio) remoteAudio.muted = true;
      showRoomAlert("對方已離開房間；計時及麥克風已暫停。");
    } else if (
      peerLeftUser
      && roster.some((item) => item.user_id === peerLeftUser && item.connected)
    ) {
      peerLeftUser = "";
      showRoomAlert("");
    }
    if (pc?.connectionState === "connected") reportRtcStatus("connected", true);
    if (phase === "active" && pc?.connectionState !== "connected") {
      reportRtcStatus("disconnected", true);
      recoverActivePeer();
    }
    applyRemoteAudioGate();
    updateButtons();
  }

  function renderTranscript() {
    $("transcript").textContent = transcriptItems.length
      ? transcriptItems.map((item) => {
        const marker = item.partial ? "【可能不完整】" : "";
        return `${item.side || ""} ${item.speaker || ""}${marker}：${item.text || ""}`;
      }).join("\n\n")
      : "暫未有已 commit 逐字稿。";
  }

  function transcriptItemKey(item) {
    const turnId = String(item?.turn_id || "");
    if (turnId) return `turn:${turnId}`;
    const revision = Number(item?.revision);
    return Number.isFinite(revision) && revision > 0
      ? `revision:${revision}`
      : "";
  }

  function sortTranscriptItems() {
    transcriptItems.sort((left, right) => {
      const leftRevision = Number(left?.revision);
      const rightRevision = Number(right?.revision);
      if (Number.isFinite(leftRevision) && Number.isFinite(rightRevision)) {
        return leftRevision - rightRevision;
      }
      return Number(left?.created_ms || 0) - Number(right?.created_ms || 0);
    });
  }

  function replaceTranscriptSnapshot(items) {
    transcriptItems = [];
    (Array.isArray(items) ? items : []).forEach((item) => {
      const key = transcriptItemKey(item);
      const index = key
        ? transcriptItems.findIndex((existing) => transcriptItemKey(existing) === key)
        : -1;
      if (index >= 0) transcriptItems[index] = item;
      else transcriptItems.push(item);
    });
    sortTranscriptItems();
  }

  function mergeTranscriptItem(item) {
    if (!item || typeof item !== "object") return;
    const key = transcriptItemKey(item);
    const index = key
      ? transcriptItems.findIndex((existing) => transcriptItemKey(existing) === key)
      : -1;
    if (index >= 0) transcriptItems[index] = item;
    else transcriptItems.push(item);
    sortTranscriptItems();
  }

  async function roomApi(url, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(url, {
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        ...options,
        signal: controller.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(
          typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`,
        );
        error.status = response.status;
        throw error;
      }
      return data;
    } finally {
      clearTimeout(timeout);
    }
  }

  const fetchRoomResult = () => roomApi(
    `/api/room/${encodeURIComponent(code)}/transcript`,
  );

  function applyRoomResult(data) {
    phase = data.phase || phase;
    isHost = data.is_host ?? isHost;
    if (Array.isArray(data.transcript)) replaceTranscriptSnapshot(data.transcript);
    transcriptRevision = Number(data.transcript_revision ?? transcriptRevision);
    judgementRevision = Number(data.judgement_revision ?? judgementRevision);
    judgementPending = Boolean(data.judgement_pending);
    judgementRequested = Boolean(data.judgement_requested);
    canRequestJudgement = Boolean(data.can_request_judgement);
    state.judge_enabled = data.judge_enabled ?? state.judge_enabled;
    state.judge_disabled_reason = data.judge_disabled_reason
      || state.judge_disabled_reason
      || "";
    if (Array.isArray(data.roster)) renderRosterList(data.roster);
    renderTranscript();
    if (data.topic) {
      $("topicLine").textContent = `${data.debate_format || ""}｜${data.topic}`;
    }
    if (data.judgement) {
      $("judgement").textContent = data.judgement;
    } else if (judgementPending) {
      $("judgement").textContent = "AI 評判分析中…可保留或稍後重新開啟本房查看。";
    } else if (state.judge_enabled === false) {
      $("judgement").textContent =
        `AI 評判已停用：${state.judge_disabled_reason || "AI 評價目前不可用"}`;
    } else if (phase === "ended" && isHost && !judgementRequested) {
      const sides = new Set(
        transcriptItems
          .filter((item) => String(item.text || "").trim())
          .map((item) => item.side),
      );
      const missing = ["正方", "反方"].filter((side) => !sides.has(side));
      $("judgement").textContent = missing.length
        ? `暫未能要求 AI 評判：${missing.join("、")}未有逐字稿。`
        : "完場後可由主持按一次「要求 AI 評判」。";
    }
    if (phase === "ended") postRoomReady();
    postRoomPhase();
    renderEndedUi();
    updateButtons();
  }

  function stopResultPolling() {
    if (resultPollTimer) clearTimeout(resultPollTimer);
    resultPollTimer = 0;
  }

  function startResultPolling() {
    resultPollAttempts = 0;
    resultPollDeadline = Date.now() + RESULT_POLL_WINDOW_MS;
    pollEndedResult();
  }

  async function pollEndedResult() {
    stopResultPolling();
    if (
      phase !== "ended"
      || !judgementPending
      || resultPollAttempts >= RESULT_POLL_MAX_ATTEMPTS
      || Date.now() >= resultPollDeadline
    ) return;
    resultPollAttempts += 1;
    try {
      const data = await fetchRoomResult();
      applyRoomResult(data);
      if (!judgementPending) return;
    } catch (error) {
      if ([401, 403, 404].includes(error.status)) {
        log(`未能再讀取完場結果：${error.message}`);
        return;
      }
    }
    resultPollTimer = setTimeout(
      pollEndedResult,
      Math.min(RESULT_POLL_INTERVAL_MS, Math.max(0, resultPollDeadline - Date.now())),
    );
  }

  async function loadEndedSnapshot(attempt = 0) {
    if (phase !== "ended") return;
    if (endedSnapshotTimer) clearTimeout(endedSnapshotTimer);
    endedSnapshotTimer = 0;
    try {
      const data = await fetchRoomResult();
      applyRoomResult(data);
      if (judgementPending) {
        startResultPolling();
      }
    } catch (error) {
      log(`未能載入完場結果：${error.message}`);
      if ([401, 403, 404].includes(error.status)) {
        showRoomAlert(`未能讀取完場結果：${error.message}`);
        return;
      }
      if (attempt >= ENDED_SNAPSHOT_RETRY_DELAYS_MS.length) {
        showRoomAlert("暫未能讀取完場結果；請保留房間並重新載入頁面再試。");
        return;
      }
      const delay = ENDED_SNAPSHOT_RETRY_DELAYS_MS[attempt];
      $("judgement").textContent =
        `正在重新讀取完場結果（${Math.ceil(delay / 1000)} 秒後重試）…`;
      endedSnapshotTimer = setTimeout(
        () => loadEndedSnapshot(attempt + 1),
        delay,
      );
    }
  }

  async function waitForEndingSnapshot(attempt = 0) {
    if (phase !== "ending") return;
    try {
      const data = await fetchRoomResult();
      applyRoomResult(data);
      if (phase === "ended") {
        if (judgementPending) {
          startResultPolling();
        }
        return;
      }
    } catch (error) {
      if ([401, 403, 404].includes(error.status)) {
        showRoomAlert(`未能讀取完場結果：${error.message}`);
        return;
      }
    }
    if (attempt >= 15) {
      showRoomAlert("房間正在完成逐字稿；請稍後重新載入頁面查看結果。");
      return;
    }
    endedSnapshotTimer = setTimeout(
      () => waitForEndingSnapshot(attempt + 1),
      1000,
    );
  }

  async function requestJudgement() {
    if (
      judgementRequestInFlight
      || !isHost
      || phase !== "ended"
      || judgementPending
      || judgementRequested
      || state.judge_enabled === false
    ) return;
    const transcriptSides = new Set(
      transcriptItems
        .filter((item) => String(item.text || "").trim())
        .map((item) => item.side),
    );
    const missingSides = ["正方", "反方"].filter((side) => !transcriptSides.has(side));
    if (missingSides.length) {
      $("judgement").textContent =
        `暫未能要求 AI 評判：${missingSides.join("、")}未有逐字稿。`;
      return;
    }

    judgementRequestInFlight = true;
    $("judgement").textContent = "正在提交 AI 評判要求…";
    updateButtons();
    try {
      const data = await roomApi(
        `/api/room/${encodeURIComponent(code)}/judgement`,
        { method: "POST", body: "{}" },
      );
      judgementPending = Boolean(data.judgement_pending);
      judgementRequested = Boolean(data.judgement_requested ?? true);
      canRequestJudgement = false;
      state.judge_enabled = data.judge_enabled ?? state.judge_enabled;
      state.judge_disabled_reason = data.judge_disabled_reason
        || state.judge_disabled_reason
        || "";
      $("judgement").textContent = data.judgement
        || (judgementPending ? "AI 評判分析中…可稍後重新開啟本房查看。" : "AI 評判已完成。");
      if (judgementPending) {
        startResultPolling();
      }
    } catch (error) {
      $("judgement").textContent = `未能要求 AI 評判：${error.message}`;
      if (error.status !== 409) showRoomAlert(`AI 評判要求失敗：${error.message}`);
    } finally {
      judgementRequestInFlight = false;
      updateButtons();
    }
  }

  function renderEndedUi() {
    const ended = phase === "ended";
    $("networkTest").classList.toggle("hidden", ended);
    $("rolePick").classList.toggle("hidden", ended);
    if (ended) {
      $("segLabel").textContent = "練習已完結";
      $("segSub").textContent = "逐字稿及 AI 評判結果只會短暫保留，請即時查看。";
      $("talkBtn").textContent = "房間已結束";
      $("connStatus").textContent = "房間已結束";
    }
  }

  function renderState(message) {
    if (phase === "ended") return;
    const incomingStateSequence = Number(message.state_sequence);
    if (
      Number.isFinite(incomingStateSequence)
      && incomingStateSequence < lastStateSequence
    ) return;
    if (Number.isFinite(incomingStateSequence)) {
      lastStateSequence = incomingStateSequence;
    }
    const previousPhase = phase;
    const previouslyOwnedTurn = ownedTurnId;
    const wasRtcPaused = Boolean(state.rtc_paused);
    state = message;
    phase = message.phase || phase;
    applyRemoteAudioGate();
    if (Number.isFinite(Number(message.server_now_ms))) {
      serverClockOffset = Number(message.server_now_ms) - Date.now();
    }

    const serverTurnId = String(message.active_turn_id || "");
    const serverTurnRequestId = String(message.active_turn_request_id || "");
    const requestedTurnMatches = Boolean(
      turnStarting
      && pendingTurnRequestId
      && serverTurnRequestId === pendingTurnRequestId
    );
    const mayAdoptOwnTurn = Boolean(
      message.active_turn_user === myUid
      && serverTurnId
      // A fresh/reloaded page must never open the microphone merely because
      // the server still has this account's old turn.  Only the page where the
      // user pressed Start Speaking, or the same page reconnecting that exact
      // turn, may adopt the authoritative turn id.
      && (requestedTurnMatches || previouslyOwnedTurn === serverTurnId)
    );
    if (mayAdoptOwnTurn) {
      ownedTurnId = serverTurnId;
      ownedTurnRequestId = serverTurnRequestId;
      pendingTurnRequestId = "";
      turnStarting = false;
      if (!message.rtc_paused && controlConnected) {
        setMicMode("live");
        if (!recognition && !turnEnding) startRecognition();
      }
    } else {
      if (previouslyOwnedTurn && !turnEnding) {
        setMicMode("muted");
        stopRecognitionImmediately();
      }
      if (!turnEnding) ownedTurnId = "";
      if (!turnEnding) ownedTurnRequestId = "";
      recognitionPausedForRtc = false;
      setMicMode("muted");
    }

    if (message.rtc_paused) {
      setMicMode("muted");
      if (!wasRtcPaused && previouslyOwnedTurn && !turnEnding) {
        pauseRecognitionForRtc(previouslyOwnedTurn);
      }
    } else if (wasRtcPaused && ownedTurnId && !turnEnding) {
      recognitionPausedForRtc = false;
      if (!recognition) startRecognition();
    }

    if (ownedTurnId && pendingTranscriptChunks.length) flushTranscriptChunks();
    $("segLabel").textContent = message.seg_label
      || (phase === "lobby" ? "等待開始…" : "");
    $("segSub").textContent = message.rtc_paused
      ? "P2P 中斷，計時已暫停，麥克風已靜音，正在 ICE restart…"
      : message.side === "雙方" && message.expected_turn_side
        ? `嚴格交替｜輪到${message.expected_turn_side}發言`
        : (message.side || "");
    if (message.judge_enabled === false) {
      $("judgement").textContent =
        `AI 評判已停用：${message.judge_disabled_reason || "AI 評價目前不可用"}`;
    }
    if (previousPhase !== phase) postRoomPhase();
    if (phase === "active") {
      if (message.rtc_paused && isHost && !restartAttempted) {
        restartAttempted = true;
        recoverActivePeer({ forceRestart: true });
      } else if (pc?.connectionState !== "connected") {
        reportRtcStatus("disconnected", true);
        recoverActivePeer();
      }
    }
    renderEndedUi();
    updateButtons();
  }

  function formatSeconds(value) {
    const seconds = Math.max(0, Math.floor(Number(value) || 0));
    return `${String(Math.floor(seconds / 60)).padStart(2, "0")}`
      + `:${String(seconds % 60).padStart(2, "0")}`;
  }

  function tickTimer() {
    if (phase !== "active") return;
    const now = Date.now() + serverClockOffset;
    const snapshotNow = Number(state.server_now_ms || now);
    const liveDeltaMs = state.rtc_paused || !controlConnected
      ? 0
      : Math.max(0, now - snapshotNow);
    const elapsedMs = Number(state.seg_elapsed_ms || 0) + liveDeltaMs;
    const isFree = state.side === "雙方";
    if (isFree) {
      $("sideTimers").classList.remove("hidden");
      const sideUsed = { ...(state.side_elapsed_ms || {}) };
      if (!state.rtc_paused && !state.turn_stop_pending && state.active_turn_side) {
        sideUsed[state.active_turn_side] =
          Number(sideUsed[state.active_turn_side] || 0) + liveDeltaMs;
      }
      $("proTimer").textContent = formatSeconds(
        Number(state.seconds || 0) - Number(sideUsed["正方"] || 0) / 1000,
      );
      $("conTimer").textContent = formatSeconds(
        Number(state.seconds || 0) - Number(sideUsed["反方"] || 0) / 1000,
      );
      $("timer").textContent = "雙方獨立計時";
    } else {
      $("sideTimers").classList.add("hidden");
      $("timer").textContent = formatSeconds(
        Number(state.seconds || 0) - elapsedMs / 1000,
      );
    }
  }

  function updateButtons() {
    const ended = phase === "ended";
    $("hostControls").classList.toggle("hidden", !isHost);
    const segmentIndex = Number(state.seg_index || 0);
    const segmentTotal = Number(state.seg_total || 0);
    const connectedMembers = roster.filter((item) => item.connected).length;
    $("startBtn").disabled = !controlConnected || !isHost || phase !== "lobby"
      || connectedMembers !== 2;
    $("prevBtn").disabled = !controlConnected || !isHost || phase !== "active"
      || state.rtc_paused || segmentIndex <= 0;
    $("nextBtn").disabled = !controlConnected || !isHost || phase !== "active"
      || state.rtc_paused || !segmentTotal || segmentIndex >= segmentTotal - 1;
    $("endBtn").disabled = !controlConnected || !isHost
      || !["lobby", "active"].includes(phase);

    const hasCurrentJudgement = Boolean(
      judgementRequested
      && !judgementPending
      && judgementRevision >= 0
      && judgementRevision === transcriptRevision
    );
    $("judgeBtn").disabled = !isHost
      || !ended
      || judgementRequestInFlight
      || judgementPending
      || judgementRequested
      || hasCurrentJudgement
      || state.judge_enabled === false
      || !canRequestJudgement;
    $("judgeBtn").textContent = judgementPending || judgementRequestInFlight
      ? "AI 評判分析中…"
      : judgementRequested || hasCurrentJudgement
        ? "AI 評判已要求"
        : "要求 AI 評判（一次）";

    const ownsTurn = state.active_turn_user === myUid && Boolean(ownedTurnId);
    const turnAvailable = !state.active_turn_user || ownsTurn;
    const fixedSpeakerOkay = state.active_speaker == null || state.active_speaker === myUid;
    const expectedSideOkay = !state.expected_turn_side || state.expected_turn_side === myRole;
    const speakableSegment = state.side === "雙方" || state.side === myRole;
    $("talkBtn").disabled = !controlConnected
      || phase !== "active"
      || !speakableSegment
      || !fixedSpeakerOkay
      || !expectedSideOkay
      || !turnAvailable
      || state.rtc_paused
      || state.turn_stop_pending
      || turnStarting
      || turnEnding;
    $("talkBtn").classList.toggle("live", ownsTurn && micMode === "live");
    $("talkBtn").textContent = turnEnding
      ? "正在完成逐字稿…"
      : ownsTurn
        ? "停止發言"
        : ended
          ? "房間已結束"
          : !expectedSideOkay && state.expected_turn_side
            ? `等待${state.expected_turn_side}發言`
            : "按一下開始發言";
    $("deviceTestBtn").disabled = !controlConnected
      || phase !== "lobby"
      || preflightRunning;
    $("rolePickButtons").querySelectorAll("button").forEach((button) => {
      button.disabled = !controlConnected || phase !== "lobby" || preflightRunning;
    });
    renderMicStatus();
  }

  function flushTranscriptChunks(turnId = ownedTurnId) {
    while (turnId && pendingTranscriptChunks.length) {
      const text = pendingTranscriptChunks.shift();
      if (!send({
        type: "transcript_chunk",
        turn_id: turnId,
        sequence: transcriptSequence,
        text,
      })) {
        pendingTranscriptChunks.unshift(text);
        break;
      }
      transcriptSequence += 1;
    }
  }

  function startRecognition() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      if (recognitionUnsupportedNotified) return;
      recognitionUnsupportedNotified = true;
      const message =
        "此瀏覽器不支援 Web Speech 逐字稿；P2P 發言仍可繼續，但 AI 評判可能沒有本段內容。";
      log(message);
      showRoomAlert(message);
      return;
    }
    if (state.judge_enabled === false || recognition || !authoritativeMicAllowed()) return;

    recognitionWanted = true;
    interimTranscript = "";
    const activeRecognition = new SpeechRecognition();
    recognition = activeRecognition;
    activeRecognition.lang = "zh-HK";
    activeRecognition.continuous = true;
    activeRecognition.interimResults = true;
    activeRecognition.onresult = (event) => {
      let newestInterim = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const text = String(event.results[index][0]?.transcript || "").trim();
        if (!text) continue;
        if (event.results[index].isFinal) {
          pendingTranscriptChunks.push(text);
        } else {
          newestInterim = text;
        }
      }
      interimTranscript = newestInterim;
      flushTranscriptChunks();
    };
    activeRecognition.onerror = (event) => {
      if (recognitionWanted && !["aborted", "no-speech"].includes(event.error)) {
        log(`逐字稿暫停：${event.error}`);
      }
    };
    activeRecognition.onend = () => {
      activeRecognition._resolveStop?.();
      activeRecognition._resolveStop = null;
      if (recognition === activeRecognition) recognition = null;
      if (recognitionWanted && authoritativeMicAllowed() && !turnEnding) {
        log("逐字稿服務中斷，正在重新啟動辨識。");
        clearTimeout(recognitionRestartTimer);
        recognitionRestartTimer = setTimeout(() => {
          if (!recognition && recognitionWanted && authoritativeMicAllowed()) {
            startRecognition();
          }
        }, 300);
      }
    };
    try {
      activeRecognition.start();
    } catch (error) {
      if (recognition === activeRecognition) recognition = null;
      log(`逐字稿未能開始：${error.message}`);
    }
  }

  function stopRecognitionGracefully(timeoutMs = RECOGNITION_STOP_TIMEOUT_MS) {
    if (recognitionStopPromise) return recognitionStopPromise;
    recognitionWanted = false;
    clearTimeout(recognitionRestartTimer);
    recognitionRestartTimer = 0;
    const activeRecognition = recognition;
    if (!activeRecognition) return Promise.resolve(false);

    const promise = new Promise((resolve) => {
      let settled = false;
      let timedOut = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        activeRecognition._resolveStop = null;
        if (recognition === activeRecognition) recognition = null;
        resolve(timedOut);
      };
      const timer = setTimeout(() => {
        timedOut = true;
        try { activeRecognition.abort(); } catch (_) {}
        finish();
      }, Math.max(0, Number(timeoutMs) || 0));
      activeRecognition._resolveStop = finish;
      try {
        activeRecognition.stop();
      } catch (_) {
        finish();
      }
    });
    recognitionStopPromise = promise;
    return promise.finally(() => {
      if (recognitionStopPromise === promise) recognitionStopPromise = null;
    });
  }

  function stopRecognitionImmediately() {
    recognitionWanted = false;
    clearTimeout(recognitionRestartTimer);
    recognitionRestartTimer = 0;
    const activeRecognition = recognition;
    recognition = null;
    try { activeRecognition?.abort(); } catch (_) {}
    activeRecognition?._resolveStop?.();
    if (activeRecognition) activeRecognition._resolveStop = null;
    interimTranscript = "";
  }

  async function pauseRecognitionForRtc(turnId) {
    if (recognitionPausedForRtc || !turnId) return;
    recognitionPausedForRtc = true;
    await stopRecognitionGracefully(FORCED_RECOGNITION_STOP_MAX_MS);
    if (interimTranscript) {
      pendingTranscriptChunks.push(interimTranscript);
      interimTranscript = "";
    }
    flushTranscriptChunks(turnId);
    if (
      recognitionPausedForRtc
      && authoritativeMicAllowed()
      && ownedTurnId === turnId
      && !turnEnding
    ) {
      recognitionPausedForRtc = false;
      startRecognition();
    }
  }

  async function startTalk() {
    if (turnStarting || turnEnding || !controlConnected || phase !== "active") return;
    showRoomAlert("");
    setMicMode("muted");
    turnStarting = true;
    transcriptSequence = 0;
    pendingTranscriptChunks = [];
    interimTranscript = "";
    ownedTurnId = "";
    ownedTurnRequestId = "";
    const requestId = newTurnRequestId();
    pendingTurnRequestId = requestId;
    updateButtons();
    if (!send({ type: "turn_begin", request_id: requestId })) {
      turnStarting = false;
      pendingTurnRequestId = "";
      updateButtons();
      return;
    }
    const started = await waitFor(
      () => Boolean(
        state.active_turn_user === myUid
        && state.active_turn_id
        && state.active_turn_request_id === requestId
        && ownedTurnId === state.active_turn_id
      ),
      2500,
    );
    turnStarting = false;
    if (!started) {
      send({
        type: "turn_stop_intent",
        request_id: requestId,
        reason: "start_confirmation_timeout",
      });
      pendingTurnRequestId = "";
      setMicMode("muted");
      showRoomAlert("Server 未確認輪到你發言；請確認連線及辯方後再試。");
      updateButtons();
      return;
    }
    setMicMode("live");
    startRecognition();
    updateButtons();
  }

  async function finalizeTalk({
    forced = false,
    reason = "manual",
    deadlineMs = 0,
    turnId = ownedTurnId,
    sendTurnEnd = true,
    timeoutMs,
  } = {}) {
    if (turnEnding || !turnId) return;
    turnEnding = true;
    recognitionPausedForRtc = false;
    setMicMode("muted");
    if (!forced && sendTurnEnd) {
      send({
        type: "turn_stop_intent",
        turn_id: turnId,
        request_id: ownedTurnRequestId,
        reason,
      });
    }
    updateButtons();

    let stopTimeout = timeoutMs ?? RECOGNITION_STOP_TIMEOUT_MS;
    if (forced) {
      const serverDeadline = Number(deadlineMs || 0);
      const remaining = serverDeadline
        ? serverDeadline - (Date.now() + serverClockOffset) - 120
        : FORCED_RECOGNITION_STOP_MAX_MS;
      stopTimeout = Math.max(0, Math.min(FORCED_RECOGNITION_STOP_MAX_MS, remaining));
    }
    const timedOut = await stopRecognitionGracefully(stopTimeout);
    if (interimTranscript) {
      pendingTranscriptChunks.push(interimTranscript);
      interimTranscript = "";
    }
    flushTranscriptChunks(turnId);
    if (transcriptSequence > 0) {
      send({
        type: "transcript_commit",
        turn_id: turnId,
        final_sequence: transcriptSequence,
        partial: Boolean(forced || timedOut),
        reason,
      });
    }
    if (sendTurnEnd) send({ type: "turn_end", turn_id: turnId });
    ownedTurnId = "";
    ownedTurnRequestId = "";
    turnEnding = false;
    updateButtons();
  }

  function onBell(message) {
    if (
      Number(message.segment_generation || 0)
      !== Number(state.segment_generation || 0)
    ) return;
    const key = [
      Number(message.segment_generation || 0),
      Number(message.seg_index || 0),
      String(message.side || ""),
      Number(message.bell_index || 0),
    ].join(":");
    if (seenBellEvents.has(key)) return;
    seenBellEvents.add(key);
    if (seenBellEvents.size > 200) {
      const oldest = seenBellEvents.values().next().value;
      seenBellEvents.delete(oldest);
    }
    ring(message.rings);
  }

  function handleEnded(message) {
    phase = "ended";
    controlConnected = false;
    applyRemoteAudioGate();
    clearTimeout(reconnectTimer);
    clearTimeout(reconnectStableTimer);
    clearInterval(heartbeatTimer);
    reconnectTimer = 0;
    reconnectStableTimer = 0;
    heartbeatTimer = 0;
    setMicMode("muted");
    stopRecognitionImmediately();
    resetPeer({ keepLocalStream: false });
    log(`房間已結束${message.reason ? `（${message.reason}）` : ""}。`);
    postRoomPhase();
    renderEndedUi();
    updateButtons();
    loadEndedSnapshot();
  }

  function onMessage(message) {
    switch (message.type) {
      case "roster":
        if (typeof message.leave_token === "string" && message.leave_token) {
          leaveToken = message.leave_token;
        }
        if (message.topic) {
          $("topicLine").textContent = `${message.debate_format || ""}｜${message.topic}`;
        }
        if (Array.isArray(message.transcript)) {
          replaceTranscriptSnapshot(message.transcript);
        }
        if (message.judgement) $("judgement").textContent = message.judgement;
        state.judge_enabled = message.judge_enabled ?? state.judge_enabled;
        state.judge_disabled_reason = message.judge_disabled_reason
          || state.judge_disabled_reason
          || "";
        renderTranscript();
        clearTimeout(reconnectStableTimer);
        {
          const stableEpoch = socketEpoch;
          reconnectStableTimer = setTimeout(() => {
            if (
              socketEpoch === stableEpoch
              && controlConnected
              && ws?.readyState === WebSocket.OPEN
            ) reconnectAttempt = 0;
          }, 15000);
        }
        renderRoster(message);
        break;
      case "state":
        renderState(message);
        break;
      case "rtc_offer":
      case "rtc_answer":
      case "rtc_ice":
        signalQueue = signalQueue
          .then(() => onSignal(message))
          .catch((error) => log(`RTC signaling：${error.message}`));
        break;
      case "rtc_status":
        if (
          message.status === "preflight_ready"
          && Number(message.roster_generation) === rosterGeneration
          && message.from
          && message.from !== myUid
        ) {
          readyPeers.add(message.from);
          if (isHost && localRtcReady) {
            makeOffer().catch((error) => log(`RTC offer：${error.message}`));
          }
        } else if (
          message.status === "restart"
          && Number(message.roster_generation) === rosterGeneration
          && isHost
        ) {
          restartAttempted = true;
          recoverActivePeer({ forceRestart: true });
        }
        break;
      case "precheck_request":
        activeCheckId = String(message.check_id || "");
        renderPrecheck(message);
        submitPrecheck(activeCheckId);
        break;
      case "precheck_status":
        if (message.check_id) {
          activeCheckId = String(message.check_id);
        } else if (activeCheckId) {
          activeCheckId = "";
          localTest = null;
          localRtcReady = false;
          setMicMode("muted");
        }
        renderPrecheck(message);
        break;
      case "precheck_failed":
        renderPrecheck(message, { failed: true });
        log("開始前 P2P media 測試未全部通過。");
        break;
      case "speaking":
        if (!message.speaking && message.user_id === myUid && !turnEnding) {
          setMicMode("muted");
          stopRecognitionImmediately();
          ownedTurnId = "";
          ownedTurnRequestId = "";
        } else if (!message.speaking && message.user_id !== myUid && remoteAudio) {
          remoteAudio.muted = true;
        }
        break;
      case "turn_stop_requested":
        if (
          (message.user_id === myUid || state.active_turn_user === myUid)
          && String(message.turn_id || "") === ownedTurnId
        ) {
          finalizeTalk({
            forced: true,
            reason: message.reason || "server_transition",
            deadlineMs: message.deadline_ms,
            turnId: ownedTurnId,
            sendTurnEnd: true,
          }).catch((error) => {
            turnEnding = false;
            setMicMode("muted");
            showRoomAlert(`未能完成逐字稿：${error.message}`);
            updateButtons();
          });
        }
        break;
      case "turn_rejected":
        turnStarting = false;
        pendingTurnRequestId = "";
        ownedTurnId = "";
        ownedTurnRequestId = "";
        setMicMode("muted");
        showRoomAlert(message.message || "暫時未能開始發言。");
        updateButtons();
        break;
      case "transcript":
        if (message.item) mergeTranscriptItem(message.item);
        renderTranscript();
        break;
      case "judge_disabled":
        state.judge_enabled = false;
        state.judge_disabled_reason = message.reason || "AI 評價目前不可用";
        $("judgement").textContent = `AI 評判已停用：${state.judge_disabled_reason}`;
        updateButtons();
        break;
      case "judgement_pending":
        judgementPending = true;
        judgementRequested = true;
        $("judgement").textContent = "AI 評判分析中…";
        updateButtons();
        break;
      case "judgement":
        judgementPending = false;
        judgementRequested = true;
        $("judgement").textContent = message.text || "AI 評判已完成。";
        updateButtons();
        break;
      case "bell":
        onBell(message);
        break;
      case "peer_left":
        if (
          (Number.isFinite(Number(message.roster_generation))
            && Number(message.roster_generation) !== rosterGeneration)
          || roster.some(
            (item) => item.user_id === message.user_id && item.connected,
          )
        ) break;
        peerLeftUser = String(message.user_id || "");
        setMicMode("muted");
        if (remoteAudio) remoteAudio.muted = true;
        showRoomAlert("對方已離開房間；計時及麥克風已暫停。");
        break;
      case "ended":
        handleEnded(message);
        break;
      case "error":
        lastServerError = message.message || "房間錯誤";
        turnStarting = false;
        pendingTurnRequestId = "";
        setMicMode("muted");
        log(lastServerError);
        showRoomAlert(lastServerError);
        updateButtons();
        break;
    }
  }

  function permanentClose(event) {
    const reason = String(event.reason || "").toLowerCase();
    return event.code === 1008
      || event.code === 1013
      || event.code === 1007
      || event.code === 1009
      || reason.includes("connection replaced")
      || reason.includes("member left room");
  }

  function scheduleReconnect() {
    if (terminalConnection || phase === "ended" || reconnectTimer) return;
    if (reconnectAttempt >= RECONNECT_DELAYS_MS.length) {
      const message = "控制連線多次重試仍未恢復，請重新加入房間。";
      $("connStatus").textContent = message;
      showRoomAlert(message);
      setMicMode("muted");
      resetPeer({ keepLocalStream: false });
      postRoomTerminal(message, 0, true);
      return;
    }
    const delay = RECONNECT_DELAYS_MS[reconnectAttempt];
    reconnectAttempt += 1;
    $("connStatus").textContent =
      `控制連線中斷，${Math.ceil(delay / 1000)} 秒後重試…`;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = 0;
      connect();
    }, delay);
  }

  function connect() {
    if (
      terminalConnection
      || phase === "ended"
      || ws?.readyState === WebSocket.OPEN
      || ws?.readyState === WebSocket.CONNECTING
    ) return;
    const epoch = ++socketEpoch;
    let socket;
    try {
      socket = new WebSocket(wsUrl());
    } catch (error) {
      log(`控制連線建立失敗：${error.message}`);
      scheduleReconnect();
      return;
    }
    ws = socket;
    socket.onopen = () => {
      if (ws !== socket || epoch !== socketEpoch) return;
      controlConnected = true;
      lastServerError = "";
      lastReportedRtc = "";
      $("connStatus").textContent = pc?.connectionState === "connected"
        ? "控制連線及 P2P 已建立"
        : "控制連線已建立；等待 P2P";
      showRoomAlert("");
      applyRemoteAudioGate();
      if (phase === "active") {
        if (pc?.connectionState === "connected") {
          reportRtcStatus("connected", true);
        } else {
          setMicMode("muted");
          reportRtcStatus("disconnected", true);
          recoverActivePeer();
        }
      }
      updateButtons();
    };
    socket.onmessage = (event) => {
      if (ws !== socket || epoch !== socketEpoch) return;
      try {
        onMessage(JSON.parse(event.data));
      } catch (error) {
        log(`控制訊息格式錯誤：${error.message}`);
      }
    };
    socket.onclose = (event) => {
      if (ws !== socket || epoch !== socketEpoch) return;
      ws = undefined;
      controlConnected = false;
      applyRemoteAudioGate();
      clearTimeout(reconnectStableTimer);
      reconnectStableTimer = 0;
      turnStarting = false;
      setMicMode("muted");
      stopRecognitionImmediately();
      updateButtons();
      if (phase === "ended") {
        $("connStatus").textContent = "房間已結束";
        return;
      }
      if (String(event.reason || "").toLowerCase().includes("practice ended")) {
        handleEnded({ reason: "practice ended" });
        return;
      }
      if (permanentClose(event)) {
        const replaced = String(event.reason || "").toLowerCase()
          .includes("connection replaced");
        const message = lastServerError || (replaced
          ? "此帳戶已在另一分頁或裝置開啟同一房間。"
          : "房間拒絕連線、已滿、已結束或你已離開。"
        );
        $("connStatus").textContent = message;
        showRoomAlert(message);
        resetPeer({ keepLocalStream: false });
        postRoomTerminal(message, event.code);
        return;
      }
      scheduleReconnect();
    };
    socket.onerror = () => {
      if (ws === socket && epoch === socketEpoch) {
        $("connStatus").textContent = "控制連線發生錯誤，正在等候重試…";
      }
    };
  }

  async function initializeRoom() {
    try {
      const result = await fetchRoomResult();
      applyRoomResult(result);
      if (result.phase === "ended") {
        if (judgementPending) {
          startResultPolling();
        }
        return;
      }
      if (result.phase === "ending") {
        postRoomReady();
        endedSnapshotTimer = setTimeout(() => waitForEndingSnapshot(), 500);
        return;
      }
    } catch (error) {
      if ([401, 404].includes(error.status)) {
        const message = error.status === 401 ? "登入已失效，請重新登入。" : error.message;
        showRoomAlert(message);
        postRoomTerminal(message, 1008);
        return;
      }
      // A new member cannot read member-only results until WebSocket admission.
      if (error.status !== 403) log(`未能預先載入房間資料：${error.message}`);
    }
    connect();
  }

  function teardownLocalRoom() {
    terminalConnection = true;
    controlConnected = false;
    applyRemoteAudioGate();
    clearTimeout(reconnectTimer);
    clearTimeout(reconnectStableTimer);
    clearTimeout(endedSnapshotTimer);
    clearTimeout(restartTimer);
    clearInterval(heartbeatTimer);
    stopResultPolling();
    reconnectTimer = 0;
    reconnectStableTimer = 0;
    endedSnapshotTimer = 0;
    restartTimer = 0;
    heartbeatTimer = 0;
    setMicMode("muted");
    stopRecognitionImmediately();
    resetPeer({ keepLocalStream: false });
    const socket = ws;
    ws = undefined;
    socketEpoch += 1;
    try { socket?.close(1000, "local leave"); } catch (_) {}
  }

  function launchLeaveRequest() {
    if (!leaveToken) return Promise.resolve();
    const token = leaveToken;
    leaveToken = "";
    return fetch(`/api/room/${encodeURIComponent(code)}/leave`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ leave_token: token }),
      keepalive: true,
    }).catch(() => {});
  }

  function configureEmbedding() {
    const embedded = window.parent !== window;
    document.documentElement.classList.toggle("embedded", embedded);
    const back = $("roomBack");
    const source = new URLSearchParams(location.search).get("from");
    back.href = source === "practice" ? "/practice/ai-debate" : "/ai-coach";
    back.textContent = source === "practice"
      ? "離開房間並返回練習中心"
      : "離開房間並返回 AI Coach";
    back.target = "_top";
  }

  configureEmbedding();
  $("codeText").textContent = code;
  $("deviceTestBtn").onclick = runDeviceTest;
  $("startBtn").onclick = () => send({ type: "start" });
  $("nextBtn").onclick = () => send({ type: "next_segment" });
  $("prevBtn").onclick = () => send({
    type: "set_segment",
    index: Math.max(0, Number(state.seg_index || 0) - 1),
  });
  $("judgeBtn").onclick = requestJudgement;
  $("endBtn").onclick = () => {
    if (confirm("結束整個練習房間？")) send({ type: "end" });
  };
  $("talkBtn").onclick = () => {
    if (state.active_turn_user === myUid && ownedTurnId) {
      finalizeTalk({ forced: false, reason: "manual" });
    } else {
      startTalk();
    }
  };
  $("roomBack").addEventListener("click", (event) => {
    setMicMode("muted");
    if (phase === "ended") return;
    event.preventDefault();
    const destination = $("roomBack").href;
    launchLeaveRequest();
    teardownLocalRoom();
    location.assign(destination);
  });
  window.addEventListener("message", (event) => {
    if (
      event.origin === location.origin
      && event.source === window.parent
      && event.data?.channel === PARENT_CHANNEL
      && event.data?.type === "parent_leave"
      && String(event.data?.code || "").toUpperCase() === code
    ) {
      if (phase !== "ended") launchLeaveRequest();
      teardownLocalRoom();
    }
  });
  window.addEventListener("pagehide", teardownLocalRoom, { once: true });

  heartbeatTimer = setInterval(() => {
    tickTimer();
    send({ type: "heartbeat" });
  }, 1000);
  renderMicStatus();
  updateButtons();
  initializeRoom();
})();
