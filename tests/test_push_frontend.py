from __future__ import annotations

from tests.conftest import FunctionAIService


def test_browser_push_permission_and_subscription_contract(client_factory):
    client = client_factory(FunctionAIService(lambda text, existing: None))
    frontend = client.get("/static/app.js").text

    assert frontend.count("Notification.requestPermission()") == 1
    enable_flow = frontend.split(
        "async function enableDeviceNotifications()",
        1,
    )[1].split("function dismissNotificationOnboarding()", 1)[0]
    assert "Notification.requestPermission()" in enable_flow
    assert "pushManager.subscribe" in enable_flow
    assert "unsubscribeBrowserSubscription" in enable_flow
    assert "pushSubscriptionUsesVapidKey" in enable_flow
    assert "userVisibleOnly: true" in enable_flow
    assert "applicationServerKey" in enable_flow

    cold_start = frontend.split(
        "async function initializePushForAuthenticatedUser()",
        1,
    )[1].split("async function enableDeviceNotifications()", 1)[0]
    assert "pushManager.getSubscription()" in cold_start
    assert "Notification.requestPermission" not in cold_start
    assert 'status: "reconnect_required"' in cold_start
    assert "syncWithAccountSwitchRecovery" in cold_start

    for endpoint in [
        "/api/push/config",
        "/api/push/subscriptions",
        "/api/push/subscriptions/current",
    ]:
        assert endpoint in frontend
    assert 'api("/api/push/test"' not in frontend
    assert "pushOperationPromise" in frontend
    assert "runPushSingleFlight" in frontend
    assert "PUSH_CLEANUP_TIMEOUT_MS" in frontend
    assert "AbortController" in frontend


def test_notification_account_surface_disable_and_logout_contract(client_factory):
    client = client_factory(FunctionAIService(lambda text, existing: None))
    frontend = client.get("/static/app.js").text
    styles = client.get("/static/styles.css").text

    for copy in [
        "让 SelfEcho 在需要的时候提醒你",
        "开启此设备通知",
        "此设备通知",
        "停用此设备通知",
        "此设备通知已停用",
        "重试停用",
        "浏览器已拒绝通知",
        "当前连接不支持系统通知",
        "系统通知当前不可用",
        "需要重新连接",
    ]:
        assert copy in frontend

    logout_flow = frontend.split("async function performLogout()", 1)[1].split(
        "function setAuthentication",
        1,
    )[0]
    assert logout_flow.index("cleanupPushBeforeLogout()") < logout_flow.index(
        'api("/api/auth/logout"'
    )
    assert "server logout remains authoritative" in logout_flow
    assert "completeLocalLogout()" in logout_flow

    assert "notificationDisableRequested()" in frontend
    assert "performDisableDeviceNotifications" in frontend
    assert "writePushPreference(notificationDisabledStorageKey(), true)" in frontend
    assert ".notification-device-settings" in styles
    assert ".notification-onboarding" in styles


def test_account_switch_recovery_is_bounded_and_never_persists_subscription_secrets(
    client_factory,
):
    client = client_factory(FunctionAIService(lambda text, existing: None))
    frontend = client.get("/static/app.js").text

    recovery = frontend.split(
        "async function syncWithAccountSwitchRecovery(",
        1,
    )[1].split("async function apiWithTimeout", 1)[0]
    assert "error.status !== 422" in recovery
    assert recovery.count("replaceAndSyncSubscriptionOnce(") == 1
    assert "while" not in recovery
    assert "for (" not in recovery

    storage_writes = [
        line.strip()
        for line in frontend.splitlines()
        if "localStorage.setItem" in line or "sessionStorage.setItem" in line
    ]
    assert all("endpoint" not in line for line in storage_writes)
    assert all("p256dh" not in line for line in storage_writes)
    assert all("auth" not in line.lower() for line in storage_writes)
    assert "console.log" not in frontend
    assert "console.warn" not in frontend


def test_service_worker_push_click_and_cache_privacy_contract(client_factory):
    client = client_factory(FunctionAIService(lambda text, existing: None))
    service_worker = client.get("/service-worker.js").text

    assert "selfecho-ai-community-v0.5" in service_worker
    assert 'addEventListener("push"' in service_worker
    assert 'addEventListener("notificationclick"' in service_worker
    assert "registration.showNotification" in service_worker
    assert "safeNotificationTargetPath" in service_worker
    assert 'requestUrl.pathname.startsWith("/api/")' in service_worker

    push_contract = service_worker.split("function parsePushNotification", 1)[1].split(
        'self.addEventListener("fetch"',
        1,
    )[0]
    for forbidden in [
        "item_id",
        "reminder_id",
        "raw_capture",
        "email",
        "localStorage",
        "indexedDB",
        "caches.",
        "/api/",
    ]:
        assert forbidden not in push_contract
