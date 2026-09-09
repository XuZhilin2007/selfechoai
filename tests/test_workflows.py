from __future__ import annotations

import json
from datetime import date

from app.schemas import AIExtraction, AIItemFields, ImportantField
from app.services.ai import AIServiceError, DisabledAIService
from tests.conftest import FunctionAIService


def new_item_extraction(
    title: str,
    *,
    importance: str = "unknown",
    urgency: str = "unknown",
    deadline: date | None = None,
) -> AIExtraction:
    evidence = set()
    if importance != "unknown":
        evidence.add(ImportantField.IMPORTANCE)
    if urgency != "unknown":
        evidence.add(ImportantField.URGENCY)
    if deadline is not None:
        evidence.add(ImportantField.DEADLINE)
    return AIExtraction(
        fields=AIItemFields(
            title=title,
            type="study",
            importance=importance,
            urgency=urgency,
            deadline=deadline,
            status="active",
            next_action="继续补充信息",
        ),
        evidence_fields=evidence,
    )


def test_capture_creates_item_and_preserves_original_input(client_factory):
    ai = FunctionAIService(
        lambda text, existing: new_item_extraction("复习高数")
    )
    client = client_factory(ai)

    response = client.post(
        "/api/inputs",
        json={"original_text": "  有空复习高数  ", "input_method": "text"},
    )

    assert response.status_code == 202
    assert response.json()["original_text"] == "有空复习高数"
    dashboard = client.get("/api/items").json()
    assert dashboard["sortable_items"] == []
    assert [item["title"] for item in dashboard["needs_confirmation"]] == [
        "复习高数"
    ]

    item_id = dashboard["needs_confirmation"][0]["id"]
    detail = client.get(f"/api/items/{item_id}").json()
    assert detail["inputs"][0]["original_text"] == "有空复习高数"
    assert detail["inputs"][0]["processing_status"] == "succeeded"


def test_capture_response_confirms_persistence_before_ai_completion(client_factory):
    client = client_factory(
        FunctionAIService(lambda text, existing: new_item_extraction("合成测试事项"))
    )

    response = client.post(
        "/api/inputs",
        json={"original_text": "保存后再处理的合成原文", "input_method": "text"},
    )

    assert response.status_code == 202
    assert response.json()["processing_status"] == "pending"
    user_id = client.get("/api/auth/me").json()["id"]
    stored = client.app.state.repository.get_input(response.json()["id"], user_id)
    assert stored.original_text == "保存后再处理的合成原文"


def test_ai_failure_keeps_saved_input_and_retry_recovers(client_factory):
    calls = 0

    def flaky_handler(text, existing):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AIServiceError("temporary failure")
        return new_item_extraction("已恢复的事项")

    client = client_factory(FunctionAIService(flaky_handler))
    captured = client.post(
        "/api/inputs", json={"original_text": "不能丢失的原文"}
    )
    input_id = captured.json()["id"]

    failed_dashboard = client.get("/api/items").json()
    assert failed_dashboard["needs_confirmation"] == []
    assert failed_dashboard["failed_inputs"][0]["original_text"] == "不能丢失的原文"
    assert failed_dashboard["failed_inputs"][0]["processing_status"] == "failed"
    assert failed_dashboard["failed_inputs"][0]["failure_type"] == "internal"
    assert failed_dashboard["failed_inputs"][0]["failure_message"]

    retry = client.post(f"/api/inputs/{input_id}/retry")
    assert retry.status_code == 202
    recovered = client.get("/api/items").json()
    assert recovered["failed_inputs"] == []
    assert recovered["needs_confirmation"][0]["title"] == "已恢复的事项"


def test_missing_ai_configuration_is_visible_to_user(client_factory):
    client = client_factory(DisabledAIService())

    client.post("/api/inputs", json={"original_text": "配置检查"})
    failed = client.get("/api/items").json()["failed_inputs"][0]

    assert failed["failure_type"] == "configuration"
    assert "AI 未配置" in failed["failure_message"]
    assert "API_KEY" in failed["failure_message"]


def test_priority_formula_and_unknown_group_are_separate(client_factory):
    def handler(text, existing):
        if text == "高高":
            return new_item_extraction(
                "高紧急高重要", importance="high", urgency="high"
            )
        if text == "高中":
            return new_item_extraction(
                "高紧急中重要", importance="medium", urgency="high"
            )
        return new_item_extraction("信息不足")

    client = client_factory(FunctionAIService(handler))
    for text in ("高中", "未知", "高高"):
        assert client.post("/api/inputs", json={"original_text": text}).status_code == 202

    dashboard = client.get("/api/items").json()
    assert [item["title"] for item in dashboard["sortable_items"]] == [
        "高紧急高重要",
        "高紧急中重要",
    ]
    assert [item["priority_score"] for item in dashboard["sortable_items"]] == [
        3.0,
        2.65,
    ]
    assert dashboard["needs_confirmation"][0]["title"] == "信息不足"
    assert dashboard["needs_confirmation"][0]["priority_score"] is None


def test_quick_confirmation_contract_updates_unknown_fields_and_preserves_item(
    client_factory,
):
    extraction = AIExtraction(
        fields=AIItemFields(
            title="比较两种阅读方案",
            type="decision",
            importance="unknown",
            urgency="unknown",
            estimated_time=25,
            status="active",
            next_action="查看两种方案的资料",
            extra_information={
                "options": ["方案甲", "方案乙"],
                "constraint": "不影响现有安排",
            },
        ),
        evidence_fields=set(),
    )
    client = client_factory(FunctionAIService(lambda text, existing: extraction))
    captured = client.post(
        "/api/inputs",
        json={"original_text": "比较两种阅读方案，之后查看资料再决定。"},
    )
    assert captured.status_code == 202

    initial_queue = client.get("/api/items").json()["needs_confirmation"]
    assert len(initial_queue) == 1
    assert initial_queue[0]["importance"] == "unknown"
    assert initial_queue[0]["urgency"] == "unknown"
    item_id = initial_queue[0]["id"]
    initial_detail = client.get(f"/api/items/{item_id}").json()

    importance_update = client.patch(
        f"/api/items/{item_id}",
        json={"importance": "medium", "confirmed_important_fields": True},
    )
    assert importance_update.status_code == 200
    one_field_left = client.get("/api/items").json()["needs_confirmation"]
    assert len(one_field_left) == 1
    assert one_field_left[0]["importance"] == "medium"
    assert one_field_left[0]["urgency"] == "unknown"

    urgency_update = client.patch(
        f"/api/items/{item_id}",
        json={"urgency": "low", "confirmed_important_fields": True},
    )
    assert urgency_update.status_code == 200
    dashboard = client.get("/api/items").json()
    assert dashboard["needs_confirmation"] == []
    assert dashboard["sortable_items"][0]["id"] == item_id

    final_detail = client.get(f"/api/items/{item_id}").json()
    item = final_detail["item"]
    assert item["title"] == "比较两种阅读方案"
    assert item["type"] == "decision"
    assert item["estimated_time"] == 25
    assert item["status"] == "active"
    assert item["next_action"] == "查看两种方案的资料"
    assert item["extra_information"] == {
        "options": ["方案甲", "方案乙"],
        "constraint": "不影响现有安排",
    }
    assert final_detail["inputs"] == initial_detail["inputs"]


def test_unknown_ai_update_cannot_erase_known_priority(client_factory):
    def handler(text, existing):
        if existing is None:
            return new_item_extraction(
                "准备考试", importance="high", urgency="medium"
            )
        return AIExtraction(
            fields=AIItemFields(
                importance="unknown",
                extra_information={"progress": "第四章已复习"},
            ),
            evidence_fields={ImportantField.IMPORTANCE},
        )

    client = client_factory(FunctionAIService(handler))
    client.post("/api/inputs", json={"original_text": "这次考试很重要"})
    item_id = client.get("/api/items").json()["sortable_items"][0]["id"]

    update = client.post(
        f"/api/items/{item_id}/inputs",
        json={"original_text": "第四章已复习，但没说重要性"},
    )
    assert update.status_code == 202
    detail = client.get(f"/api/items/{item_id}").json()["item"]
    assert detail["importance"] == "high"
    assert detail["extra_information"] == {"progress": "第四章已复习"}


def test_context_survives_updates_and_is_returned_by_detail_api(client_factory):
    original_context = {
        "decision_context": "不确定现在购买还是等待",
        "concerns": ["担心之后涨价"],
        "constraints": ["预算充足", "购买时间不会影响正常安排"],
    }

    def handler(text, existing):
        if existing is None:
            return AIExtraction(
                fields=AIItemFields(
                    title="购买阅读设备",
                    type="purchase_decision",
                    importance="high",
                    urgency="low",
                    status="active",
                    next_action=None,
                    extra_information=original_context,
                ),
                evidence_fields={ImportantField.URGENCY},
            )
        assert existing.extra_information == original_context
        return AIExtraction(
            fields=AIItemFields(
                importance="high",
                next_action="比较现有候选型号的测评",
                extra_information={
                    "current_progress": "已经选出两个候选型号",
                    "options": ["候选A", "候选B"],
                },
            ),
            evidence_fields=set(),
        )

    client = client_factory(FunctionAIService(handler))
    original_text = (
        "我想买一个阅读设备，不算特别急，但担心之后涨价，不知道现在买还是等，"
        "预算充足，购买时间不会影响正常安排。"
    )
    captured = client.post("/api/inputs", json={"original_text": original_text})
    assert captured.status_code == 202

    dashboard_item = client.get("/api/items").json()["needs_confirmation"][0]
    assert dashboard_item["importance"] == "unknown"
    assert dashboard_item["urgency"] == "low"

    item_id = dashboard_item["id"]
    initial_detail = client.get(f"/api/items/{item_id}").json()
    assert initial_detail["item"]["extra_information"] == original_context
    assert initial_detail["item"]["next_action"] is None
    assert initial_detail["inputs"][0]["original_text"] == original_text

    update = client.post(
        f"/api/items/{item_id}/inputs",
        json={
            "original_text": "已经选出两个候选型号，接下来比较候选A和候选B的测评。"
        },
    )
    assert update.status_code == 202

    updated_item = client.get(f"/api/items/{item_id}").json()["item"]
    assert updated_item["importance"] == "unknown"
    assert updated_item["urgency"] == "low"
    assert updated_item["next_action"] == "比较现有候选型号的测评"
    assert updated_item["extra_information"] == {
        **original_context,
        "current_progress": "已经选出两个候选型号",
        "options": ["候选A", "候选B"],
    }


def test_failed_detail_update_can_be_retried(client_factory):
    update_attempts = 0

    def handler(text, existing):
        nonlocal update_attempts
        if existing is None:
            return new_item_extraction("渐进更新事项")
        update_attempts += 1
        if update_attempts == 1:
            raise AIServiceError("temporary update failure")
        return AIExtraction(
            fields=AIItemFields(next_action="重新处理成功"),
            evidence_fields=set(),
        )

    client = client_factory(FunctionAIService(handler))
    client.post("/api/inputs", json={"original_text": "先创建事项"})
    item_id = client.get("/api/items").json()["needs_confirmation"][0]["id"]

    failed = client.post(
        f"/api/items/{item_id}/inputs", json={"original_text": "补充失败后重试"}
    )
    input_id = failed.json()["id"]
    detail = client.get(f"/api/items/{item_id}").json()
    assert detail["inputs"][-1]["processing_status"] == "failed"

    assert client.post(f"/api/inputs/{input_id}/retry").status_code == 202
    recovered = client.get(f"/api/items/{item_id}").json()
    assert recovered["inputs"][-1]["processing_status"] == "succeeded"
    assert recovered["item"]["next_action"] == "重新处理成功"


def test_reprocess_updates_same_item_and_preserves_history_and_known_core_fields(
    client_factory,
):
    def handler(text, existing):
        if existing is None:
            return AIExtraction(
                fields=AIItemFields(
                    title="选择学习设备",
                    type="decision",
                    importance="high",
                    urgency="medium",
                    deadline=date(2026, 10, 15),
                    status="active",
                    extra_information={"existing_note": "保留这条背景"},
                ),
                evidence_fields={
                    ImportantField.IMPORTANCE,
                    ImportantField.URGENCY,
                    ImportantField.DEADLINE,
                },
            )
        assert "Item Input 1: 比较两种学习设备" in text
        return AIExtraction(
            fields=AIItemFields(
                title="模型试图改标题",
                type="other",
                importance="low",
                urgency="high",
                deadline=date(2026, 9, 1),
                status="completed",
                next_action="查看两种设备的续航测试",
                extra_information={
                    "options": ["设备甲", "设备乙"],
                    "uncertainty": "尚未确定更看重便携还是续航",
                },
            ),
            evidence_fields={
                ImportantField.IMPORTANCE,
                ImportantField.URGENCY,
                ImportantField.DEADLINE,
            },
        )

    client = client_factory(FunctionAIService(handler))
    client.post("/api/inputs", json={"original_text": "比较两种学习设备"})
    original = client.get("/api/items").json()["sortable_items"][0]
    item_id = original["id"]
    history_before = client.get(f"/api/items/{item_id}").json()["inputs"]

    response = client.post(f"/api/items/{item_id}/reprocess")

    assert response.status_code == 200
    item = response.json()
    assert item["id"] == item_id
    assert item["title"] == "选择学习设备"
    assert item["type"] == "decision"
    assert item["status"] == "active"
    assert item["importance"] == "high"
    assert item["urgency"] == "medium"
    assert item["deadline"] == "2026-10-15"
    assert item["next_action"] == "查看两种设备的续航测试"
    assert item["extra_information"] == {
        "existing_note": "保留这条背景",
        "options": ["设备甲", "设备乙"],
        "uncertainty": "尚未确定更看重便携还是续航",
    }
    detail_after = client.get(f"/api/items/{item_id}").json()
    assert detail_after["inputs"] == history_before
    user_id = client.get("/api/auth/me").json()["id"]
    assert len(client.app.state.repository.list_active_items(user_id)) == 1


def test_reprocess_can_fill_unknown_core_fields_with_supported_evidence(
    client_factory,
):
    def handler(text, existing):
        if existing is None:
            return new_item_extraction("准备合成演示")
        return AIExtraction(
            fields=AIItemFields(
                importance="medium",
                urgency="low",
                deadline=date(2026, 11, 20),
                extra_information={"goal": "完成一次演示"},
            ),
            evidence_fields={
                ImportantField.IMPORTANCE,
                ImportantField.URGENCY,
                ImportantField.DEADLINE,
            },
        )

    client = client_factory(FunctionAIService(handler))
    client.post("/api/inputs", json={"original_text": "准备合成演示"})
    item_id = client.get("/api/items").json()["needs_confirmation"][0]["id"]

    item = client.post(f"/api/items/{item_id}/reprocess").json()

    assert item["importance"] == "medium"
    assert item["urgency"] == "low"
    assert item["deadline"] == "2026-11-20"
    assert item["extra_information"] == {"goal": "完成一次演示"}


def test_direct_important_changes_require_confirmation_and_delete_requires_trash(
    client_factory,
):
    client = client_factory(
        FunctionAIService(
            lambda text, existing: new_item_extraction(
                "确认规则", importance="high", urgency="medium"
            )
        )
    )
    client.post("/api/inputs", json={"original_text": "很重要"})
    item_id = client.get("/api/items").json()["sortable_items"][0]["id"]

    unconfirmed = client.patch(
        f"/api/items/{item_id}", json={"importance": "low"}
    )
    assert unconfirmed.status_code == 409
    confirmed = client.patch(
        f"/api/items/{item_id}",
        json={"importance": "low", "confirmed_important_fields": True},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["importance"] == "low"

    assert client.delete(f"/api/items/{item_id}").status_code == 409
    moved = client.patch(
        f"/api/items/{item_id}", json={"status": "trash"}
    )
    assert moved.status_code == 200
    assert moved.json()["status"] == "trash"

    restored = client.patch(
        f"/api/items/{item_id}", json={"status": "active"}
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    assert client.delete(f"/api/items/{item_id}").status_code == 409

    moved_again = client.patch(
        f"/api/items/{item_id}", json={"status": "trash"}
    )
    assert moved_again.status_code == 200
    assert client.delete(f"/api/items/{item_id}").status_code == 204
    assert client.get(f"/api/items/{item_id}").status_code == 404


def test_dashboard_filters_lifecycle_statuses_and_defaults_to_active(
    client_factory,
):
    client = client_factory(
        FunctionAIService(
            lambda text, existing: new_item_extraction(
                text, importance="medium", urgency="medium"
            )
        )
    )
    titles = ["保留当前", "完成事项", "回收事项"]
    for title in titles:
        response = client.post("/api/inputs", json={"original_text": title})
        assert response.status_code == 202

    active_items = client.get("/api/items").json()["sortable_items"]
    item_ids = {item["title"]: item["id"] for item in active_items}
    for title, status in {
        "完成事项": "completed",
        "回收事项": "trash",
    }.items():
        response = client.patch(
            f"/api/items/{item_ids[title]}", json={"status": status}
        )
        assert response.status_code == 200

    expected_by_status = {
        "active": {"保留当前"},
        "completed": {"完成事项"},
        "trash": {"回收事项"},
    }
    default_dashboard = client.get("/api/items").json()
    assert {item["title"] for item in default_dashboard["sortable_items"]} == {
        "保留当前"
    }
    item_parameters = client.get("/openapi.json").json()["paths"]["/api/items"][
        "get"
    ]["parameters"]
    assert any(
        parameter["name"] == "status" and parameter["in"] == "query"
        for parameter in item_parameters
    )
    assert client.get("/api/items", params={"status": "archived"}).status_code == 422

    for status, expected_titles in expected_by_status.items():
        dashboard = client.get("/api/items", params={"status": status}).json()
        returned_items = dashboard["sortable_items"] + dashboard["needs_confirmation"]
        assert {item["title"] for item in returned_items} == expected_titles
        if status != "active":
            assert dashboard["pending_inputs"] == []
            assert dashboard["failed_inputs"] == []

    restored = client.patch(
        f"/api/items/{item_ids['回收事项']}", json={"status": "active"}
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    assert client.get("/api/items", params={"status": "trash"}).json()[
        "sortable_items"
    ] == []
    active_after_restore = client.get("/api/items").json()
    assert {
        item["title"]
        for item in active_after_restore["sortable_items"]
        + active_after_restore["needs_confirmation"]
    } == {"保留当前", "回收事项"}


def test_pwa_routes_and_health_start(client_factory):
    client = client_factory(
        FunctionAIService(lambda text, existing: new_item_extraction("测试"))
    )
    assert client.get("/api/health").json() == {"message": "ok"}
    shell = client.get("/capture").text
    assert "SelfEcho" in shell
    assert client.get("/openapi.json").json()["info"]["title"] == "SelfEcho AI"
    manifest_response = client.get("/static/manifest.webmanifest")
    assert manifest_response.status_code == 200
    manifest = json.loads(manifest_response.text)
    assert manifest["name"] == "SelfEcho AI"
    assert manifest["short_name"] == "SelfEcho"
    service_worker = client.get("/service-worker.js")
    assert service_worker.status_code == 200
    assert service_worker.headers["content-type"].startswith("text/javascript")
    assert service_worker.headers["cache-control"] == "no-cache"
    assert "selfecho-ai-community-v0.6" in service_worker.text
    frontend = client.get("/static/app.js").text
    assert "<h1>先记下来</h1>" in frontend
    assert 'class="visually-hidden" for="capture-text">记录内容</label>' in frontend
    assert 'class="primary-button capture-submit" type="submit">保存</button>' in frontend
    assert "把脑中的事情原样写下" not in frontend
    assert "这次想到什么？" not in frontend
    assert 'button.textContent = "保存中…"' in frontend
    assert "尚未保存，输入仍保留" in frontend
    assert "✓ 已保存" in frontend
    assert "AI 正在后台整理" in frontend
    assert "重新整理" in frontend
    assert "永久删除" in frontend
    assert "window.confirm" in frontend
    assert "待快速确认" in frontend
    assert "priority-choice-button" in frontend
    assert "confirmed_important_fields: true" in frontend
    assert 'register("/service-worker.js", { scope: "/" })' in frontend
    assert "function detailRefreshBlocked()" in frontend
    assert "automatic && detailRefreshBlocked()" in frontend
    assert 'form.dataset.dirty = "true"' in frontend
    assert '"#trash-confirm-dialog[open], #edit-dialog[open]"' in frontend
    assert "状态已更新；当前未提交输入已保留。" in frontend
    assert 'const selectedStatus = selectedDashboardStatus();' in frontend
    assert 'api(`/api/items?status=${encodeURIComponent(selectedStatus)}`)' in frontend
    for label in ["当前", "已完成", "回收站"]:
        assert label in frontend
    assert "archived" not in frontend
    assert "已归档" not in frontend
    assert "确定将这个事项移入回收站吗？之后仍可以从回收站恢复。" in frontend
    assert 'value="cancel"' in frontend
    assert 'value="confirm"' in frontend
    lifecycle_handler = frontend.split(
        'document.querySelectorAll(".lifecycle-status-button")', 1
    )[1].split('const deleteButton = document.querySelector', 1)[0]
    confirmation_guard = (
        'if (nextStatus === "trash" && !(await confirmTrashMove())) return;'
    )
    assert confirmation_guard in lifecycle_handler
    assert lifecycle_handler.index(confirmation_guard) < lifecycle_handler.index(
        "button.disabled = true"
    )
    assert lifecycle_handler.index(confirmation_guard) < lifecycle_handler.index(
        'api(`/api/items/${itemId}`'
    )
    styles = client.get("/static/styles.css").text
    assert "#capture-text" in styles
    assert "--shell-width: 920px" in styles
    assert ".capture-view" in styles
    assert ":where(a, button):focus-visible" in styles
    assert "min-height: 48px" in styles
    assert "safe-area-inset-bottom" in styles
    assert ".lifecycle-navigation" in styles
    assert ".confirmation-dialog" in styles
    quick_controls = frontend.split("function quickPriorityButtons", 1)[1].split(
        "function quickConfirmationCard",
        1,
    )[0]
    assert "<button" in quick_controls
    assert "<select" not in quick_controls
