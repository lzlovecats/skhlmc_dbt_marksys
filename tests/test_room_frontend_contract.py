"""Static contracts for the two-person AI Coach room client.

These checks intentionally cover browser/server protocol seams that are easy to
break while editing the standalone room script without requiring a real mic or
two WebRTC-capable browsers in pytest.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ROOM_JS = (ROOT / "frontend/shared/room-debate-p2p.js").read_text("utf-8")
PARITY_JS = (ROOT / "frontend/shared/ai-parity.js").read_text("utf-8")
ROOM_HTML = (ROOT / "templates/room_debate.html").read_text("utf-8")
COACH_HTML = (ROOT / "frontend/ai_coach/index.html").read_text("utf-8")
APPLIANCE_HTML = (ROOT / "templates/appliance_ai_debate.html").read_text("utf-8")


def test_room_script_dom_references_exist_in_template():
    referenced_ids = set(re.findall(r'\$\("([A-Za-z][A-Za-z0-9_-]*)"\)', ROOM_JS))
    template_ids = set(re.findall(r'\bid="([A-Za-z][A-Za-z0-9_-]*)"', ROOM_HTML))
    assert referenced_ids <= template_ids
    assert {"micStatus", "roomAlert", "roomBack"} <= template_ids


def test_outbound_mic_is_authoritatively_turn_gated_and_visibly_muted():
    assert 'track.enabled = enabled' in ROOM_JS
    assert 'state.active_turn_user === myUid' in ROOM_JS
    assert 'state.active_turn_id === ownedTurnId' in ROOM_JS
    assert "mayAdoptOwnTurn" in ROOM_JS
    assert "requestedTurnMatches || previouslyOwnedTurn === serverTurnId" in ROOM_JS
    assert 'setMicMode("testing")' in ROOM_JS
    assert 'finally {' in ROOM_JS and 'setMicMode("muted")' in ROOM_JS
    assert "咪高峰：已靜音" in ROOM_HTML
    assert "咪高峰：已開啟" in ROOM_JS


def test_dead_media_tracks_pause_and_recover_instead_of_silent_timing():
    assert ROOM_JS.count('track.addEventListener("ended"') >= 2
    assert 'reportRtcStatus("disconnected", true)' in ROOM_JS
    assert 'recoverActivePeer({ forceRestart: true })' in ROOM_JS
    assert "pc !== createdPeer || epoch !== peerEpoch" in ROOM_JS


def test_remote_audio_and_data_channel_are_authoritatively_bounded():
    assert "function applyRemoteAudioGate()" in ROOM_JS
    assert "peerIds.has(state.active_turn_user)" in ROOM_JS
    assert "remoteAudio.muted" in ROOM_JS
    assert 'typeof event.data !== "string" || event.data.length > 100' in ROOM_JS
    assert "/^(?:ping|pong):\\d{1,16}$/" in ROOM_JS


def test_forced_turn_stop_uses_deadline_and_commits_partial_before_turn_end():
    assert 'case "turn_stop_requested"' in ROOM_JS
    assert "deadline_ms" in ROOM_JS
    assert "FORCED_RECOGNITION_STOP_MAX_MS = 700" in ROOM_JS
    commit = ROOM_JS.index('type: "transcript_commit"')
    turn_end = ROOM_JS.index('type: "turn_end"', commit)
    assert commit < turn_end
    assert "partial: Boolean(forced || timedOut)" in ROOM_JS
    assert "【可能不完整】" in ROOM_JS


def test_disabled_judgement_also_prevents_browser_speech_recognition():
    recognition = ROOM_JS.split("function startRecognition", 1)[1].split(
        "function stopRecognitionGracefully", 1
    )[0]
    assert "state.judge_enabled === false" in recognition


def test_turn_start_is_correlated_and_manual_stop_freezes_server_clock_first():
    assert 'type: "turn_begin", request_id: requestId' in ROOM_JS
    assert "state.active_turn_request_id === requestId" in ROOM_JS
    assert 'reason: "start_confirmation_timeout"' in ROOM_JS
    intent = ROOM_JS.index('type: "turn_stop_intent"', ROOM_JS.index("async function finalizeTalk"))
    stop_recognition = ROOM_JS.index("stopRecognitionGracefully", intent)
    assert intent < stop_recognition
    assert "!state.turn_stop_pending && state.active_turn_side" in ROOM_JS


def test_transcript_events_dedupe_by_server_revision_or_turn_id():
    assert "function transcriptItemKey(item)" in ROOM_JS
    assert "revision:${revision}" in ROOM_JS
    assert "turn:${turnId}" in ROOM_JS
    assert "mergeTranscriptItem(message.item)" in ROOM_JS
    key_function = ROOM_JS.split("function transcriptItemKey", 1)[1].split(
        "function sortTranscriptItems", 1
    )[0]
    assert key_function.index("turn:${turnId}") < key_function.index(
        "revision:${revision}"
    )


def test_reopened_result_snapshot_renders_the_retained_roster():
    assert "function renderRosterList(items)" in ROOM_JS
    assert "if (Array.isArray(data.roster)) renderRosterList(data.roster);" in ROOM_JS


def test_timer_and_bells_use_authoritative_server_snapshots():
    assert "state.seg_elapsed_ms" in ROOM_JS
    assert "state.rtc_paused || !controlConnected" in ROOM_JS
    assert 'case "bell"' in ROOM_JS
    assert "segment_generation" in ROOM_JS
    assert "seenBellEvents" in ROOM_JS
    assert "Number(state.segment_generation || 0)" in ROOM_JS
    assert "state.bells" not in ROOM_JS
    assert "lastBellSegment" not in ROOM_JS


def test_strict_free_turn_is_visible_and_gates_the_speak_button():
    assert "嚴格交替｜輪到${message.expected_turn_side}發言" in ROOM_JS
    assert "!expectedSideOkay && state.expected_turn_side" in ROOM_JS
    assert "等待${state.expected_turn_side}發言" in ROOM_JS
    assert "每次停咪後嚴格正反交替" in PARITY_JS
    assert "每次停咪後嚴格正反交替" in APPLIANCE_HTML


def test_rtc_signalling_is_serialized_singleflight_and_memory_bounded():
    assert "signalQueue = signalQueue" in ROOM_JS
    assert "if (peerPromise) return peerPromise" in ROOM_JS
    assert "if (offerPromise) return offerPromise" in ROOM_JS
    assert "peerPromise === promise" in ROOM_JS
    assert "MAX_PENDING_REMOTE_ICE = 100" in ROOM_JS
    assert "queueRemoteIce" in ROOM_JS
    assert 'phase === "lobby" && !preflightRunning && !localRtcReady' in ROOM_JS


def test_rtc_description_mutations_revalidate_exact_peer_and_generation():
    current_check = ROOM_JS.split("function rtcOperationIsCurrent", 1)[1].split(
        "function resetExactStalePeer", 1
    )[0]
    exact_reset = ROOM_JS.split("function resetExactStalePeer", 1)[1].split(
        "async function ensurePeer", 1
    )[0]
    make_offer = ROOM_JS.split("async function makeOffer", 1)[1].split(
        "async function flushRemoteIce", 1
    )[0]
    on_signal = ROOM_JS.split("async function onSignal", 1)[1].split(
        "function reportRtcStatus", 1
    )[0]

    assert "pc === activePeer" in current_check
    assert "peerEpoch === operationPeerEpoch" in current_check
    assert "Number(generation) === rosterGeneration" in current_check
    assert "pc !== activePeer || peerEpoch !== operationPeerEpoch" in exact_reset
    assert "resetPeer({ keepLocalStream: true })" in exact_reset
    assert "peerEpoch === recoveryPeerEpoch" in exact_reset
    assert "pc === null" in exact_reset

    local_description = make_offer.index("await activePeer.setLocalDescription")
    assert make_offer.index("rtcOperationIsCurrent", local_description) > local_description
    assert make_offer.index("resetExactStalePeer", local_description) > local_description
    assert "if (!sent) resetExactStalePeer" in make_offer

    assert on_signal.count("await activePeer.setRemoteDescription") == 2
    assert on_signal.count("await activePeer.setLocalDescription") == 1
    assert on_signal.count("resetExactStalePeer") >= 7
    assert "flushRemoteIce(generation, activePeer, operationPeerEpoch)" in on_signal
    assert "await activePeer.addIceCandidate(message.candidate)" in on_signal


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_rtc_generation_races_reset_only_the_mutated_stale_peer():
    reset_and_guards = ROOM_JS.split("function resetPeer", 1)[1].split(
        "async function ensurePeer", 1
    )[0]
    make_offer = "async function makeOffer" + ROOM_JS.split(
        "async function makeOffer", 1
    )[1].split("async function flushRemoteIce", 1)[0]
    flush_remote_ice = "async function flushRemoteIce" + ROOM_JS.split(
        "async function flushRemoteIce", 1
    )[1].split("function queueRemoteIce", 1)[0]
    on_signal = "async function onSignal" + ROOM_JS.split(
        "async function onSignal", 1
    )[1].split("function reportRtcStatus", 1)[0]

    script = textwrap.dedent(
        f"""
        "use strict";
        let pc = null;
        let peerEpoch = 0;
        let peerPromise = null;
        let offerPromise = null;
        let dataChannel = null;
        let remoteStream = null;
        let pendingRemoteIce = [];
        let localSignalGeneration = 0;
        let remoteAudio = null;
        const localStream = {{ marker: "preserved" }};
        let rosterGeneration = 1;
        let controlConnected = true;
        let terminalConnection = false;
        let phase = "active";
        let isHost = true;
        let localRtcReady = false;
        let preflightRunning = false;
        let readyPeers = new Set();
        let roster = [{{ connected: true }}, {{ connected: true }}];
        let sent = [];
        let stoppedLocal = 0;
        let recoveryCalls = 0;
        let logs = [];

        function stopLocalStream() {{ stoppedLocal += 1; }}
        function recoverActivePeer() {{ recoveryCalls += 1; return Promise.resolve(); }}
        function send(message) {{ sent.push(message); return true; }}
        function log(message) {{ logs.push(message); }}
        function queueRemoteIce(generation, candidate) {{
          pendingRemoteIce.push({{ generation, candidate }});
        }}
        async function ensurePeer() {{ return pc; }}

        function resetPeer{reset_and_guards}
        {make_offer}
        {flush_remote_ice}
        {on_signal}

        function peer(overrides = {{}}) {{
          return {{
            signalingState: "stable",
            connectionState: "connected",
            remoteDescription: {{ type: "offer" }},
            localDescription: {{ toJSON: () => ({{ type: "offer" }}) }},
            closed: false,
            close() {{ this.closed = true; }},
            async createOffer(options) {{ return {{ type: "offer", options }}; }},
            async createAnswer() {{ return {{ type: "answer" }}; }},
            async setLocalDescription(description) {{
              this.localDescription = {{ ...description, toJSON: () => description }};
              this.signalingState = "have-local-offer";
            }},
            async setRemoteDescription(description) {{
              this.remoteDescription = description;
              this.signalingState = "stable";
            }},
            async addIceCandidate() {{}},
            ...overrides,
          }};
        }}

        function resetHarness(activePeer, signalingState = "stable") {{
          pc = activePeer;
          pc.signalingState = signalingState;
          peerEpoch = 10;
          rosterGeneration = 1;
          offerPromise = null;
          pendingRemoteIce = [];
          localSignalGeneration = 0;
          sent = [];
          stoppedLocal = 0;
          recoveryCalls = 0;
          controlConnected = true;
          terminalConnection = false;
          phase = "active";
        }}

        (async () => {{
          // ICE-restart offer: generation changes after the local description
          // mutates the exact current peer, so it must be reset and never sent.
          const staleOfferPeer = peer({{
            async setLocalDescription(description) {{
              this.localDescription = {{ ...description, toJSON: () => description }};
              this.signalingState = "have-local-offer";
              rosterGeneration = 2;
            }},
          }});
          resetHarness(staleOfferPeer);
          const offered = await makeOffer({{ iceRestart: true }});
          if (offered || sent.length || !staleOfferPeer.closed || pc !== null) process.exit(11);
          if (stoppedLocal !== 0 || localStream.marker !== "preserved") process.exit(12);

          // If a newer peer has already replaced it, the stale continuation must
          // not close or clear that newer peer.
          const newerPeer = peer();
          const replacedOfferPeer = peer({{
            async setLocalDescription(description) {{
              this.localDescription = {{ ...description, toJSON: () => description }};
              pc = newerPeer;
              peerEpoch += 1;
              rosterGeneration = 2;
            }},
          }});
          resetHarness(replacedOfferPeer);
          const replacedResult = await makeOffer({{ iceRestart: true }});
          if (replacedResult || pc !== newerPeer || newerPeer.closed) process.exit(13);

          for (const signalType of ["rtc_offer", "rtc_answer", "rtc_ice"]) {{
            const staleSignalPeer = peer({{
              signalingState: signalType === "rtc_answer" ? "have-local-offer" : "stable",
              async setRemoteDescription(description) {{
                this.remoteDescription = description;
                rosterGeneration = 2;
              }},
              async addIceCandidate() {{ rosterGeneration = 2; }},
            }});
            resetHarness(
              staleSignalPeer,
              signalType === "rtc_answer" ? "have-local-offer" : "stable",
            );
            await onSignal({{
              type: signalType,
              roster_generation: 1,
              description: {{ type: signalType === "rtc_answer" ? "answer" : "offer" }},
              candidate: {{ candidate: "bounded-test" }},
            }});
            if (!staleSignalPeer.closed || pc !== null || sent.length) process.exit(14);
            if (stoppedLocal !== 0) process.exit(15);
          }}

          await new Promise((resolve) => setTimeout(resolve, 5));
        }})().catch((error) => {{
          console.error(error);
          process.exit(20);
        }});
        """
    )
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_stale_roster_and_state_snapshots_cannot_roll_the_client_back():
    assert "incomingGeneration < rosterGeneration" in ROOM_JS
    assert "effectiveGeneration = rosterGeneration" in ROOM_JS
    assert "incomingStateSequence < lastStateSequence" in ROOM_JS
    assert 'if (phase === "ended") return;' in ROOM_JS
    assert "Number(message.roster_generation) !== rosterGeneration" in ROOM_JS
    assert 'peerLeftUser = ""' in ROOM_JS


def test_segment_controls_are_disabled_during_rtc_pause():
    prev_guard = ROOM_JS.split('$("prevBtn").disabled =', 1)[1].split(";", 1)[0]
    next_guard = ROOM_JS.split('$("nextBtn").disabled =', 1)[1].split(";", 1)[0]
    assert "state.rtc_paused" in prev_guard
    assert "state.rtc_paused" in next_guard


def test_judgement_is_one_explicit_ended_room_http_request():
    assert '`/api/room/${encodeURIComponent(code)}/judgement`' in ROOM_JS
    assert '{ method: "POST", body: "{}" }' in ROOM_JS
    assert 'phase !== "ended"' in ROOM_JS
    assert "judgementRequestInFlight" in ROOM_JS
    assert "RESULT_POLL_MAX_ATTEMPTS = 300" in ROOM_JS
    assert "RESULT_POLL_WINDOW_MS = 15 * 60 * 1000" in ROOM_JS
    assert "Date.now() >= resultPollDeadline" in ROOM_JS
    assert 'send({ type: "request_judgement" })' not in ROOM_JS
    assert "要求 AI 評判（一次）" in ROOM_HTML


def test_ai_coach_room_guard_handshake_and_immediate_leave_teardown():
    assert "hasUnendedRoom" in PARITY_JS
    assert 'name !== "room"' in PARITY_JS
    assert 'message.type === "room_phase"' in PARITY_JS
    assert 'type: "parent_leave"' in PARITY_JS
    assert PARITY_JS.index('type: "parent_leave"') < PARITY_JS.index(
        "resetRoomUi(true)", PARITY_JS.index('type: "parent_leave"')
    )
    assert 'window.addEventListener("beforeunload"' in PARITY_JS
    assert "?embedded=1" in PARITY_JS
    assert 'allow="microphone; autoplay"' in COACH_HTML
    assert "房間控制連線仍在重試" in PARITY_JS
    assert 'id="retryRoom"' in COACH_HTML
    assert "message.recoverable" in PARITY_JS
    assert "房間仍然保留" in PARITY_JS
    assert "postRoomTerminal(message, 0, true)" in ROOM_JS
    assert "roomLeaveToken = message.leave_token" in PARITY_JS
    assert "leave_token: token" in PARITY_JS
    assert "leave_token: leaveToken" in ROOM_JS
    room_open = PARITY_JS.split("function roomOpen", 1)[1].split(
        'window.addEventListener("message"', 1
    )[0]
    assert "sessionStorage.aiRoom = normalized" in room_open
    assert "resetRoomUi(true)" not in PARITY_JS.split(
        "roomHandshakeTimer = setTimeout", 1
    )[1].split("}, 60000)", 1)[0]


def test_ended_snapshot_and_reconnect_retries_are_bounded():
    assert "ENDED_SNAPSHOT_RETRY_DELAYS_MS" in ROOM_JS
    assert "loadEndedSnapshot" in ROOM_JS
    assert "RECONNECT_DELAYS_MS" in ROOM_JS
    assert "reconnectStableTimer" in ROOM_JS
    assert "MAX_PENDING_REMOTE_ICE" in ROOM_JS


def test_room_codes_are_exactly_five_unambiguous_characters():
    for source in (PARITY_JS, COACH_HTML, APPLIANCE_HTML):
        assert "A-HJ-KM-NP-Z2-9" in source
    assert 'maxlength="5"' in COACH_HTML
    assert 'maxlength="5"' in APPLIANCE_HTML


def test_room_surfaces_disclose_network_speech_and_manual_ai_processing():
    combined = ROOM_HTML + COACH_HTML + APPLIANCE_HTML
    assert "STUN／ICE" in combined
    assert "網絡候選地址" in combined
    assert "Web Speech" in combined
    assert "瀏覽器供應商條款" in combined
    assert "完場後" in combined and "Gemini" in combined
    assert "15 分鐘" in combined
    assert "4GB 後停止一般 AI、聯機 AI 評判及聯機 Web Speech 逐字稿" in COACH_HTML


def test_room_shared_script_is_versioned_and_embedded_nav_targets_top():
    assert 'src="/shared/room-debate-p2p.js?v=__APP_VERSION__"' in ROOM_HTML
    assert 'target="_top"' in ROOM_HTML
    assert "html.embedded #practiceNav" in ROOM_HTML


def test_standalone_back_control_explicitly_leaves_before_navigation():
    assert "離開房間並返回 AI Coach" in ROOM_HTML
    leave_helper = ROOM_JS.split("function launchLeaveRequest", 1)[1].split(
        "function configureEmbedding", 1
    )[0]
    click = ROOM_JS.split('$("roomBack").addEventListener("click"', 1)[1]
    assert "/leave" in leave_helper
    assert "keepalive: true" in leave_helper
    assert "launchLeaveRequest()" in click
    assert "JSON.stringify({ leave_token: token })" in ROOM_JS
    assert click.index("teardownLocalRoom()") < click.index("location.assign(destination)")
