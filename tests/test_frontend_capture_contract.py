from __future__ import annotations

from pathlib import Path

from tests.conftest import FunctionAIService


def _frontend(client_factory) -> tuple[str, str]:
    client = client_factory(FunctionAIService(lambda text, existing: None))
    return (
        client.get("/static/app.js").text,
        client.get("/static/styles.css").text,
    )


def test_capture_uses_durable_draft_state_machine_without_legacy_double_submit(
    client_factory,
) -> None:
    frontend, _ = _frontend(client_factory)
    capture = frontend.split("function captureDraftStorageKey", 1)[1].split(
        "function inputStatusCard",
        1,
    )[0]

    for phase in [
        "BOOTSTRAPPING",
        "OPEN",
        "DIRTY",
        "REQUESTING_MIC",
        "RECORDING",
        "CANCEL_ARMED",
        "FINALIZING",
        "UPLOADING",
        "POLLING",
        "SAVING",
        "REVISION_CONFLICT",
    ]:
        assert f'{phase}: "{phase}"' in frontend

    assert 'api("/api/capture-draft")' in capture
    assert 'method: "PUT"' in capture
    assert 'api("/api/capture-draft/save"' in capture
    assert 'draft_id: state.draft.id' in capture
    assert 'revision: state.draft.revision' in capture
    assert 'api("/api/inputs"' not in capture
    assert "CAPTURE_AUTOSAVE_DELAY_MS = 700" in frontend
    assert "CAPTURE_STATUS_POLL_DELAY_MS = 800" in frontend
    assert "state.flushPromise" in capture
    assert "textarea.value === snapshot" in capture
    assert (
        "JSON.stringify({ current_text: snapshot, revision: expectedRevision })"
        in capture
    )


def test_capture_buffer_is_user_namespaced_exact_text_only_and_conflict_safe(
    client_factory,
) -> None:
    frontend, _ = _frontend(client_factory)
    storage = frontend.split("function captureDraftStorageKey", 1)[1].split(
        "function voiceGestureCancelArmed",
        1,
    )[0]
    capture = frontend.split("function renderCapture", 1)[1].split(
        "function inputStatusCard",
        1,
    )[0]

    assert "`${CAPTURE_DRAFT_KEY}.${authentication.user.id}`" in storage
    assert "sessionStorage.getItem(captureDraftStorageKey())" in storage
    assert "sessionStorage.setItem(key, JSON.stringify" in storage
    assert "text: value" in storage
    assert "draftId" in storage
    assert "revision" in storage
    assert "savePending" in storage
    assert "Blob" not in storage
    assert "localStorage" not in storage
    assert "serverDraft.current_text === localText" in frontend
    assert "conflict-keep-local" in capture
    assert "conflict-use-server" in capture
    assert "服务器 Draft 未被自动覆盖" in capture
    assert "textarea.disabled = state.savePending || hasActive || transient" in capture


def test_press_hold_recording_upload_and_cleanup_contract(client_factory) -> None:
    frontend, _ = _frontend(client_factory)
    voice = frontend.split("async function beginVoiceGesture", 1)[1].split(
        'segmentList.addEventListener("click"',
        1,
    )[0]

    assert "navigator.mediaDevices?.getUserMedia" in voice
    assert "navigator.mediaDevices.getUserMedia({ audio: true })" in voice
    assert 'typeof MediaRecorder === "undefined"' in voice
    assert "recorder = new MediaRecorder(stream);" in voice
    assert "new MediaRecorder(stream," not in voice
    assert "MediaRecorder.isTypeSupported" not in voice
    assert "isSecureContext" not in voice
    for event_name in [
        "pointerdown",
        "pointermove",
        "pointerup",
        "pointercancel",
        "lostpointercapture",
    ]:
        assert f'addEventListener("{event_name}"' in voice
    assert "VOICE_CANCEL_DISTANCE_PX = 72" in frontend
    assert "VOICE_MAX_RECORDING_MS = 60_000" in frontend
    assert "finishRecording(gesture.cancelArmed)" in voice
    assert "gesture.discard || state.disposed" in voice
    assert "stopTracks(stream)" in voice
    assert "body: blob" in frontend
    assert 'headers: { "Content-Type": blob.type || "application/octet-stream" }' in frontend
    assert "globalThis.crypto?.randomUUID?.()" in voice


def test_segment_polling_feedback_audio_retry_and_delete_contract(
    client_factory,
) -> None:
    frontend, _ = _frontend(client_factory)
    capture = frontend.split("function renderCapture", 1)[1].split(
        "function inputStatusCard",
        1,
    )[0]
    refresh = capture.split("async function refreshDraft", 1)[1].split(
        "function scheduleAutosave",
        1,
    )[0]

    assert "captureHasActiveSegment(state.draft)" in capture
    assert "scheduleDraftPoll()" in refresh
    assert (
        refresh.index("textarea.value = response.draft.current_text")
        < refresh.index("applyVoiceCompletionFeedback(textarea)")
    )
    assert "voiceSegmentStatusSnapshot(state.draft)" in capture
    assert "pending" in capture and "transcribing" in capture
    assert "已加入可编辑文字" in capture
    assert "转写未完成" in capture
    assert 'src="/api/voice-segments/${segmentId}/audio"' in frontend
    assert "storage_key" not in frontend
    assert "provider_request" not in frontend
    assert 'api(`/api/voice-segments/${segmentId}/retry`' in capture
    assert 'api(`/api/voice-segments/${segmentId}`, { method: "DELETE" })' in capture
    assert "VOICE_COMPLETION_HIGHLIGHT_MS = 700" in frontend
    assert "VOICE_COMPLETION_VIBRATION_MS = 25" in frontend
    assert 'documentObject.visibilityState === "visible"' in frontend
    assert "textarea.scrollTop = textarea.scrollHeight" in frontend
    assert "textarea.setSelectionRange(textEnd, textEnd)" in frontend


def test_final_save_navigation_session_and_mobile_accessibility_contract(
    client_factory,
) -> None:
    frontend, styles = _frontend(client_factory)
    capture = frontend.split("function renderCapture", 1)[1].split(
        "function inputStatusCard",
        1,
    )[0]
    submit = capture.split('form.addEventListener("submit"', 1)[1].split(
        "void bootstrapDraft",
        1,
    )[0]

    for blocker in [
        "state.pendingUpload",
        "state.revisionConflict",
        "state.gesture",
        "captureHasActiveSegment(state.draft)",
        "captureHasFailedSegment(state.draft)",
    ]:
        assert blocker in submit
    assert "await flushDraft({ ensureDraft: true })" in submit
    assert "writeSafetyBuffer({ savePending: true })" in submit
    assert (
        submit.index('await api("/api/capture-draft/save"')
        < submit.index('textarea.value = ""')
    )
    assert "最终保存尚未确认，输入仍保留" in submit
    assert "activeCaptureController.prepareNavigation()" in frontend
    assert "activeCaptureController?.deferSessionExpiredRedirect()" in frontend
    assert "previousCaptureController.dispose()" in frontend
    assert "state.gesture.discard = true" in capture
    assert "clearCaptureDraft();" in frontend.split("function completeLocalLogout", 1)[1]
    assert 'aria-live="polite"' in capture
    assert 'aria-label="${escapeHtml(label)}"' in frontend

    for selector in [
        ".voice-hold-button",
        ".voice-segment",
        ".voice-audio",
        ".pending-upload-actions",
        ".capture-revision-conflict",
        ".capture-actions",
    ]:
        assert selector in styles
    assert "min-height: 58px" in styles
    assert "touch-action: none" in styles
    assert "@media (max-width: 520px)" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
    reduced_motion = styles.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert "#capture-text.voice-completion-feedback" in reduced_motion
    assert "animation: none" in reduced_motion
