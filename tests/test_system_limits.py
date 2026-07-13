import pathlib
import re

import system_limits as limits


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_critical_resource_gates_have_safe_ordering_and_defaults():
    assert limits.LIMIT_SPECS["BANDWIDTH_WARN_BYTES"].default == 3_000_000_000
    assert limits.LIMIT_SPECS["BANDWIDTH_STOP_LIVE_BYTES"].default == 3_500_000_000
    assert limits.LIMIT_SPECS["BANDWIDTH_ESSENTIAL_ONLY_BYTES"].default == 4_000_000_000
    assert limits.BANDWIDTH_WARN_BYTES < limits.BANDWIDTH_STOP_LIVE_BYTES
    assert limits.BANDWIDTH_STOP_LIVE_BYTES < limits.BANDWIDTH_ESSENTIAL_ONLY_BYTES
    assert limits.LIMIT_SPECS["R2_STORAGE_WARN_BYTES"].default == 7_000_000_000
    assert limits.LIMIT_SPECS["R2_STORAGE_STOP_BYTES"].default == 8_000_000_000
    assert limits.R2_STORAGE_WARN_BYTES < limits.R2_STORAGE_STOP_BYTES


def test_every_registered_limit_has_operational_metadata():
    registry = limits.effective_limits()
    assert len(registry) >= 80
    for name, spec in registry.items():
        assert name == spec["name"]
        assert spec["group"]
        assert spec["description"]
        assert spec["value"] >= spec["minimum"]


def test_production_modules_do_not_read_limit_environment_variables_directly():
    pattern = re.compile(
        r"os\.getenv\(\s*['\"][^'\"]*"
        r"(?:LIMIT|MAX|WARN|STOP|RETENTION|CONCURRENCY|TIMEOUT|TTL|BYTES|ROWS|DAYS|SECONDS)"
    )
    for folder in ("api", "core", "deploy", "tools"):
        for path in (ROOT / folder).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert not pattern.search(source), f"move operational limit from {path} to system_limits.py"


def test_render_startup_reads_process_limits_from_registry():
    script = (ROOT / "deploy" / "start.sh").read_text(encoding="utf-8")
    assert "system_limits.py --startup" in script
    assert '${UVICORN_LIMIT_CONCURRENCY:-' not in script
