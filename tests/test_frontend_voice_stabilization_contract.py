from __future__ import annotations

from tests.conftest import FunctionAIService


def test_discard_button_state_is_derived_from_capture_state(client_factory) -> None:
    client = client_factory(FunctionAIService(lambda text, existing: None))
    frontend = client.get("/static/app.js").text

    assert "discardPending: false," in frontend
    assert "function captureDiscardControl(" in frontend
    assert "async function runWithCaptureDiscardPending(" in frontend

    discard_flow = frontend.split('discardButton.addEventListener("click"', 2)[1].split(
        "const visibilityHandler", 1
    )[0]
    assert "if (state.discardPending) return;" in discard_flow
    assert "runWithCaptureDiscardPending({" in discard_flow
    assert "discardButton.disabled" not in discard_flow

    pending_helper = frontend.split(
        "async function runWithCaptureDiscardPending", 2
    )[1].split("function voiceSegmentStatusSnapshot", 1)[0]
    assert "if (state.discardPending) return false;" in pending_helper
    assert "state.discardPending = true;" in pending_helper
    assert "finally {" in pending_helper
    assert "state.discardPending = false;" in pending_helper

    controls_flow = frontend.split("function updateCaptureControls", 2)[1].split(
        "function scheduleDraftPoll", 1
    )[0]
    assert "captureDiscardControl({" in controls_flow
    assert "discardButton.hidden = discardControl.hidden;" in controls_flow
    assert "discardButton.disabled = discardControl.disabled;" in controls_flow


def test_voice_failure_copy_uses_layered_truthful_hierarchy(client_factory) -> None:
    client = client_factory(FunctionAIService(lambda text, existing: None))
    frontend = client.get("/static/app.js").text

    assert 'failed: "转写未完成"' in frontend
    assert "这段录音没有得到可用文字。原始录音已保留。" in frontend
    assert "function voiceSegmentFailureCopy(segment)" in frontend
    for safe_code in ["media_probe", "unsupported_media", "conversion", "draft_text_limit"]:
        assert f'"{safe_code}",' in frontend
    assert "voiceSegmentFailureCopy(segment)" in frontend
    assert "escapeHtml(segment.failure_message" not in frontend
    assert "语音转写服务" not in frontend

    assert 'setCaptureStatus("请先处理未完成的录音，再保存。", "error");' in frontend
    assert "转写未完成：原始录音已保留。请播放、重试或删除。" not in frontend
    assert "voice-segment-retry" in frontend
    assert "voice-segment-delete" in frontend
    assert 'api(`/api/voice-segments/${segmentId}/retry`' in frontend
    assert 'method: "POST"' in frontend
