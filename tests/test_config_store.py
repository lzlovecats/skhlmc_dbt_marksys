import unittest

import pandas as pd

from core.config_store import config_spec, get_config, get_configs, set_config


class FakeConfigDb:
    def __init__(self, typed=None, legacy=None):
        self.typed = dict(typed or {})
        self.legacy = dict(legacy or {})
        self.queries = []
        self.executions = []

    @staticmethod
    def _requested(params):
        return [value for key, value in sorted((params or {}).items()) if key.startswith("key_")]

    def query(self, sql, params=None):
        self.queries.append((sql, params or {}))
        source = self.typed if "FROM app_config" in sql else self.legacy
        if " IN (" in sql:
            keys = self._requested(params)
            return pd.DataFrame(
                [{"key": key, "value": source[key]} for key in keys if key in source]
            )
        key = (params or {}).get("key")
        return pd.DataFrame([] if key not in source else [{"value": source[key]}])

    def execute(self, sql, params=None):
        self.executions.append((sql, params or {}))


class ConfigStoreTests(unittest.TestCase):
    def test_batch_read_uses_typed_values_then_one_legacy_fallback(self):
        db = FakeConfigDb(
            typed={"maintenance_mode": True, "ai_fund_target_hkd": 800},
            legacy={"tts_recording_allowed_users": '["member-a","member-b"]'},
        )

        values = get_configs(
            db,
            ["maintenance_mode", "ai_fund_target_hkd", "tts_recording_allowed_users"],
        )

        self.assertEqual(values["maintenance_mode"], True)
        self.assertEqual(values["ai_fund_target_hkd"], 800)
        self.assertEqual(values["tts_recording_allowed_users"], ["member-a", "member-b"])
        self.assertEqual(len(db.queries), 2)
        self.assertIn("FROM app_config", db.queries[0][0])
        self.assertIn("FROM system_config", db.queries[1][0])

    def test_single_read_falls_back_to_legacy_and_coerces_boolean(self):
        db = FakeConfigDb(legacy={"maintenance_mode": "yes"})
        self.assertIs(get_config(db, "maintenance_mode"), True)
        self.assertEqual(len(db.queries), 2)

    def test_registered_secret_is_classified_and_serialized(self):
        db = FakeConfigDb()
        set_config(db, "cookie_secret", "private-value")
        self.assertEqual(len(db.executions), 1)
        params = db.executions[0][1]
        self.assertEqual(params["namespace"], "auth")
        self.assertIs(params["is_secret"], True)
        self.assertEqual(params["value_type"], "string")
        self.assertEqual(params["value"], '"private-value"')

    def test_unknown_runtime_write_is_rejected(self):
        db = FakeConfigDb()
        with self.assertRaises(KeyError):
            set_config(db, "unclassified_setting", "value")
        self.assertEqual(db.executions, [])

    def test_known_array_rejects_wrong_shape(self):
        db = FakeConfigDb()
        with self.assertRaises(ValueError):
            set_config(db, "login_disabled_accounts", {"member": True})

    def test_prefix_resource_keys_are_classified(self):
        spec = config_spec("bandwidth_3gb_push_2026-07")
        self.assertEqual((spec.namespace, spec.value_type), ("resource", "number"))


if __name__ == "__main__":
    unittest.main()
