"""Migration catalog hygiene — apply-time surprises found the hard way.

The percent rule exists because a literal % anywhere in a migration file
(even a comment) makes exec_driver_sql fail with a TypeError at apply time;
20260714_0002 hit exactly that against production before lint caught it.
"""

import pytest

from core.db_migrations import discover_migrations, stray_files
from tools.manage_db_migrations import lint_report, load_catalog


def _write_pair(directory, version="20990101_0001", name="example",
                up="SELECT 1;\n", down="SELECT 2;\n"):
    (directory / f"{version}_{name}.up.sql").write_text(up, encoding="utf-8")
    (directory / f"{version}_{name}.down.sql").write_text(down, encoding="utf-8")


def test_repository_catalog_is_valid_offline():
    baseline, migrations = load_catalog()
    report = lint_report(baseline, migrations)
    assert report["catalog_valid"] is True
    assert report["registered_sql_migrations"] == len(migrations) >= 6


def test_valid_pair_is_discovered(tmp_path):
    _write_pair(tmp_path)
    migrations = discover_migrations(tmp_path)
    assert [m.version for m in migrations] == ["20990101_0001"]


def test_unpaired_migration_is_rejected(tmp_path):
    (tmp_path / "20990101_0001_example.up.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        discover_migrations(tmp_path)


def test_duplicate_version_is_rejected(tmp_path):
    _write_pair(tmp_path, name="first")
    (tmp_path / "20990101_0001_second.up.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        discover_migrations(tmp_path)


def test_empty_direction_is_rejected(tmp_path):
    _write_pair(tmp_path, down="   \n")
    with pytest.raises(ValueError, match="empty"):
        discover_migrations(tmp_path)


def test_embedded_transaction_control_is_rejected(tmp_path):
    _write_pair(tmp_path, up="BEGIN;\nSELECT 1;\nCOMMIT;\n")
    with pytest.raises(ValueError, match="transaction boundaries"):
        discover_migrations(tmp_path)


def test_percent_character_is_rejected_anywhere_in_file(tmp_path):
    _write_pair(tmp_path, up="-- 100% safe looking comment\nSELECT 1;\n")
    with pytest.raises(ValueError, match="percent"):
        discover_migrations(tmp_path)


def test_stray_files_are_reported(tmp_path):
    _write_pair(tmp_path)
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "baseline.json").write_text("{}", encoding="utf-8")
    assert stray_files(tmp_path, {"baseline.json"}) == ["notes.txt"]
