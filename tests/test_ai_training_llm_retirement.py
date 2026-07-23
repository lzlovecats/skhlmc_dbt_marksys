"""Contracts for retiring the database-backed LLM submission workspace."""

import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_database_backed_llm_submission_ui_and_api_are_retired():
    page = (ROOT / "frontend" / "ai_training" / "index.html").read_text("utf-8")
    script = (ROOT / "frontend" / "ai_training" / "app.js").read_text("utf-8")
    api = (ROOT / "api" / "ai_training_api.py").read_text("utf-8")
    shared_tables = (
        ROOT / "frontend" / "shared" / "server-tables.js"
    ).read_text("utf-8")
    proxy = (ROOT / "deploy" / "proxy.py").read_text("utf-8")

    assert 'data-pane="llm"' not in page
    assert 'id="llmForm"' not in page
    assert 'data-admin="llm-review"' not in page
    assert 'id="llmExport"' not in page
    assert "/api/ai-training/llm" not in script
    assert "/api/ai-training/admin/submissions" not in script
    assert "/api/ai-training/export/llm.jsonl" not in script
    assert '@router.post("/llm")' not in api
    assert '@router.delete("/llm/{submission_id}")' not in api
    assert '@router.get("/admin/submissions")' not in api
    assert '@router.get("/export/llm.jsonl")' not in api
    assert '@router.post("/llm/{submission_id}/review")' not in api
    assert '@router.post("/rag/reindex")' not in api
    assert '"my-llm"' not in shared_tables
    assert '"/api/ai-training/llm"' not in proxy
    assert '"/api/ai-training/rag/reindex"' not in proxy
    assert '<script src="/shared/vote-ui.js?v=__APP_VERSION__"></script>' in page


def test_bootstrap_and_runtime_no_longer_depend_on_retired_table():
    schema = (ROOT / "schema.py").read_text("utf-8")
    api = (ROOT / "api" / "ai_training_api.py").read_text("utf-8")
    rag = (ROOT / "core" / "rag.py").read_text("utf-8")

    assert "TABLE_LLM_TRAINING_SUBMISSIONS" not in schema
    assert "TABLE_LLM_TRAINING_SUBMISSIONS" not in api
    assert "llm_training_submissions" not in rag


def test_retirement_migration_drops_private_data_and_has_structural_rollback():
    migration = ROOT / "migrations" / "20260723_0003_retire_llm_submissions"
    up = migration.with_suffix(".up.sql").read_text("utf-8")
    down = migration.with_suffix(".down.sql").read_text("utf-8")

    assert "DELETE FROM public.ai_training_audit" in up
    assert "target_type='llm_submission'" in up
    assert "DROP TABLE public.llm_training_submissions" in up
    assert "CREATE TABLE public.llm_training_submissions" in down
    assert "REVOKE ALL PRIVILEGES" in down
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in down
    assert "does not restore retired submission rows" in down


def test_sft_filename_is_documented_as_recommendation_not_loader_contract():
    page = (ROOT / "frontend" / "ai_training" / "index.html").read_text("utf-8")
    manual = (ROOT / "assets" / "user_manual.md").read_text("utf-8")

    assert "<code>sft.jsonl</code>" in page
    assert "建議檔名" in page
    assert "`sft.jsonl`" in manual
    assert "目前未有 Workstation SFT 匯入器" in manual


def test_server_paging_does_not_let_an_old_filter_response_replace_a_new_one():
    source = (ROOT / "frontend" / "shared" / "vote-ui.js").read_text()
    harness = f"""
global.window = global;
global.location = {{origin: "https://example.test"}};
global.MutationObserver = class {{
  disconnect() {{}}
  observe() {{}}
}};
global.document = {{
  addEventListener() {{}},
  createElement() {{ return {{className: "", setAttribute() {{}}, addEventListener() {{}}}}; }}
}};
const pending = [];
global.fetch = url => new Promise((resolve, reject) =>
  pending.push({{url: String(url), resolve, reject}})
);
eval({json.dumps(source)});
const element = {{
  innerHTML: "",
  nextElementSibling: null,
  insertAdjacentElement() {{}},
}};
const response = value => ({{
  ok: true,
  json: async () => ({{items: [value], page: 1, total: 1, total_pages: 1}}),
}});
(async () => {{
  const first = VoteUI.serverPaged(element, "/old", rows => rows[0]);
  const second = VoteUI.serverPaged(element, "/new", rows => rows[0]);
  pending[1].resolve(response("new"));
  await second;
  pending[0].resolve(response("old"));
  await first;
  if (element.innerHTML !== "new") throw new Error(`stale render: ${{element.innerHTML}}`);
  const oldFailure = VoteUI.serverPaged(element, "/old-failure", rows => rows[0]);
  const newest = VoteUI.serverPaged(element, "/newest", rows => rows[0]);
  pending[3].resolve(response("newest"));
  await newest;
  pending[2].reject(new Error("stale network error"));
  await oldFailure;
  if (element.innerHTML !== "newest") throw new Error("stale failure escaped");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""

    result = subprocess.run(
        ["node", "-e", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
