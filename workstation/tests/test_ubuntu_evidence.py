from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from workstation.scripts import collect_ubuntu_evidence as evidence
from workstation.workloads.errors import WorkloadError


def test_key_value_parser_and_platform_gate_require_exact_ubuntu_point_release(
    tmp_path, monkeypatch,
):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
    (etc / "lsb-release").write_text(
        'DISTRIB_DESCRIPTION="Ubuntu 24.04.4 LTS"\n'
    )
    real_path = Path

    def mapped_path(value):
        mapping = {
            "/etc/os-release": etc / "os-release",
            "/etc/lsb-release": etc / "lsb-release",
        }
        return mapping.get(str(value), real_path(value))

    monkeypatch.setattr(evidence, "Path", mapped_path)
    assert evidence._platform_check()["ok"] is True

    (etc / "lsb-release").write_text(
        'DISTRIB_DESCRIPTION="Ubuntu 24.04.3 LTS"\n'
    )
    result = evidence._platform_check()
    assert result["ok"] is False
    assert result["code"] == "ubuntu_release_mismatch"


def test_service_gate_requires_every_unit_and_released_gpt_runtime(monkeypatch):
    states = {
        unit: {"active": True, "enabled": True}
        for unit in (
            *evidence.REQUIRED_ACTIVE_SERVICES,
            *evidence.REQUIRED_ACTIVE_TIMERS,
        )
    }
    states["skhlmc-lmc-ai-node.service"] = {
        "active": False, "enabled": False,
    }
    states["lmc-ai-gpt-sovits.service"] = {
        "active": False, "enabled": False,
    }
    monkeypatch.setattr(evidence, "_unit_state", lambda unit: states[unit])
    assert evidence._services_check()["ok"] is True

    states["lmc-ai-gpt-sovits.service"]["active"] = True
    result = evidence._services_check()
    assert result["ok"] is False
    assert result["gpt_sovits_released"] is False


def test_cold_boot_gate_rejects_late_start_or_graphical_user(monkeypatch):
    monkeypatch.setattr(evidence, "_monotonic_start_seconds", lambda _unit: 120)
    monkeypatch.setattr(
        evidence,
        "_session_types",
        lambda: [{"type": "tty", "class": "user", "remote": True}],
    )
    assert evidence._cold_boot_check()["ok"] is True

    monkeypatch.setattr(evidence, "_monotonic_start_seconds", lambda _unit: 301)
    assert evidence._cold_boot_check()["ok"] is False
    monkeypatch.setattr(evidence, "_monotonic_start_seconds", lambda _unit: 120)
    monkeypatch.setattr(
        evidence,
        "_session_types",
        lambda: [{"type": "wayland", "class": "user", "remote": False}],
    )
    assert evidence._cold_boot_check()["ok"] is False


def test_full_health_summary_never_copies_provider_details(monkeypatch):
    payload = {
        "healthy": False,
        "checked_epoch": 123,
        "checks": {
            "r2": {
                "ok": False,
                "code": "r2_failed",
                "signed_url": "https://secret.invalid/?signature=secret",
            },
        },
    }
    monkeypatch.setattr(
        evidence,
        "_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout=__import__("json").dumps(payload), stderr=""
        ),
    )
    result = evidence._full_health_check()
    assert result["ok"] is False
    assert result["checks"] == {"r2": {"ok": False, "code": "r2_failed"}}
    assert "secret" not in __import__("json").dumps(result)


def test_release_gate_reports_invalid_tree_without_aborting_collection(
    tmp_path, monkeypatch,
):
    release_root = tmp_path / "opt"
    target = release_root / "releases" / evidence.WORKSTATION_VERSION
    target.mkdir(parents=True)
    (target / "release.ready").write_text("", encoding="utf-8")
    (release_root / "current").symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(evidence, "DEFAULT_RELEASE_ROOT", release_root)
    monkeypatch.setattr(
        evidence,
        "verify_release_tree",
        lambda _path: (_ for _ in ()).throw(
            WorkloadError("release_tree_invalid", "invalid")
        ),
    )

    assert evidence._release_check() == {
        "ok": False,
        "code": "release_tree_invalid",
    }
