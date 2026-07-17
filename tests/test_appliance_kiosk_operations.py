from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_backup_enforces_private_files_and_validates_restore_catalog():
    script = (ROOT / "appliance" / "backup_db.sh").read_text(encoding="utf-8")
    service = (ROOT / "appliance" / "marksys-backup.service").read_text(
        encoding="utf-8"
    )
    readme = (ROOT / "appliance" / "README.md").read_text(encoding="utf-8")
    assert "umask 077" in script
    assert 'chmod 700 "$BACKUP_DIR"' in script
    assert "pg_restore --list" in script
    assert "UMask=0077" in service
    assert "chmod 700 /var/backups/marksys" in readme
    assert "加密" in readme


def test_kiosk_chooser_does_not_offer_unprivileged_self_update():
    chooser = (ROOT / "appliance" / "marksys-kiosk.sh").read_text(encoding="utf-8")
    updater = (ROOT / "appliance" / "update.sh").read_text(encoding="utf-8")
    assert "更新系統（拉取最新版本）" not in chooser
    assert "run_update" not in chooser
    assert "管理員" in updater
