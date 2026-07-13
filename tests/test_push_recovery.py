import json
import unittest
from pathlib import Path

import pandas as pd
from fastapi import HTTPException

from core.push import notify_committee
from deploy.proxy import _validated_push_subscription


ROOT = Path(__file__).resolve().parents[1]


class RecordingDb:
    def __init__(self):
        self.executed = []

    def query(self, _sql, _params=None):
        subscription = {
            "endpoint": "https://fcm.googleapis.com/fcm/send/stale",
            "keys": {"p256dh": "key", "auth": "auth"},
        }
        return pd.DataFrame([
            {
                "endpoint": subscription["endpoint"],
                "user_id": "member",
                "subscription_json": json.dumps(subscription),
            }
        ])

    def execute(self, sql, params=None):
        self.executed.append((sql, params or {}))


class PushRecoveryTests(unittest.TestCase):
    def test_gone_fcm_subscription_is_marked_inactive_for_reconciliation(self):
        db = RecordingDb()

        sent = notify_committee(
            db,
            {"public_key": "public", "private_key": "private", "subject": "mailto:test@example.com"},
            "Test",
            "Body",
            send_fn=lambda *_args, **_kwargs: (False, "410 Gone"),
        )

        self.assertEqual(sent, 0)
        self.assertEqual(len(db.executed), 1)
        sql, params = db.executed[0]
        self.assertIn("is_active = CASE", sql)
        self.assertTrue(params["disable"])
        self.assertEqual(params["endpoint"], "https://fcm.googleapis.com/fcm/send/stale")

    def test_vote_page_reconciles_and_persists_push_subscription_after_login(self):
        html = (ROOT / "frontend" / "vote" / "index.html").read_text(encoding="utf-8")

        for marker in (
            "schedulePushReconcile();",
            "async function reconcilePushSubscription",
            'Notification.permission',
            'registration.pushManager.getSubscription()',
            'registration.pushManager.subscribe({',
            '"/api/push/subscribe"',
            "navigator.storage.persist()",
            "sameApplicationServerKey",
            'fetch("/sw.js", { cache: "no-store" })',
            'localStorage.getItem(PUSH_OPT_OUT_KEY) === "1"',
            'localStorage.setItem(PUSH_OPT_OUT_KEY, "1")',
        ):
            self.assertIn(marker, html)
        self.assertNotIn('postJSON("/api/push/unsubscribe", {})', html)

    def test_service_worker_retries_and_checks_migration_response(self):
        service_worker = (ROOT / "deploy" / "sw.js").read_text(encoding="utf-8")

        for marker in (
            "async function migrateSubscription",
            "attempt < 3",
            "if (response.ok) return true",
            "response.status !== 429",
            "newSubscription.toJSON()",
            "await migrateSubscription(oldEndpoint, newSub)",
        ):
            self.assertIn(marker, service_worker)

    def test_resubscribe_rejects_unknown_owner_instead_of_creating_orphan(self):
        proxy = (ROOT / "deploy" / "proxy.py").read_text(encoding="utf-8")

        self.assertIn('detail="Missing old endpoint"', proxy)
        self.assertIn('detail="Old subscription not found"', proxy)
        self.assertIn("if not user_id:", proxy)

    def test_push_subscription_requires_https_endpoint_and_both_keys(self):
        valid = {
            "endpoint": "https://fcm.googleapis.com/fcm/send/current",
            "keys": {"p256dh": "key", "auth": "auth"},
        }
        self.assertEqual(_validated_push_subscription(valid), valid["endpoint"])
        for invalid in (
            {"endpoint": "http://example.test/push", "keys": valid["keys"]},
            {"endpoint": valid["endpoint"], "keys": {"p256dh": "key"}},
        ):
            with self.assertRaises(HTTPException):
                _validated_push_subscription(invalid)


if __name__ == "__main__":
    unittest.main()
