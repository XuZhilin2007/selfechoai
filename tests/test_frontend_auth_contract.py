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
    assert ".then((registration) => registration.update())" in frontend


def test_service_worker_never_caches_api_responses_and_manifest_is_valid(
    client_factory,
) -> None:
    client = client_factory(FunctionAIService(lambda text, existing: None))

    service_worker = client.get("/service-worker.js").text
    shell_entries = service_worker.split("const SHELL = [", 1)[1].split("];", 1)[0]
    api_guard_index = service_worker.index('requestUrl.pathname.startsWith("/api/")')
    response_handler_index = service_worker.index("event.respondWith")
    assert "selfecho-ai-community-v0.8.0-ui-1" in service_worker
    assert '"/static/app.js?v=0.8.0-community-ui-1"' in shell_entries
    assert '"/static/styles.css?v=0.8.0-community-ui-1"' in shell_entries
    assert "public-security-filing" not in service_worker
    assert "/api/" not in shell_entries
    assert api_guard_index < response_handler_index

    manifest_response = client.get("/static/manifest.webmanifest")
    assert manifest_response.status_code == 200
    manifest = json.loads(manifest_response.text)
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["icons"]


def test_v07_quiet_utility_structure_and_community_setup_contract(
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
    assert "高重要" not in frontend
    assert "优先级待确认" not in frontend
    assert "已过期" in frontend

    detail_renderer = frontend.split("async function renderDetail", 1)[1].split(
        "function renderRoute", 1
    )[0]
    detail_order = [
        detail_renderer.index('class="page-heading detail-heading"'),
        detail_renderer.index('class="detail-content"'),
        detail_renderer.index('class="detail-block understanding-block"'),
        detail_renderer.index('id="edit-item-button"'),
        detail_renderer.index("${reminderDetailSection(item, data.reminder)}"),
        detail_renderer.index('class="detail-block continue-block"'),
        detail_renderer.index("原始记录（"),
        detail_renderer.index("更多信息与操作"),
        detail_renderer.index('class="detail-block lifecycle-block"'),
    ]
    assert detail_order == sorted(detail_order)
    assert "继续记录" in frontend
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
    assert 'class="capture-panel" aria-label="记录内容"' in frontend
    assert 'class="panel capture-panel"' not in frontend
    assert 'class="account-panel"' in frontend
    assert 'class="panel account-panel"' not in frontend
    assert 'class="settings-section profile-settings"' in frontend
    assert 'class="settings-section notification-device-settings"' in frontend

    for selector in [
        ".dashboard-title-row",
        ".card-signal",
        ".card-meta",
        ".detail-view",
        ".detail-content",
        ".continue-block",
        ".understanding-heading",
        ".edit-entry-button",
        ".detail-block",
        ".edit-surface",
        ".supplemental-grid",
        ".advanced-block",
        ".settings-section",
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
        "EMAIL_REMINDER_PROVIDER_ENABLED",
        "TENCENT_SES_REGION",
        "TENCENTCLOUD_SECRET_ID",
        "TENCENTCLOUD_SECRET_KEY",
        "TENCENT_SES_FROM_EMAIL_ADDRESS",
        "TENCENT_SES_VERIFICATION_TEMPLATE_ID",
        "TENCENT_SES_REMINDER_TEMPLATE_ID",
        "TENCENT_SES_TEST_TEMPLATE_ID",
        "TENCENT_SES_TIMEOUT_SECONDS",
        "EMAIL_VERIFICATION_CODE_PEPPER",
        "VOICE_ASR_ENABLED",
        "VOICE_STORAGE_ROOT",
        "VOICE_MAX_UPLOAD_BYTES",
        "FFPROBE_PATH",
        "FFMPEG_PATH",
        "ALIBABA_ASR_API_URL",
        "ALIBABA_API_KEY",
        "VOICE_ASR_TIMEOUT_SECONDS",
    ]:
        assert setting in environment_example
    assert "WEB_PUSH_ENABLED=false" in environment_example
    assert "WEB_PUSH_TEST_SEND_ENABLED=false" in environment_example
    assert "EMAIL_REMINDER_PROVIDER_ENABLED=false" in environment_example
    assert "REMINDER_WORKER_ENABLED=false" in environment_example
    assert "VOICE_ASR_ENABLED=false" in environment_example
    assert "VOICE_MAX_UPLOAD_BYTES=16777216" in environment_example
    assert "VOICE_STORAGE_ROOT=" in environment_example
    assert "ALIBABA_API_KEY=" in environment_example
    assert "python -m app.bootstrap" in readme
    assert "hash_invite_code" in readme
    assert "http://127.0.0.1:8000" in readme
    assert "APP_ORIGIN=http://127.0.0.1:8000" in environment_example
    assert "AUTH_COOKIE_SECURE=false" in environment_example
    assert "python -m app.migrations.v005_voice_capture" in readme
    assert "python -m app.migrations.v006_email_reminders" in readme
    assert "python -m app.migrations.v007_item_lifecycle" in readme
    assert "python -m app.migrations.v008_item_pin" in readme
    assert "MIGRATE PUBLIC V5 TO V6" in readme
    assert "MIGRATE PUBLIC V6 TO V7" in readme
    assert "MIGRATE PUBLIC V7 TO V8" in readme
    assert "--check-only" in readme
    assert "qwen-audio-3.0-asr-flash" in readme
    assert "does not bundle or redistribute ffmpeg/ffprobe" in readme
    assert "limited real-device validation" in readme
    assert "data/personal_ai_inbox.db" not in readme
    assert "C:\\Users\\" not in readme
    assert not (repository_root / "docs" / "LOCAL_DEVELOPMENT.md").exists()
