import datetime as dt
import unittest

import pandas as pd

from core.registration_logic import HKT, submit_registration


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

    def query(self, sql, params=None):
        if "competition_registration_settings" in sql and "SELECT" in sql:
            return self.settings.copy()
        if "SELECT 1 FROM competition_registrations" in sql:
            return pd.DataFrame()
        raise AssertionError(sql)

    def execute(self, sql, params=None):
        if "INSERT INTO competition_registrations (" in sql:
            self.inserted.append(dict(params or {}))


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


if __name__ == "__main__":
    unittest.main()
