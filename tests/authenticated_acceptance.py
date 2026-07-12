"""Manual production-like acceptance smoke; prompts securely for temporary credentials."""

import getpass

from fastapi.testclient import TestClient

from deploy.proxy import app


def check(response, label, allowed=(200,)):
    print(label, response.status_code, len(response.content))
    assert response.status_code in allowed, (label, response.status_code, response.text[:300])
    return response


def assert_page(response):
    if response.status_code != 200:
        return
    data = response.json()
    assert {"items", "page", "page_size", "total", "total_pages"} <= set(data)
    assert data["page_size"] == 20 and len(data["items"]) <= 20


def main():
    committee = getpass.getpass("committee user: ")
    committee_pw = getpass.getpass("committee password: ")
    admin_pw = getpass.getpass("organiser password: ")
    developer_pw = getpass.getpass("developer password: ")
    match_id = getpass.getpass("test match: ")
    judge_code = getpass.getpass("judge access code: ")
    review_pw = getpass.getpass("score review password: ")

    committee_client = TestClient(app)
    check(committee_client.post("/api/committee/login", json={"user_id": committee, "password": committee_pw}), "committee login")
    for path in ("/api/committee/me", "/api/vote/data", "/api/ai-coach/data", "/api/ai-training/data",
                 "/api/ai-fund/data", "/api/video-replay/data", "/api/match-photos/data"):
        check(committee_client.get(path), path)
    for path in ("/api/ai-fund/transactions?page=1", "/api/ai-fund/usage?page=1",
                 "/api/ai-training/collection/my-recordings?page=1", "/api/ai-training/collection/my-llm?page=1",
                 "/api/match-photos/photos?page=1"):
        assert_page(check(committee_client.get(path), path))
    for path in ("/api/ai-training/inventory", "/api/ai-training/coverage",
                 "/api/ai-training/export/recordings.zip", "/api/ai-training/export/llm.jsonl"):
        check(committee_client.get(path), path, (200, 403))

    admin = TestClient(app)
    check(admin.post("/api/registration-admin/login", json={"password": admin_pw}), "organiser login")
    for path in ("/api/registration-admin/data", "/api/chairperson/data", "/api/management/data", "/api/video-admin/data?page=1"):
        check(admin.get(path), path)

    developer = TestClient(app)
    check(developer.post("/api/developer/login", json={"password": developer_pw}), "developer login")
    check(developer.get("/api/developer/data"), "/api/developer/data")
    for kind in ("bugs", "accounts", "subscriptions"):
        assert_page(check(developer.get(f"/api/developer/collection/{kind}?page=1"), f"developer {kind}"))

    judge = TestClient(app)
    check(judge.post("/api/judging/login", json={"match_id": match_id, "password": judge_code}), "judging login")
    check(judge.get("/api/judging/state"), "judging state")

    review = TestClient(app)
    check(review.post("/api/review/login", json={"match_id": match_id, "password": review_pw}), "review login")
    check(review.get("/api/review/data"), "review data")
    print("AUTHENTICATED ACCEPTANCE SMOKE OK")


if __name__ == "__main__":
    main()
