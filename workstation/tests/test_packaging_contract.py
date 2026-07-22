from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_deb_install_never_downloads_models_or_starts_unpaired_node():
    postinst = (ROOT / "workstation/packaging/debian/postinst").read_text()
    node_unit = (ROOT / "workstation/systemd/lmc-ai-node.service").read_text()
    assert "curl " not in postinst
    assert "wget " not in postinst
    assert "ollama pull" not in postinst
    assert "ConditionPathExists=/etc/lmc-ai-workstation/credentials/node-token" in node_unit
    assert "Conflicts=skhlmc-lmc-ai-node.service" in node_unit
    assert "--no-create-home" in postinst
    assert 'root -g "$service_group" -m 0750 "$data_root/health"' in postinst
    assert "/var/lib/lmc-ai-workstation/release" in postinst
    assert 'chown root:"$service_group" "$config_dir/config.json"' in postinst
    assert 'chmod 0640 "$config_dir/config.json"' in postinst
    assert '[[ -L "$config_dir/config.json"' in postinst


def test_pairing_migrates_the_legacy_node_without_dual_claiming():
    helper = (ROOT / "workstation/privileged_helper/server.py").read_text()
    assert 'LEGACY_NODE_SERVICE = "skhlmc-lmc-ai-node.service"' in helper
    assert '["systemctl", "disable", "--now", LEGACY_NODE_SERVICE]' in helper
    assert '["systemctl", "enable", "lmc-ai-node.service"]' in helper
    assert '["systemctl", "restart", "lmc-ai-manager.service"]' in helper
    assert '["systemctl", "stop", "lmc-ai-node.service"]' in helper
    assert '["systemctl", "start", "lmc-ai-node.service"]' in helper
    assert 'website_receipt.unlink(missing_ok=True)' in helper
    assert "_wait_for_fresh_website_receipt(started_epoch)" in helper
    assert "old_config" in helper and "old_token" in helper


def test_security_reboot_is_separate_from_unattended_upgrade():
    apt = (ROOT / "workstation/packaging/52lmc-ai-workstation-unattended").read_text()
    service = (ROOT / "workstation/systemd/lmc-ai-reboot.service").read_text()
    cli = (ROOT / "workstation/scripts/workstationctl.py").read_text()
    assert 'Automatic-Reboot "false"' in apt
    assert "#clear Unattended-Upgrade::Allowed-Origins;" in apt
    assert '${distro_codename}-security' in apt
    assert '${distro_codename}-apps-security' in apt
    assert '${distro_codename}-infra-security' in apt
    assert '${distro_codename}-updates' not in apt
    assert "ConditionPathExists=/var/run/reboot-required" in service
    assert 'manager.get("sleep_inhibited")' in cli
    preflight = (ROOT / "workstation/scripts/preflight_ubuntu.sh").read_text()
    assert "apt-config dump" in preflight
    assert "apt-daily.timer apt-daily-upgrade.timer" in preflight
    assert 'Unattended-Upgrade::Automatic-Reboot \\"false\\";' not in preflight
    assert 'Unattended-Upgrade::Automatic-Reboot "false";' in preflight
    assert "non-security unattended-upgrade origin is enabled" in preflight


def test_preflight_requires_tailscale_ssh_rtc_and_suspend_capabilities():
    preflight = (ROOT / "workstation/scripts/preflight_ubuntu.sh").read_text()
    assert 'value.get("RunSSH") is not True' in preflight
    assert "rtcwake --mode show" in preflight
    assert "grep -qw mem /sys/power/state" in preflight
    assert "findmnt -n -o SOURCE --target /srv/lmc-ai" in preflight
    assert 'grep -qx crypt' in preflight


def test_periodic_full_health_is_separate_and_never_skips_manager_arbitration():
    service = (ROOT / "workstation/systemd/lmc-ai-full-health.service").read_text()
    timer = (ROOT / "workstation/systemd/lmc-ai-full-health.timer").read_text()
    postinst = (ROOT / "workstation/packaging/debian/postinst").read_text()
    assert "workstationctl full-health" in service
    assert "OnUnitInactiveSec=6h" in timer
    assert "Persistent=true" in timer
    assert "lmc-ai-full-health.timer" in postinst


def test_manual_rollback_is_a_separate_drained_health_gated_service():
    unit = (ROOT / "workstation/systemd/lmc-ai-rollback.service").read_text()
    gui = (ROOT / "workstation/gui/server.py").read_text()
    cli = (ROOT / "workstation/scripts/workstationctl.py").read_text()
    assert "rollback-previous" in unit
    assert "trigger_rollback" in gui
    assert "_wait_for_idle(args)" in cli
    assert "_wait_for_full_health(args)" in cli
    assert "release-operation.lock" in cli


def test_maintainer_scripts_use_strict_bash_and_preserve_data_on_remove():
    for name in ("postinst", "prerm", "postrm"):
        content = (ROOT / "workstation/packaging/debian" / name).read_text()
        assert content.startswith("#!/bin/bash\nset -euo pipefail")
    assert "retains credentials" in (ROOT / "workstation/packaging/debian/postrm").read_text()


def test_ollama_is_loopback_only_and_uses_managed_model_storage():
    drop_in = (
        ROOT
        / "workstation/packaging/ollama.service.d/lmc-ai-workstation.conf"
    ).read_text()
    build = (ROOT / "workstation/scripts/build_deb.sh").read_text()
    assert "OLLAMA_HOST=127.0.0.1:11434" in drop_in
    assert "OLLAMA_MODELS=/srv/lmc-ai/models/ollama" in drop_in
    assert "SupplementaryGroups=lmc-ai" in drop_in
    assert "ollama.service.d" in build


def test_manager_cannot_write_the_pinned_vendor_runtime():
    unit = (ROOT / "workstation/systemd/lmc-ai-manager.service").read_text()
    assert "/srv/lmc-ai/vendor" not in next(
        line for line in unit.splitlines() if line.startswith("ReadWritePaths=")
    )


def test_gpt_sovits_service_uses_only_the_explicit_approved_voice_config():
    unit = (ROOT / "workstation/systemd/lmc-ai-gpt-sovits.service").read_text()
    assert "-a 127.0.0.1 -p 9880" in unit
    assert "-c /srv/lmc-ai/models/gpt-sovits/tts_infer.json" in unit
    assert "GPT_SoVITS/configs/tts_infer.yaml" not in unit
    assert "/srv/lmc-ai/vendor/GPT-SoVITS" not in next(
        line for line in unit.splitlines() if line.startswith("ReadWritePaths=")
    )


def test_privileged_helper_can_write_only_required_release_state_paths():
    service = (ROOT / "workstation/systemd/lmc-ai-privileged.service").read_text()
    assert "/var/lib/lmc-ai-workstation" in service
    assert "/srv/lmc-ai" not in service
    assert "Group=lmc-ai" in service
    assert "RuntimeDirectoryMode=0770" in service
    helper = (ROOT / "workstation/privileged_helper/server.py").read_text()
    assert "_safe_extract(archive_copy, temporary)" in helper
    assert "verify_release_tree(temporary)" in helper
    assert "verified-envelope.json" not in helper
    assert "shutil.copytree(tree" not in helper
    assert "return 75 if reload_helper else 0" in helper


def test_node_can_write_only_its_runtime_and_public_website_receipt():
    unit = (ROOT / "workstation/systemd/lmc-ai-node.service").read_text()
    writable = next(
        line for line in unit.splitlines() if line.startswith("ReadWritePaths=")
    )
    assert writable == (
        "ReadWritePaths=/run/lmc-ai-workstation "
        "/var/lib/lmc-ai-workstation"
    )


def test_build_requires_ed25519_trust_key_and_excludes_test_cache():
    build = (ROOT / "workstation/scripts/build_deb.sh").read_text()
    assert "WORKSTATION_RELEASE_PUBLIC_KEY_FILE" in build
    assert "Ed25519PublicKey" in build
    assert "release-signing-public-key.pem" in build
    assert "__pycache__" in build
    assert 'workstation/tests" -depth -delete' in build
    assert 'find "$release_root" -type d -exec chmod a-s,a-t,go-w' in build
    assert 'find "$release_root" -type f -exec chmod a-s,a-t,go-w' in build
    assert 'chmod 0644 "$release_root/release-files.sha256"' in build


def test_runtime_uses_ubuntu_python_packages_without_install_time_pip():
    control = (ROOT / "workstation/packaging/debian/control").read_text()
    postinst = (ROOT / "workstation/packaging/debian/postinst").read_text()
    node = (ROOT / "workstation/node/client.py").read_text()
    assert "python3-httpx" in control
    assert "python3-websockets" in control
    assert "python3-cryptography" in control
    assert "pip install" not in postinst
    assert '"extra_headers"' in node
    assert '"additional_headers"' in node


def test_asr_uses_the_pinned_official_qwen_package_without_deb_auto_install():
    asr_requirements = (
        ROOT / "workstation/requirements-asr.txt"
    ).read_text()
    postinst = (
        ROOT / "workstation/packaging/debian/postinst"
    ).read_text()
    runbook = (ROOT / "docs/AI_WORKSTATION_RUNBOOK.md").read_text()
    assert "qwen-asr==0.0.6" in asr_requirements
    assert "requirements-asr.txt" not in postinst
    assert ".deb` **唔會自動安裝 ASR runtime 或下載 model**" in runbook


def test_runbook_has_machine_and_browser_acceptance_evidence_commands():
    runbook = (ROOT / "docs/AI_WORKSTATION_RUNBOOK.md").read_text()
    assert not (ROOT / "docs/workstation_plan.md").exists()
    assert not (ROOT / "docs/LMC_AI_NODE_RUNBOOK.md").exists()
    assert not (ROOT / "workstation/RUNBOOK.md").exists()
    assert not (ROOT / "workstation/README.md").exists()
    assert "## 0. 遲啲 setup 實體 Workstation：由呢度開始" in runbook
    assert "### 0.1 安裝日前要準備" in runbook
    assert "### 0.2 安裝日執行次序" in runbook
    assert "### 0.3 Go-live gate" in runbook
    assert "workstation.scripts.collect_ubuntu_evidence" in runbook
    assert "manual_gates_complete" in runbook
    assert "lmcAiPracticeAcceptanceReport" in runbook
    assert "workstation.scripts.verify_voice_latency" in runbook
    assert "20 個全部成功嘅 local warm turns" in runbook
