"""Live page render contracts, each one a shipped bug:

1. The「自由辯論→Mock」UI rebrand once rewrote the injected system prompt,
   runtime prompts and segment labels (AI received corrupted instructions).
2. Solo Live must connect browser-to-Google and keep Mock hand-offs JIT, rather
   than exposing a batch of tokens or routing audio through Render.
"""

import json
import shutil
import subprocess

import pytest

import deploy.proxy as proxy
from debate_timing import get_full_mock_sequence, split_mock_into_sessions
from prompts import LIVE_RUNTIME_PROMPTS, build_full_mock_live_prompt


def _mock_payload():
    prompt = build_full_mock_live_prompt("測試辯題", "正方", "聯中", free_debate_minutes=5)
    segments = get_full_mock_sequence("聯中", free_debate_minutes=5)
    sessions = split_mock_into_sessions(segments)
    flat = [
        {**segment, "session": index}
        for index, session in enumerate(sessions)
        for segment in session["segments"]
    ]
    return prompt, flat, [session["label"] for session in sessions]


def _render_mock(tokens):
    prompt, flat, labels = _mock_payload()
    html = proxy._render_live_debate_html(
        tokens[0] if tokens else "", prompt, 60, [], False, segments=flat, tokens=tokens,
        session_labels=labels, session_label="Mock", practice_id="practice-claim",
    )
    return prompt, flat, html


def test_mock_rebrand_never_rewrites_injected_payloads():
    prompt, flat, html = _render_mock(["tok-1", "tok-2"])
    assert json.dumps(prompt, ensure_ascii=False) in html
    assert json.dumps(flat, ensure_ascii=False) in html
    assert json.dumps(LIVE_RUNTIME_PROMPTS, ensure_ascii=False) in html
    assert "開始Mock" in html
    assert "開始自由辯論" not in html


def test_free_debate_render_keeps_original_copy_and_prompt():
    html = proxy._render_live_debate_html("tok-f", "prompt 自由辯論", 2.5, [], True)
    assert "開始自由辯論" in html
    assert json.dumps("prompt 自由辯論", ensure_ascii=False) in html


def test_user_prompt_cannot_escape_the_inline_live_script():
    prompt = (
        "辯題 </script><script>globalThis.PWNED=1</script> & \u2028 \u2029 "
        "__LIVE_PROMPTS__ __AI_STARTS__ __MOCK_SEGMENTS__"
    )
    html = proxy._render_live_debate_html("tok-f", prompt, 2.5, [], True)
    literal = html.split("const SYSTEM_PROMPT = ", 1)[1].split(";", 1)[0]

    assert json.loads(literal) == prompt
    assert "</script><script>globalThis.PWNED=1</script>" not in html
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in literal
    assert "\\u0026" in literal
    assert "\\u2028" in literal and "\\u2029" in literal


def test_solo_live_uses_direct_google_websocket_and_official_session_fields():
    _, _, html = _render_mock(["tok-1"])
    assert "generativelanguage.googleapis.com/ws/" in html
    assert '"?access_token=" + encodeURIComponent(token)' in html
    assert "RELAY_WS_BASE" not in html
    assert "TOKEN_SIGS" not in html
    assert "contextWindowCompression" in html
    assert "triggerTokens: CONTEXT_TRIGGER_TOKENS" in html
    assert "slidingWindow: { targetTokens: CONTEXT_TARGET_TOKENS }" in html
    assert "sessionResumption: resumptionHandle" in html
    assert "msg.sessionResumptionUpdate" in html
    assert "resumptionUpdate.newHandle" in html
    assert "msg.goAway.timeLeft" in html
    assert 'Object.prototype.hasOwnProperty.call(\n            msg,\n            "setupComplete"' in html


def test_initial_and_mock_handoff_tokens_are_minted_just_in_time():
    _, _, html = _render_mock(["tok-1", "tok-2"])
    assert 'const LIVE_PRACTICE_ID = "practice-claim"' in html
    assert 'const LIVE_TOKEN_URL = "/api/ai-coach/live-token"' in html
    assert "tok-1" not in html and "tok-2" not in html
    assert "await getLiveSessionToken(0, startEpoch)" in html
    assert "await getLiveSessionToken(sessionIdx, handoffEpoch)" in html
    assert "practice_id: LIVE_PRACTICE_ID" in html
    assert "session_index: sessionIdx" in html
    setup_prompt_function = html.split("function buildSystemInstruction()", 1)[1].split(
        "async function prepareMicAfterSetup", 1
    )[0]
    assert "return SYSTEM_PROMPT" in setup_prompt_function
    assert "sessionContextNote" not in setup_prompt_function
    assert '"以下係上一節逐字摘要。請延續同一場辯論' in html


def test_free_live_tracks_two_audio_budgets_and_a_separate_hard_deadline():
    html = proxy._render_live_debate_html(
        "tok-f", "prompt", 10, [], False, session_max_seconds=1800,
    )
    assert "你方已用／每邊上限" in html
    assert "AI 已用" in html
    assert "整節尚餘" in html
    assert "decodedBase64ByteLength(base64) / (sampleRate * channels * 2)" in html
    assert "const nextElapsed = aiSpeechElapsed + duration" in html
    assert "if (nextElapsed >= SPEECH_SECONDS)" in html
    assert "aiSpeechElapsed = SPEECH_SECONDS" in html
    assert "!accountAiPcmAudio(" in html
    assert "getUserSpeechElapsed() >= SPEECH_SECONDS" in html
    assert "const FREE_SESSION_MAX_SECONDS = Number(1800)" in html
    hard_deadline = html.split("function startFreeHardDeadline()", 1)[1].split(
        "function fireBellOnce", 1
    )[0]
    assert "if (MOCK_MODE) return" not in hard_deadline
    assert "if (!overallStartedAt)" in hard_deadline
    assert "overallStartedAt = Date.now()" in hard_deadline
    assert "if (hardDeadlineTimer || hardDeadlineReached) return" in hard_deadline
    assert "Date.now() - overallStartedAt" in hard_deadline
    assert "setTimeout(expireFreeHardDeadline, remainingMs)" in hard_deadline
    start_live = html.split("async function startLive()", 1)[1].split(
        "function currentToken", 1
    )[0]
    assert start_live.index("startFreeHardDeadline()") < start_live.index("openSessionWs()")
    assert start_live.index("getLiveSessionToken(0, startEpoch)") < start_live.index("openSessionWs()")
    clear_timers = html.split("function clearTimers(", 1)[1].split(
        "function startWaitTimer", 1
    )[0]
    assert "clearTimeout(hardDeadlineTimer)" in clear_timers


def test_solo_pcm_hard_boundaries_are_enforced_before_send_or_playback():
    html = proxy._render_live_debate_html(
        "tok-f", "prompt", 10, [], False, session_max_seconds=1800,
    )
    user_bound = html.split("function boundUserPcmFrame", 1)[1].split(
        "function bytesToBase64", 1
    )[0]
    mic_callback = html.split("nextProcessor.onaudioprocess", 1)[1].split(
        "nextSource.connect(nextProcessor)", 1
    )[0]
    model_parts = html.split("function processAiModelTurnParts", 1)[1].split(
        "function playPcm", 1
    )[0]
    socket_model_turn = html.split(
        "if (sc.modelTurn && sc.modelTurn.parts)", 1
    )[1].split("if (sc.generationComplete", 1)[0]

    assert "USER_PCM_SAMPLE_LIMIT - userPcmSamplesSent" in user_bound
    assert "SPEECH_SECONDS - getUserSpeechElapsed()" in user_bound
    assert "Math.min(" in user_bound and "wallRemaining" in user_bound
    assert "pcm.subarray(0, acceptedLength)" in user_bound
    assert "userPcmSamplesSent += acceptedLength" in user_bound
    assert mic_callback.index("boundUserPcmFrame(pcm)") < mic_callback.index(
        "ws.send("
    )
    assert "boundedPcm.pcm.byteOffset" in mic_callback
    assert "boundedPcm.pcm.byteLength" in mic_callback
    assert "if (boundedPcm.budgetReached)" in mic_callback
    assert "userSpeechElapsed = SPEECH_SECONDS" in mic_callback
    assert html.count("userPcmSamplesSent = 0") >= 3

    assert "for (const part of parts)" in model_parts
    assert model_parts.index("!accountAiPcmAudio(") < model_parts.index("break;")
    assert model_parts.index("break;") < model_parts.index(
        "nativeAudioParts.push("
    )
    assert "processAiModelTurnParts(sc.modelTurn.parts)" in socket_model_turn
    assert ".forEach(" not in socket_model_turn


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_solo_pcm_boundary_helpers_execute_fail_closed_offline():
    html = proxy._render_live_debate_html(
        "tok-f", "prompt", 10, [], False, session_max_seconds=1800,
    )
    user_bound = "function boundUserPcmFrame" + html.split(
        "function boundUserPcmFrame", 1
    )[1].split("function bytesToBase64", 1)[0]
    model_parts = "function processAiModelTurnParts" + html.split(
        "function processAiModelTurnParts", 1
    )[1].split("function playPcm", 1)[0]
    script = f"""
const MOCK_MODE = false;
const SPEECH_SECONDS = 600;
const USER_PCM_SAMPLE_RATE = 16000;
const USER_PCM_SAMPLE_LIMIT = SPEECH_SECONDS * USER_PCM_SAMPLE_RATE;
let userPcmSamplesSent = USER_PCM_SAMPLE_LIMIT - 100;
let userElapsed = 0;
function getUserSpeechElapsed() {{ return userElapsed; }}
{user_bound}

let result = boundUserPcmFrame(new Int16Array(2048));
if (result.pcm.length !== 100 || !result.budgetReached)
  throw new Error("sample ledger admitted a boundary-crossing mic frame");
result = boundUserPcmFrame(new Int16Array(2048));
if (result.pcm.length !== 0 || !result.budgetReached)
  throw new Error("exhausted mic budget admitted more PCM");

userPcmSamplesSent = 0;
userElapsed = 599.99;
result = boundUserPcmFrame(new Int16Array(2048));
if (!(result.pcm.length > 0 && result.pcm.length <= 160) || !result.budgetReached)
  throw new Error("wall-clock remainder did not trim the last mic frame");

let azureTtsEnabled = true;
let azureTtsSessionDisabled = false;
let ttsFallbackActive = false;
let nativeAudioParts = [];
let played = [];
let appended = [];
let accountCalls = 0;
const aiLine = {{}};
function accountAiPcmAudio() {{ accountCalls += 1; return accountCalls === 1; }}
function playPcm(data) {{ played.push(data); }}
function appendLineText(_line, text) {{ appended.push(text); }}
{model_parts}

const parts = [
  {{ inlineData: {{ data: "accepted", mimeType: "audio/pcm" }}, text: "first" }},
  {{ inlineData: {{ data: "crossing", mimeType: "audio/pcm" }}, text: "crossing" }},
  {{ inlineData: {{ data: "later", mimeType: "audio/pcm" }}, text: "later" }},
];
processAiModelTurnParts(parts);
if (accountCalls !== 2 || nativeAudioParts.length !== 1 || played.length !== 0)
  throw new Error("AI PCM queue continued after the crossing part");
if (appended.join(",") !== "first")
  throw new Error("later model-turn parts continued after the PCM boundary");

azureTtsEnabled = false;
nativeAudioParts = [];
played = [];
appended = [];
accountCalls = 0;
processAiModelTurnParts(parts);
if (accountCalls !== 2 || nativeAudioParts.length !== 0 || played.join(",") !== "accepted")
  throw new Error("native playback continued after the crossing part");
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_every_websocket_attempt_has_a_stale_safe_setup_watchdog():
    _, _, html = _render_mock(["tok-1"])
    assert "const SETUP_COMPLETE_TIMEOUT_MS = 15000" in html
    watchdog = html.split("function startSetupWatchdog", 1)[1].split(
        "function failLiveConnection", 1
    )[0]
    assert "expectedSessionEpoch !== sessionEpoch" in watchdog
    assert "SETUP_COMPLETE_TIMEOUT_MS" in watchdog
    assert "未收到 Google 完成設定確認" in watchdog
    assert "scheduleSessionReconnect" in watchdog
    open_ws = html.split("function openSessionWs()", 1)[1].split(
        "function formatCloseMessage", 1
    )[0]
    assert "startSetupWatchdog(openedSessionEpoch)" in open_ws
    assert "clearSetupWatchdog(openedSessionEpoch)" in open_ws


def test_used_single_use_token_returns_to_setup_instead_of_reloading():
    html = proxy._render_live_debate_html("tok-f", "prompt", 10, [], False)
    repeat_start = html.split('startBtn.addEventListener("click"', 1)[1].split(
        'stopBtn.addEventListener("click"', 1
    )[0]
    assert "location.reload" not in repeat_start
    assert "location.assign" in repeat_start
    assert '"/ai-coach"' in repeat_start
    assert '"/practice/ai-debate"' in repeat_start


def test_token_fetch_is_abortable_timeout_bounded_and_epoch_safe():
    _, _, html = _render_mock(["must-not-render"])
    fetcher = html.split("async function getLiveSessionToken", 1)[1].split(
        "function mockSideClass", 1
    )[0]
    assert "const controller = new AbortController()" in fetcher
    assert "TOKEN_FETCH_TIMEOUT_MS" in fetcher
    assert "signal: controller.signal" in fetcher
    assert "expectedSessionEpoch !== sessionEpoch" in fetcher
    assert fetcher.count("expectedSessionEpoch !== sessionEpoch") >= 3
    cleanup = html.split("function cleanupMedia", 1)[1].split(
        "function stopLive", 1
    )[0]
    assert "tokenFetchController.abort()" in cleanup
    handoff = html.split("async function switchToSession", 1)[1].split(
        "function onSessionReconnected", 1
    )[0]
    assert "const handoffEpoch = sessionEpoch" in handoff
    assert "handoffEpoch !== sessionEpoch" in handoff
    assert "const handoffGeneration = ++tokenHandoffGeneration" in handoff
    assert "handoffGeneration === tokenHandoffGeneration" in handoff
    assert "mockElapsed = getMockElapsed()" in handoff
    assert handoff.count("recoverMockAfterAbortedHandoff(") >= 3
    assert "handoffEpoch === sessionEpoch" not in handoff.split("finally", 1)[1]
    assert "SESSION_HANDOFF_CLOSE_TIMEOUT_MS" in handoff
    assert "handoffGeneration !== tokenHandoffGeneration" in handoff
    assert "switchingSession = false" in handoff
    assert "openSessionWs()" in handoff


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_stale_mock_handoff_recovery_waits_for_reconnect_then_restores_offline():
    _, _, html = _render_mock([])
    recovery = "function recoverMockAfterAbortedHandoff" + html.split(
        "function recoverMockAfterAbortedHandoff", 1
    )[1].split("async function switchToSession", 1)[0]
    resumed = html.split("function onConnectionResumed", 1)[1].split(
        "function openSessionWs", 1
    )[0]
    assert "pendingTokenHandoffRecovery" in resumed
    assert "recoverMockAfterAbortedHandoff(" in resumed

    script = """
const WebSocket = { OPEN: 1 };
let tokenHandoffGeneration = 9;
let pendingTokenHandoffRecovery = null;
let manualStopping = false;
let hardDeadlineReached = false;
let micReady = true;
let pendingSessionHandoff = false;
let feedbackPromptShown = false;
let finalFeedbackRequested = false;
let reconnectingSession = true;
let ws = { readyState: WebSocket.OPEN };
let mockIndex = 2;
const mockPrevBtn = { disabled: true };
const mockToggleBtn = { disabled: true };
const mockNextBtn = { disabled: true };
let resumeCalls = 0;
function canMockPrev() { return true; }
function mockResume() { resumeCalls += 1; }
""" + recovery + """

recoverMockAfterAbortedHandoff(9, true);
if (!pendingTokenHandoffRecovery || resumeCalls !== 0)
  throw new Error("handoff recovery did not wait for the reconnect");
if (!mockNextBtn.disabled || !mockToggleBtn.disabled)
  throw new Error("handoff recovery unlocked controls before setup completed");

reconnectingSession = false;
recoverMockAfterAbortedHandoff(
  pendingTokenHandoffRecovery.generation,
  pendingTokenHandoffRecovery.resumeTimer,
);
if (pendingTokenHandoffRecovery !== null || resumeCalls !== 1)
  throw new Error("handoff recovery did not resume the preserved timer");
if (mockPrevBtn.disabled || mockToggleBtn.disabled || mockNextBtn.disabled)
  throw new Error("handoff recovery did not restore Mock controls");

mockNextBtn.disabled = true;
recoverMockAfterAbortedHandoff(8, true);
if (!mockNextBtn.disabled || resumeCalls !== 1)
  throw new Error("a stale handoff generation changed the current lifecycle");
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_goaway_timer_is_epoch_bound_and_cannot_close_replacement_socket_offline():
    _, _, html = _render_mock([])
    goaway = "function clearGoAwayReconnectTimer" + html.split(
        "function clearGoAwayReconnectTimer", 1
    )[1].split("function onConnectionResumed", 1)[0]
    open_socket = html.split("function openSessionWs()", 1)[1].split(
        "function formatCloseMessage", 1
    )[0]
    resumed = html.split("function onConnectionResumed", 1)[1].split(
        "function openSessionWs", 1
    )[0]

    assert "const goAwaySessionEpoch = sessionEpoch" in goaway
    assert "goAwayReconnectTimer !== scheduledTimer" in goaway
    assert "goAwaySessionEpoch !== sessionEpoch" in goaway
    assert open_socket.lstrip().startswith("{\n        clearGoAwayReconnectTimer();")
    assert "ws.onclose = (ev) => {\n          if (openedSessionEpoch !== sessionEpoch) return;\n          clearGoAwayReconnectTimer();" in open_socket
    assert "clearGoAwayReconnectTimer();" in resumed

    script = """
const WebSocket = { CONNECTING: 0, OPEN: 1 };
let sessionEpoch = 7;
let goAwayReconnectTimer = null;
let manualStopping = false;
let pendingSessionHandoff = false;
let reconnectingSession = false;
let nextTimer = 1;
const timers = new Map();
let closeCalls = 0;
let reconnectCalls = 0;
let failed = false;
let ws = {
  readyState: WebSocket.OPEN,
  close() { closeCalls += 1; },
};
function setTimeout(callback) {
  const id = nextTimer++;
  timers.set(id, callback);
  return id;
}
function clearTimeout(id) { timers.delete(id); }
function googleDurationMs() { return 5000; }
function setState() {}
function setSubstatus() {}
function currentResumptionHandle() { return "resume-handle"; }
function failLiveConnection() { failed = true; }
function scheduleSessionReconnect() { reconnectCalls += 1; }
""" + goaway + """

scheduleGoAwayReconnect("5s");
const staleId = goAwayReconnectTimer;
const staleCallback = timers.get(staleId);
clearGoAwayReconnectTimer();
scheduleGoAwayReconnect("5s");
const replacementId = goAwayReconnectTimer;
staleCallback();
if (goAwayReconnectTimer !== replacementId || closeCalls !== 0)
  throw new Error("queued stale GoAway callback affected its replacement");

const replacementCallback = timers.get(replacementId);
sessionEpoch += 1;
replacementCallback();
if (closeCalls !== 0 || reconnectCalls !== 0 || failed)
  throw new Error("old-epoch GoAway callback affected the new socket");

scheduleGoAwayReconnect("5s");
timers.get(goAwayReconnectTimer)();
if (closeCalls !== 1 || reconnectCalls !== 0 || failed)
  throw new Error("current GoAway callback did not close exactly its own socket");
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_mock_both_sides_uses_two_time_banks_and_shifted_bells():
    _, _, html = _render_mock([])
    duration = html.split("function mockSegmentDuration", 1)[1].split(
        "function renderMockBells", 1
    )[0]
    assert 'seg.side === "雙方" ? perSide * 2 : perSide' in duration
    assert 'seg.side === "雙方" && originalTime > 0' in duration
    assert "originalTime + perSide" in duration
    assert "本段總計" in html and "（每邊 " in html
    timer = html.split("function updateMockTimer", 1)[1].split(
        "function mockResume", 1
    )[0]
    assert "const cap = mockSegmentDuration(seg)" in timer
    assert "mockBellEvents(seg).forEach" in timer


def test_mock_previous_stays_in_google_session_and_reannounces_role():
    _, _, html = _render_mock([])
    previous = html.split("function mockPrev()", 1)[1].split(
        "function startMockSequence", 1
    )[0]
    assert "canMockPrev(mockIndex)" in previous
    assert "不能跨節返回" in previous
    assert "announceSegmentToAi(MOCK_SEGMENTS[targetIndex])" in previous
    assert 'String(key).startsWith(prefix)' in html
    apply_segment = html.split("function applyMockSegment", 1)[1].split(
        "function mockNext", 1
    )[0]
    assert apply_segment.index("clearMockSegmentBellKeys(targetIndex)") < apply_segment.index(
        "updateMockTimer()"
    )
    assert 'fireBellOnce("mock-" + mockIndex + "-" + i' in html


def test_resumption_revocation_and_audio_interrupt_are_terminal_for_old_state():
    _, _, html = _render_mock([])
    socket = html.split("function openSessionWs()", 1)[1].split(
        "function formatCloseMessage", 1
    )[0]
    assert "resumptionUpdate.resumable === false" in socket
    assert 'sessionResumptionHandles[openedSessionIdx] = ""' in socket
    assert "if (sc.interrupted) resetAiAudioForTurn(true)" in socket
    assert "nativePcmSources.add(src)" in html
    assert "source.stop()" in html


def test_tts_429_disables_session_and_preserves_safe_native_remainder():
    _, _, html = _render_mock([])
    assert "resp.status === 429" in html
    fallback = html.split("function activateNativeTtsFallback", 1)[1].split(
        "async function processTtsQueue", 1
    )[0]
    assert "const safeNativeRemainderStart = nativeFallbackBoundary" in fallback
    assert "playNativeAudioParts(" in fallback
    assert "safeNativeRemainderStart" in fallback
    assert "只會播放可安全分辨的餘下 Gemini 聲音" in fallback
    assert "nativeAudioParts = []" not in fallback
    assert "ttsPlayedAnyAudio = true" in html
    assert "nativeFallbackBoundary = nativeAudioParts.length" in html


def test_hung_response_retry_interrupts_immediately():
    _, _, html = _render_mock([])
    retry = html.split("function retryCurrentResponseNow", 1)[1].split(
        "function cleanupMedia", 1
    )[0]
    assert 'ws.close(4002, "retry current response")' in retry
    assert retry.index("discardServerResponseUntilTurnSent = true") < retry.index(
        'ws.close(4002, "retry current response")'
    )
    assert "activityStart" in retry and "activityEnd" in retry
    assert "finishRetryAfterResponse()" in retry
    click = html.split('retryBtn.addEventListener("click"', 1)[1].split(
        'talkBtn.addEventListener("click"', 1
    )[0]
    assert "retryCurrentResponseNow()" in click
    assert "startWaitTimer()" not in click


def test_initial_token_retry_preserves_original_hard_deadline():
    html = proxy._render_live_debate_html("", "prompt", 10, [], False)
    cleanup = html.split("function cleanupMedia", 1)[1].split(
        "function stopLive", 1
    )[0]
    assert "const keepHardDeadline = Boolean(preserveHardDeadline)" in cleanup
    assert "clearTimers(keepHardDeadline)" in cleanup
    assert "if (!keepHardDeadline)" in cleanup
    assert "overallStartedAt = 0" in cleanup
    start_click = html.split('startBtn.addEventListener("click"', 1)[1].split(
        'stopBtn.addEventListener("click"', 1
    )[0]
    assert "cleanupMedia(true, canRetryInitialToken)" in start_click
    stop_click = html.split('stopBtn.addEventListener("click"', 1)[1].split(
        'feedbackBtn.addEventListener("click"', 1
    )[0]
    assert 'stopLive(true, "自由辯論已停止。", canRetryInitialToken)' in stop_click
    start_live = html.split("async function startLive()", 1)[1].split(
        "function currentToken", 1
    )[0]
    assert "overallStartedAt = 0" not in start_live
    assert "hardDeadlineReached = false" not in start_live
    assert start_live.index("startFreeHardDeadline()") < start_live.index(
        "if (!overallStartedAt) return"
    ) < start_live.index("getLiveSessionToken(0, startEpoch)")


def test_mock_uses_same_start_anchored_deadline_through_retry_and_pause():
    _, _, html = _render_mock([])
    deadline = html.split("function startFreeHardDeadline()", 1)[1].split(
        "function fireBellOnce", 1
    )[0]
    cleanup = html.split("function cleanupMedia", 1)[1].split(
        "function stopLive", 1
    )[0]
    start_live = html.split("async function startLive()", 1)[1].split(
        "function currentToken", 1
    )[0]
    mock_pause = html.split("function mockPause", 1)[1].split(
        "function mockToggleTimer", 1
    )[0]

    assert "if (MOCK_MODE) return" not in deadline
    assert "Date.now() - overallStartedAt" in deadline
    assert "const keepHardDeadline = Boolean(preserveHardDeadline);" in cleanup
    assert "&& !MOCK_MODE" not in cleanup
    assert "if (!overallStartedAt) return" in start_live
    assert "overallStartedAt = 0" not in mock_pause


def test_retry_resume_keeps_discard_guard_until_a_real_new_turn_is_sent():
    _, _, html = _render_mock([])
    resumed = html.split("function onConnectionResumed", 1)[1].split(
        "function openSessionWs", 1
    )[0]
    assert "discardServerResponseUntilTurnSent = false" not in resumed
    end_turn = html.split("function endTurn", 1)[1].split(
        "function toggleTurn", 1
    )[0]
    assert end_turn.index("activityEnd") < end_turn.index(
        "discardServerResponseUntilTurnSent = false"
    )
    text_turn = html.split("function sendTextTurn", 1)[1].split(
        "function isDiscardingNormalResponse", 1
    )[0]
    assert text_turn.index("ws.send") < text_turn.index(
        "discardServerResponseUntilTurnSent = false"
    )
    cleanup = html.split("function cleanupMedia", 1)[1].split(
        "function stopLive", 1
    )[0]
    assert "tokenHandoffGeneration += 1" in cleanup
    assert "tokenHandoffInFlight = false" in cleanup
