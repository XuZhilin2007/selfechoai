from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import FunctionAIService


def test_authentication_pages_share_the_pwa_shell_and_unknown_route_is_404(
    client_factory,
) -> None:
    client = client_factory(FunctionAIService(lambda text, existing: None))

    for path in ["/", "/login", "/register", "/account"]:
        response = client.get(path)
        assert response.status_code == 200
        assert '<main id="app"' in response.text
        assert 'id="primary-navigation"' in response.text

    assert client.get("/this-route-does-not-exist").status_code == 404


def test_frontend_authentication_state_and_csrf_contract(client_factory) -> None:
    client = client_factory(FunctionAIService(lambda text, existing: None))
    frontend = client.get("/static/app.js").text

    assert 'api("/api/auth/me")' in frontend
    for state in ["loading", "authenticated", "unauthenticated", "network_error"]:
        assert f'"{state}"' in frontend
    for endpoint in [
        "/api/auth/register",
        "/api/auth/login",
        "/api/auth/logout",
    ]:
        assert endpoint in frontend
    for route in ["/login", "/register", "/account"]:
        assert route in frontend

    assert 'headers.set("X-CSRF-Token", csrfToken)' in frontend
    assert 'credentials: "same-origin"' in frontend
    assert "appElement.replaceChildren()" in frontend
    assert "if (!isKnownPath(path))" in frontend
    assert "sessionStorage.getItem(captureDraftStorageKey())" in frontend
    assert "A transient failure must not erase an already resolved user" in frontend


def test_service_worker_never_caches_api_responses_and_manifest_is_valid(
    client_factory,
) -> None:
    client = client_factory(FunctionAIService(lambda text, existing: None))

    service_worker = client.get("/service-worker.js").text
    shell_entries = service_worker.split("const SHELL = [", 1)[1].split("];", 1)[0]
    api_guard_index = service_worker.index('requestUrl.pathname.startsWith("/api/")')
    response_handler_index = service_worker.index("event.respondWith")
    assert "selfecho-ai-community-v0.5" in service_worker
    assert "/api/" not in shell_entries
    assert api_guard_index < response_handler_index

    manifest_response = client.get("/static/manifest.webmanifest")
    assert manifest_response.status_code == 200
    manifest = json.loads(manifest_response.text)
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["icons"]


def test_ui_phase2_scanning_continue_first_and_community_setup_contract(
    client_factory,
) -> None:
    client = client_factory(FunctionAIService(lambda text, existing: None))
    frontend = client.get("/static/app.js").text
    styles = client.get("/static/styles.css").text

    assert "现在值得关注的事" in frontend
    assert "已经完成的事" in frontend
    assert "回收站里的事" in frontend
    assert "dashboard-title-row" in frontend
    assert "card-signals" in frontend
    assert "card-meta" in frontend
    assert "高重要" in frontend
    assert "优先级待确认" in frontend

    detail_order = [
        frontend.index('class="page-heading detail-heading"'),
        frontend.index('class="detail-block continue-block"'),
        frontend.index('class="detail-block understanding-block"'),
        frontend.index('id="edit-item-button"'),
        frontend.index('class="detail-block lifecycle-block"'),
        frontend.index("查看原始记录"),
        frontend.index("更多信息与操作"),
    ]
    assert detail_order == sorted(detail_order)
    assert "又想到什么？" in frontend
    assert "补充原文会先保存。" in frontend
    assert "尚未保存，输入仍保留" in frontend
    assert "function renderSupplementalValue(value)" in frontend
    assert "supplementalLabels[normalizedKey]" in frontend
    assert 'comparison_notes: "对比考虑"' in frontend
    assert 'use_case: "使用场景"' in frontend
    assert 'preferences: "偏好"' in frontend
    assert 'normalizedKey.replaceAll("_", " ")' in frontend
    assert "lifecycle-primary" in frontend
    assert 'id="reprocess-status"' in frontend
    assert '<summary>直接修正字段</summary>' not in frontend
    assert 'id="edit-dialog"' in frontend
    assert 'aria-haspopup="dialog"' in frontend
    assert "editDialog.showModal()" in frontend
    assert "放弃尚未保存的字段修改吗？" in frontend
    assert 'document.querySelector("#edit-title").focus()' in frontend

    for selector in [
        ".dashboard-title-row",
        ".card-signal",
        ".card-meta",
        ".detail-view",
        ".continue-block",
        ".understanding-heading",
        ".edit-entry-button",
        ".edit-dialog",
        ".edit-surface",
        ".supplemental-grid",
        ".advanced-block",
    ]:
        assert selector in styles

    repository_root = Path(__file__).resolve().parents[1]
    readme = (repository_root / "README.md").read_text(encoding="utf-8")
    environment_example = (repository_root / ".env.example").read_text(
        encoding="utf-8"
    )
    for setting in [
        "APP_DATABASE_PATH",
        "AUTH_REGISTRATION_MODE",
        "AUTH_INVITE_CODE_HASH",
        "APP_ORIGIN",
        "AUTH_COOKIE_SECURE",
        "REMINDER_WORKER_ENABLED",
        "REMINDER_POLL_INTERVAL_SECONDS",
        "REMINDER_BATCH_SIZE",
        "REMINDER_SENDING_STALE_SECONDS",
        "WEB_PUSH_ENABLED",
        "WEB_PUSH_VAPID_PUBLIC_KEY",
        "WEB_PUSH_VAPID_PRIVATE_KEY",
        "WEB_PUSH_VAPID_SUBJECT",
        "WEB_PUSH_TIMEOUT_SECONDS",
        "WEB_PUSH_TEST_SEND_ENABLED",
    ]:
        assert setting in environment_example
    assert "WEB_PUSH_ENABLED=false" in environment_example
    assert "WEB_PUSH_TEST_SEND_ENABLED=false" in environment_example
    assert "REMINDER_WORKER_ENABLED=false" in environment_example
    assert "python -m app.bootstrap" in readme
    assert "hash_invite_code" in readme
    assert "http://127.0.0.1:8000" in readme
    assert "APP_ORIGIN=http://127.0.0.1:8000" in environment_example
    assert "AUTH_COOKIE_SECURE=false" in environment_example
    assert "data/personal_ai_inbox.db" not in readme
    assert "C:\\Users\\" not in readme
    assert not (repository_root / "docs" / "LOCAL_DEVELOPMENT.md").exists()
