from __future__ import annotations

from tests.conftest import FunctionAIService


def test_manual_reminder_quick_choice_and_native_date_time_controls(client_factory):
    client = client_factory(FunctionAIService(lambda text, existing: None))
    frontend = client.get("/static/app.js").text

    assert "需要提醒吗？" in frontend
    assert "不用" in frontend
    assert "设置提醒" in frontend
    for option in ["明天", "后天", "大后天", "选日期"]:
        assert option in frontend
    assert 'id="reminder-date" type="date"' in frontend
    assert 'id="reminder-time" type="time"' in frontend
    assert 'min="${profileToday()}"' in frontend
    assert "今天是 ${formatDate(profileToday())}" in frontend
    assert "authentication.user.default_reminder_time" in frontend
    assert "改时间" in frontend
    assert "openReminderEditor" in frontend
    assert "/reminder-prompt/dismiss" in frontend
    assert "你想之后被提醒，但还没有确定时间。" in frontend
    assert 'item.reminder?.status === "needs_confirmation"' in frontend
    assert "暂时不用提醒" in frontend
    assert "reminder-decline-button" in frontend
    assert 'api(`/api/reminders/${item.reminder.id}`' in frontend
    assert 'method: "DELETE"' in frontend
    assert "原话里的时间：" in frontend


def test_dashboard_detail_and_account_reminder_contract(client_factory):
    client = client_factory(FunctionAIService(lambda text, existing: None))
    frontend = client.get("/static/app.js").text
    styles = client.get("/static/styles.css").text

    assert "function reminderNaturalText(reminder)" in frontend
    assert "function reminderCardText(reminder)" in frontend
    assert "function inAppReminderList(reminders)" in frontend
    assert 'id="capture-micro-reminders"' in frontend
    assert "captureReminders.innerHTML = inAppReminderList(data.due_reminders)" in frontend
    assert "提醒一下：你之前希望现在记起这件事：" in frontend
    assert "markRenderedRemindersSurfaced(data.due_reminders)" in frontend
    assert 'api(`/api/reminders/${reminder.id}/surface`' in frontend
    assert "这只表示 SelfEcho 已把它重新带回视野。" in frontend
    assert "知道了" not in frontend

    assert "function reminderDetailSection(item, reminder)" in frontend
    assert "修改时间" in frontend
    assert "关闭提醒" in frontend
    assert "再次设置提醒" in frontend
    assert "这个微提醒已关闭；记录仍保留。" in frontend
    assert 'method: "DELETE"' in frontend

    assert "无具体时间时默认提醒" in frontend
    assert 'api("/api/reminder-settings"' in frontend
    assert "default_reminder_time: defaultTime" in frontend

    for selector in [
        ".quick-reminder-choice",
        ".reminder-dialog",
        ".reminder-date-options",
        ".reminder-time-summary",
        ".micro-reminder",
        ".reminder-detail-block",
        ".reminder-settings-form",
    ]:
        assert selector in styles


def test_in_app_surface_is_recorded_only_after_dashboard_markup_is_rendered(
    client_factory,
):
    client = client_factory(FunctionAIService(lambda text, existing: None))
    frontend = client.get("/static/app.js").text
    dashboard = frontend.split("async function renderDashboard", 1)[1].split(
        "function detailField",
        1,
    )[0]

    render_index = dashboard.index("appElement.innerHTML = `")
    surface_index = dashboard.index(
        "markRenderedRemindersSurfaced(data.due_reminders)",
    )
    assert render_index < surface_index
    assert 'data-reminder-id="${reminder.id}"' in frontend

    micro_reminder = frontend.split("function inAppReminderList", 1)[1].split(
        "function markRenderedRemindersSurfaced",
        1,
    )[0]
    for pressure_word in ["⚠️", "已逾期", "未完成", "紧急", "你还没有做"]:
        assert pressure_word not in micro_reminder
