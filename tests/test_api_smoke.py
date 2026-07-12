import unittest

from fastapi.testclient import TestClient

from deploy.proxy import app


class ApiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_public_html_and_pwa_routes(self):
        for path in (
            "/", "/vote", "/admin-hub", "/chairperson", "/ai-coach",
            "/ai-training", "/judging", "/lateness-fund", "/manifest.json", "/sw.js",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_unknown_route_is_not_forwarded_to_streamlit(self):
        response = self.client.get("/definitely-not-a-route")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Not Found"})

    def test_protected_api_role_gates(self):
        paths = (
            "/api/ai-fund/data", "/api/ai-training/data", "/api/video-replay/data",
            "/api/match-photos/data", "/api/registration-admin/data",
            "/api/chairperson/data", "/api/management/data",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)


if __name__ == "__main__":
    unittest.main()
