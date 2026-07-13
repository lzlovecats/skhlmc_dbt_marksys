import datetime as dt
import unittest

import pandas as pd

from core.registration_logic import (
    HKT,
    save_registration_settings,
    submit_registration,
    update_registration_status,
)


VALID = {
    "team_name": "測試隊伍",
    "main_debater_name": "甲",
    "first_deputy_name": "乙",
    "second_deputy_name": "丙",
    "closing_debater_name": "丁",
    "contact_name": "聯絡人",
    "contact_class": "6A",
    "contact_phone": "12345678",
}


class RegistrationDb:
    def __init__(self, edition=4):
        now = dt.datetime.now(HKT).replace(tzinfo=None)
        self.settings = pd.DataFrame([{
            "competition_edition": edition,
            "registration_start": now - dt.timedelta(hours=1),
            "registration_end": now + dt.timedelta(hours=1),
            "updated_at": now,
        }])
        self.inserted = []
        self.saved_settings = None
        self.changed = 1

    def query(self, sql, params=None):
        if "competition_registration_settings" in sql and "SELECT" in sql:
            return self.settings.copy()
        if "COUNT(*) AS n" in sql and "FROM competition_registrations" in sql:
            return pd.DataFrame([{"n": 0, "duplicate": 0}])
        if "SELECT 1 FROM competition_registrations" in sql:
            return pd.DataFrame()
        raise AssertionError(sql)

    def execute(self, sql, params=None):
        if "INSERT INTO competition_registrations (" in sql:
            self.inserted.append(dict(params or {}))
        if "INSERT INTO competition_registration_settings" in sql:
            self.saved_settings = dict(params or {})

    def execute_count(self, sql, params=None):
        return self.changed


class RegistrationSubmissionTests(unittest.TestCase):
    def test_rejects_stale_or_tampered_edition(self):
        db = RegistrationDb(edition=4)
        result = submit_registration(VALID, 3, db=db)
        self.assertFalse(result["ok"])
        self.assertIn("屆數已更新", result["message"])
        self.assertEqual(db.inserted, [])

    def test_uses_current_server_edition_for_insert(self):
        db = RegistrationDb(edition=4)
        result = submit_registration(VALID, 4, db=db)
        self.assertTrue(result["ok"])
        self.assertEqual(db.inserted[0]["competition_edition"], 4)

    def test_admin_rejects_invalid_edition_and_missing_record(self):
        db = RegistrationDb()
        invalid = save_registration_settings(0, "2026-10-01T08:30", "2026-10-31T23:45", db=db)
        self.assertFalse(invalid["ok"])
        db.changed = 0
        missing = update_registration_status(999, "confirmed", db=db)
        self.assertFalse(missing["ok"])
        self.assertIn("找不到", missing["message"])

    def test_settings_normalise_aware_datetimes_to_hong_kong_naive(self):
        db = RegistrationDb()
        result = save_registration_settings(
            4,
            "2026-10-01T00:30:00+00:00",
            "2026-10-01T02:30:00+00:00",
            db=db,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(db.saved_settings["registration_start"].hour, 8)
        self.assertIsNone(db.saved_settings["registration_start"].tzinfo)


if __name__ == "__main__":
    unittest.main()
