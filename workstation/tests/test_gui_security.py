from pathlib import Path
from types import SimpleNamespace

from workstation.gui import server


def test_gui_has_no_external_assets_or_shell_and_binds_localhost():
    html = (Path(server.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    script = (Path(server.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    service = (Path(server.__file__).parents[1] / "systemd" / "lmc-ai-gui.service").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8765/" in (Path(server.__file__).parents[1] / "packaging" / "lmc-ai-workstation.desktop").read_text()
    assert 'src="https://' not in html
    assert 'href="https://' not in html
    assert "shell" not in html.casefold()
    assert "X-LMC-CSRF" in script
    assert "IPAddressDeny=any" in service
    assert "IPAddressAllow=localhost" in service


def test_gui_rollback_only_triggers_the_drained_background_service(
    tmp_path, monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        server,
        "request_privileged",
        lambda payload, socket: calls.append((payload, socket)) or {"ok": True},
    )
    config = SimpleNamespace(paths=SimpleNamespace(state=tmp_path))
    application = server.GuiApplication(
        config,
        manager_socket=tmp_path / "manager.sock",
        privileged_socket=tmp_path / "privileged.sock",
    )
    assert application.action({"action": "rollback_previous"}) == {"ok": True}
    assert calls == [(
        {"action": "trigger_rollback"}, tmp_path / "privileged.sock",
    )]


def test_gui_reconfiguration_requires_a_completely_idle_manager(
    tmp_path, monkeypatch,
):
    privileged_calls = []
    monkeypatch.setattr(
        server,
        "request_privileged",
        lambda payload, socket: privileged_calls.append(payload) or {"ok": True},
    )
    config = SimpleNamespace(paths=SimpleNamespace(state=tmp_path))
    application = server.GuiApplication(
        config,
        manager_socket=tmp_path / "manager.sock",
        privileged_socket=tmp_path / "privileged.sock",
    )
    application.manager_request = lambda _payload: {
        "manager": {"mode": "voice_coach", "voice_session_active": True},
    }
    try:
        application.action({"action": "set_update_channel", "channel": "stable"})
    except ValueError:
        pass
    else:
        raise AssertionError("active voice session must block reconfiguration")
    try:
        application.action({
            "action": "restart_service", "service": "ollama.service",
        })
    except ValueError:
        pass
    else:
        raise AssertionError("active voice session must block service restart")
    assert privileged_calls == []

    application.manager_request = lambda _payload: {
        "manager": {
            "mode": "idle",
            "draining": True,
            "active_operation": None,
            "voice_session_active": False,
            "voice_session_pending": False,
        },
    }
    try:
        application.action({"action": "set_update_channel", "channel": "stable"})
    except ValueError:
        pass
    else:
        raise AssertionError("drained manager must block reconfiguration")
    assert privileged_calls == []

    application.manager_request = lambda _payload: {
        "manager": {
            "mode": "idle",
            "active_operation": None,
            "voice_session_active": False,
            "voice_session_pending": False,
        },
    }
    assert application.action({
        "action": "set_update_channel", "channel": "candidate",
    }) == {"ok": True}
    assert privileged_calls == [{
        "action": "set_update_channel", "channel": "candidate",
    }]
