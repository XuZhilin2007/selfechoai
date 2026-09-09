import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const testsDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendPath = path.join(testsDirectory, "..", "app", "static", "app.js");
const frontendSource = readFileSync(frontendPath, "utf8");
const stylesPath = path.join(testsDirectory, "..", "app", "static", "styles.css");
const stylesSource = readFileSync(stylesPath, "utf8");

function createHarness(fetchImpl = async () => ({
  ok: true,
  status: 200,
  async json() {
    return {};
  },
})) {
  const storage = new Map();
  const genericElement = {
    addEventListener() {},
    classList: { add() {}, contains() { return false; }, remove() {} },
    dataset: {},
    focus() {},
    hidden: false,
    innerHTML: "",
    isConnected: true,
    querySelector() { return genericElement; },
    querySelectorAll() { return []; },
    removeAttribute() {},
    setAttribute() {},
    textContent: "",
    value: "",
  };
  const appElement = { focus() {}, innerHTML: "", replaceChildren() {} };
  const document = {
    body: { classList: { add() {}, remove() {} } },
    cookie: "selfecho_csrf=voice-token",
    addEventListener() {},
    querySelector(selector) {
      if (selector === "#app") return appElement;
      if (selector === "#primary-navigation") return { hidden: false };
      return genericElement;
    },
    querySelectorAll() { return []; },
  };
  const window = {
    addEventListener() {},
    clearTimeout() {},
    location: {
      origin: "https://selfecho.example",
      pathname: "/capture",
      search: "",
    },
    scrollTo() {},
    setTimeout() { return 1; },
  };
  const context = vm.createContext({
    Blob,
    console,
    document,
    fetch: fetchImpl,
    Headers,
    history: { pushState() {}, replaceState() {} },
    navigator: {},
    sessionStorage: {
      getItem(key) { return storage.get(key) ?? null; },
      removeItem(key) { storage.delete(key); },
      setItem(key, value) { storage.set(key, String(value)); },
    },
    URL,
    URLSearchParams,
    window,
  });
  const instrumented = frontendSource.replace(
    /^void initializeAuthentication\(\);$/m,
    "globalThis.__startupPromise = Promise.resolve();",
  ) + `
    globalThis.__voiceTest = {
      rawApi,
      readCaptureBuffer,
      writeCaptureDraft,
      voiceGestureCancelArmed,
      captureHasActiveSegment,
      captureHasFailedSegment,
      captureDiscardControl,
      runWithCaptureDiscardPending,
      voiceSegmentFailureCopy,
      voiceSegmentStatusSnapshot,
      observeVoiceSegmentCompletions,
      applyVoiceCompletionFeedback,
      ApiError,
      isDraftRevisionConflict,
      recoverVoiceUploadRevisionConflict,
      setUser(user) { authentication.user = user; },
    };
  `;
  vm.runInContext(instrumented, context, { filename: frontendPath });
  context.__voiceTest.setUser({ id: 17 });
  return { context, storage };
}

test("recording UX uses one press-and-hold Pointer Events flow", () => {
  const voiceFlow = frontendSource.split("async function beginVoiceGesture", 2)[1]
    .split("segmentList.addEventListener", 1)[0];
  for (const eventName of [
    "pointerdown",
    "pointermove",
    "pointerup",
    "pointercancel",
    "lostpointercapture",
  ]) {
    assert.match(voiceFlow, new RegExp(`addEventListener\\(\\"${eventName}\\"`));
  }
  assert.ok(
    voiceFlow.indexOf("navigator.mediaDevices.getUserMedia({ audio: true })") <
      voiceFlow.indexOf("await Promise.allSettled"),
  );
  assert.match(voiceFlow, /recorder = new MediaRecorder\(stream\);/);
  assert.doesNotMatch(voiceFlow, /new MediaRecorder\(stream,/);
  assert.match(voiceFlow, /stopTracks\(stream\)/);
  assert.match(frontendSource, /VOICE_MAX_RECORDING_MS = 60_000/);
  assert.match(frontendSource, /voiceGestureCancelArmed\(gesture\.startY, event\.clientY\)/);
  assert.doesNotMatch(voiceFlow, /voiceButton\.addEventListener\("click"/);
  assert.match(voiceFlow, /abandonVoiceGesture/);
  assert.match(voiceFlow, /浏览器无法启动录音/);
});

test("durable Draft, exact Final Save and pending-upload safeguards are wired", () => {
  assert.match(frontendSource, /CAPTURE_AUTOSAVE_DELAY_MS = 700/);
  assert.match(frontendSource, /JSON\.stringify\(\{ current_text: snapshot, revision: expectedRevision \}\)/);
  assert.match(frontendSource, /api\("\/api\/capture-draft\/save"/);
  assert.match(frontendSource, /draft_id: state\.draft\.id/);
  assert.match(frontendSource, /revision: state\.draft\.revision/);
  assert.match(frontendSource, /writeSafetyBuffer\(\{ savePending: true \}\)/);
  assert.match(frontendSource, /if \(state\.pendingUpload\) \{[\s\S]*return false;/);
  assert.match(frontendSource, /body: blob/);
  assert.match(frontendSource, /activeCaptureController\?\.deferSessionExpiredRedirect\(\)/);
  assert.doesNotMatch(
    frontendSource.split('form.addEventListener("submit"', 2)[1].split("void bootstrapDraft", 1)[0],
    /textarea\.value\.trim\(\)[\s\S]*original_text/,
  );
});

test("local safety buffer preserves exact text and reads legacy values", () => {
  const { context, storage } = createHarness();
  context.__voiceTest.writeCaptureDraft("  exact text\n", {
    dirty: true,
    draftId: 9,
    revision: 4,
    savePending: true,
  });
  const stored = JSON.parse(storage.get("selfecho.capture-draft.17"));
  assert.equal(stored.text, "  exact text\n");
  assert.equal(stored.draftId, 9);
  assert.equal(stored.revision, 4);
  assert.equal(stored.savePending, true);
  const recovered = context.__voiceTest.readCaptureBuffer();
  assert.equal(recovered.text, "  exact text\n");
  assert.equal(recovered.dirty, true);

  storage.set("selfecho.capture-draft.17", "legacy exact value");
  const legacy = context.__voiceTest.readCaptureBuffer();
  assert.equal(legacy.text, "legacy exact value");
  assert.equal(legacy.dirty, true);
});

test("raw upload keeps browser content type and adds CSRF without JSON coercion", async () => {
  const calls = [];
  const { context } = createHarness(async (pathName, options) => {
    calls.push({ pathName, options });
    return {
      ok: true,
      status: 202,
      async json() { return { id: 4 }; },
    };
  });
  const body = new Blob(["synthetic"], { type: "audio/webm" });
  await context.__voiceTest.rawApi("/api/capture-draft/voice-segments/client-1?revision=3", {
    method: "PUT",
    body,
    headers: { "Content-Type": body.type },
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.body, body);
  assert.equal(calls[0].options.headers.get("Content-Type"), "audio/webm");
  assert.equal(calls[0].options.headers.get("X-CSRF-Token"), "voice-token");
  assert.equal(calls[0].options.credentials, "same-origin");
});

test("slide-up threshold and server segment blockers are deterministic", () => {
  const { context } = createHarness();
  assert.equal(context.__voiceTest.voiceGestureCancelArmed(200, 129), false);
  assert.equal(context.__voiceTest.voiceGestureCancelArmed(200, 128), true);
  assert.equal(context.__voiceTest.captureHasActiveSegment({
    voice_segments: [{ transcription_status: "transcribing" }],
  }), true);
  assert.equal(context.__voiceTest.captureHasFailedSegment({
    voice_segments: [{ transcription_status: "failed" }],
  }), true);
});

test("a newly succeeded Voice Segment is observed once, while bootstrap history is quiet", () => {
  const { context } = createHarness();
  const pendingDraft = {
    current_text: "before",
    voice_segments: [
      { id: 11, transcription_status: "transcribing" },
      { id: 7, transcription_status: "succeeded" },
    ],
  };
  const succeededDraft = {
    current_text: "before\nexact transcript",
    voice_segments: [
      { id: 11, transcription_status: "succeeded" },
      { id: 7, transcription_status: "succeeded" },
    ],
  };

  let observation = context.__voiceTest.observeVoiceSegmentCompletions(
    context.__voiceTest.voiceSegmentStatusSnapshot(pendingDraft),
    succeededDraft,
  );
  assert.deepEqual([...observation.completedSegmentIds], [11]);

  observation = context.__voiceTest.observeVoiceSegmentCompletions(
    observation.statuses,
    succeededDraft,
  );
  assert.deepEqual([...observation.completedSegmentIds], []);

  const bootstrapStatuses = context.__voiceTest.voiceSegmentStatusSnapshot(succeededDraft);
  const bootstrapObservation = context.__voiceTest.observeVoiceSegmentCompletions(
    bootstrapStatuses,
    succeededDraft,
  );
  assert.deepEqual([...bootstrapObservation.completedSegmentIds], []);

  const refreshFlow = frontendSource.split("async function refreshDraft", 2)[1]
    .split("function scheduleAutosave", 1)[0];
  assert.ok(
    refreshFlow.indexOf("textarea.value = response.draft.current_text") <
      refreshFlow.indexOf("applyVoiceCompletionFeedback(textarea)"),
  );
});

test("visible completion highlights, scrolls, preserves exact text, and lightly vibrates", () => {
  const { context } = createHarness();
  const classes = new Set();
  const selections = [];
  const textarea = {
    classList: {
      add(value) { classes.add(value); },
      remove(value) { classes.delete(value); },
    },
    scrollHeight: 480,
    scrollTop: 0,
    setSelectionRange(start, end) { selections.push([start, end]); },
    value: "existing text\nexact appended transcript",
  };
  const originalText = textarea.value;
  const vibrations = [];
  const scheduled = [];

  const timer = context.__voiceTest.applyVoiceCompletionFeedback(textarea, {
    documentObject: { activeElement: textarea, visibilityState: "visible" },
    navigatorObject: {
      vibrate(duration) {
        vibrations.push(duration);
        return true;
      },
    },
    schedule(callback, delay) {
      scheduled.push({ callback, delay });
      return 91;
    },
  });

  assert.equal(timer, 91);
  assert.equal(classes.has("voice-completion-feedback"), true);
  assert.equal(textarea.scrollTop, 480);
  assert.deepEqual(selections, [[originalText.length, originalText.length]]);
  assert.equal(textarea.value, originalText);
  assert.equal(vibrations.length, 1);
  assert.ok(vibrations[0] >= 20 && vibrations[0] <= 30);
  assert.ok(scheduled[0].delay >= 500 && scheduled[0].delay <= 800);
  scheduled[0].callback();
  assert.equal(classes.has("voice-completion-feedback"), false);
  assert.match(stylesSource, /#capture-text\.voice-completion-feedback/);
  assert.match(
    stylesSource,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*#capture-text\.voice-completion-feedback[\s\S]*animation: none/,
  );
});

test("unsupported or hidden haptics are silent and never block visual feedback", () => {
  const { context } = createHarness();
  const createTextarea = () => ({
    classList: { add() {}, remove() {} },
    scrollHeight: 100,
    scrollTop: 0,
    value: "exact text",
  });
  const schedule = () => 1;

  assert.doesNotThrow(() => {
    context.__voiceTest.applyVoiceCompletionFeedback(createTextarea(), {
      documentObject: { activeElement: null, visibilityState: "visible" },
      navigatorObject: {},
      schedule,
    });
  });

  let declinedVibrations = 0;
  assert.doesNotThrow(() => {
    context.__voiceTest.applyVoiceCompletionFeedback(createTextarea(), {
      documentObject: { activeElement: null, visibilityState: "visible" },
      navigatorObject: {
        vibrate() {
          declinedVibrations += 1;
          return false;
        },
      },
      schedule,
    });
  });
  assert.equal(declinedVibrations, 1);

  let hiddenVibrations = 0;
  assert.doesNotThrow(() => {
    context.__voiceTest.applyVoiceCompletionFeedback(createTextarea(), {
      documentObject: { activeElement: null, visibilityState: "hidden" },
      navigatorObject: {
        vibrate() {
          hiddenVibrations += 1;
          return false;
        },
      },
      schedule,
    });
  });
  assert.equal(hiddenVibrations, 0);
});

test("equivalent stale Draft adopts server revision and retries the same pending Blob", async () => {
  const { context } = createHarness();
  const blob = new Blob(["same pending audio"], { type: "audio/webm" });
  const pendingUpload = { blob, clientSegmentId: "stable-client-id" };
  let retained = pendingUpload;
  let adopted = null;
  const retries = [];

  const result = await context.__voiceTest.recoverVoiceUploadRevisionConflict({
    pendingUpload,
    localText: "same text",
    async loadServerDraft() {
      return {
        draft: { id: 4, revision: 9, current_text: "same text", voice_segments: [] },
        voice_available: true,
      };
    },
    isRetained(candidate) { return retained === candidate; },
    adoptEquivalent(serverDraft, voiceAvailable) {
      adopted = { serverDraft, voiceAvailable };
    },
    requireChoice() { assert.fail("equivalent text must not require a choice"); },
    async retryPendingUpload(candidate) { retries.push(candidate); },
  });

  assert.equal(result, "retried");
  assert.equal(adopted.serverDraft.revision, 9);
  assert.equal(adopted.voiceAvailable, true);
  assert.equal(retries.length, 1);
  assert.equal(retries[0], pendingUpload);
  assert.equal(retries[0].blob, blob);
  assert.equal(retries[0].clientSegmentId, "stable-client-id");
  retained = null;
});

test("upload preflight recognizes only the stale Draft revision conflict", () => {
  const { context } = createHarness();
  assert.equal(
    context.__voiceTest.isDraftRevisionConflict(
      new context.__voiceTest.ApiError("Capture Draft revision is stale", 409),
    ),
    true,
  );
  assert.equal(
    context.__voiceTest.isDraftRevisionConflict(
      new context.__voiceTest.ApiError("another Voice Segment is still being transcribed", 409),
    ),
    false,
  );
  const uploadFlow = frontendSource.split("async function uploadRecording", 2)[1]
    .split("async function beginVoiceGesture", 1)[0];
  assert.ok(uploadFlow.indexOf("state.pendingUpload = pendingUpload") <
    uploadFlow.indexOf("await flushDraft({ ensureDraft: true })"));
  assert.match(uploadFlow, /isDraftRevisionConflict\(error\)/);
  assert.match(uploadFlow, /state\.pendingUpload === pendingUpload/);
  assert.match(uploadFlow, /retained\.clientSegmentId/);
});

test("differing stale Draft keeps Blob and requires explicit text choice", async () => {
  const { context } = createHarness();
  const blob = new Blob(["conflicting pending audio"], { type: "audio/webm" });
  const pendingUpload = { blob, clientSegmentId: "same-client-after-choice" };
  let conflict = null;
  let retryCount = 0;

  const result = await context.__voiceTest.recoverVoiceUploadRevisionConflict({
    pendingUpload,
    localText: "local exact text",
    async loadServerDraft() {
      return {
        draft: { id: 7, revision: 12, current_text: "server exact text", voice_segments: [] },
        voice_available: true,
      };
    },
    isRetained(candidate) { return candidate === pendingUpload; },
    adoptEquivalent() { assert.fail("different text must not auto-adopt"); },
    requireChoice(serverDraft, voiceAvailable, candidate) {
      conflict = { serverDraft, voiceAvailable, candidate };
    },
    async retryPendingUpload() { retryCount += 1; },
  });

  assert.equal(result, "requires_choice");
  assert.equal(retryCount, 0);
  assert.equal(conflict.serverDraft.revision, 12);
  assert.equal(conflict.serverDraft.current_text, "server exact text");
  assert.equal(conflict.candidate, pendingUpload);
  assert.equal(conflict.candidate.blob, blob);
  assert.equal(conflict.candidate.clientSegmentId, "same-client-after-choice");
  assert.match(frontendSource, /conflict-keep-local/);
  assert.match(frontendSource, /conflict-use-server/);
  assert.match(frontendSource, /loadServerDraft: \(\) => api\("\/api\/capture-draft"\)/);
});

test("Current Capture Voice Segment audio player preloads media metadata, not none", () => {
  const markupBody = frontendSource.split("function voiceAudioMarkup", 2)[1]
    .split("function renderCapture", 1)[0];
  assert.match(markupBody, /<audio class="voice-audio" controls preload="metadata"/);
  assert.doesNotMatch(markupBody, /preload="none"/);
  const currentCaptureRendering = frontendSource.split("function renderVoiceSegments", 2)[1]
    .split("function renderRevisionConflict", 1)[0];
  assert.match(currentCaptureRendering, /voiceAudioMarkup\(segment\.id/);
});

test("Discard pending blocks duplicates and restores after success or exception", async () => {
  const { context } = createHarness();
  const control = context.__voiceTest.captureDiscardControl;
  const runPending = context.__voiceTest.runWithCaptureDiscardPending;

  const duringDelete = control({
    draft: { id: 1 },
    hasText: true,
    hasPendingUpload: false,
    savePending: false,
    discardPending: true,
  });
  assert.equal(duringDelete.hidden, false);
  assert.equal(duringDelete.disabled, true);

  const afterSuccess = control({
    draft: null,
    hasText: false,
    hasPendingUpload: false,
    savePending: false,
    discardPending: false,
  });
  assert.equal(afterSuccess.hidden, true);
  assert.equal(afterSuccess.disabled, false);

  const nextDraft = control({
    draft: { id: 2 },
    hasText: true,
    hasPendingUpload: false,
    savePending: false,
    discardPending: false,
  });
  assert.equal(nextDraft.hidden, false);
  assert.equal(nextDraft.disabled, false);

  const state = { discardPending: false };
  let finishFirst;
  let operations = 0;
  let controlUpdates = 0;
  const first = runPending({
    state,
    updateControls() { controlUpdates += 1; },
    operation() {
      operations += 1;
      return new Promise((resolve) => { finishFirst = resolve; });
    },
  });
  assert.equal(state.discardPending, true);
  assert.equal(await runPending({
    state,
    updateControls() { controlUpdates += 1; },
    operation() { operations += 1; },
  }), false);
  assert.equal(operations, 1);
  finishFirst();
  assert.equal(await first, true);
  assert.equal(state.discardPending, false);
  assert.equal(controlUpdates, 2);

  await assert.rejects(
    runPending({
      state,
      updateControls() { controlUpdates += 1; },
      async operation() { throw new Error("synthetic discard failure"); },
    }),
    /synthetic discard failure/,
  );
  assert.equal(state.discardPending, false);
  assert.equal(controlUpdates, 4);

  const discardFlow = frontendSource.split('discardButton.addEventListener("click"', 2)[1]
    .split("const visibilityHandler", 1)[0];
  assert.match(discardFlow, /if \(state\.discardPending\) return;/);
  assert.match(discardFlow, /runWithCaptureDiscardPending\(\{/);
  assert.doesNotMatch(discardFlow, /discardButton\.disabled\s*=/);
});

test("Voice failure copy keeps details only for the safe allowlist", () => {
  const { context } = createHarness();
  const copy = context.__voiceTest.voiceSegmentFailureCopy;
  const generic = "这段录音没有得到可用文字。原始录音已保留。";

  for (const failureCode of [
    "configuration",
    "network",
    "timeout",
    "authentication",
    "provider_rejected",
    "provider_unavailable",
    "invalid_response",
    "internal",
  ]) {
    assert.equal(
      copy({ failure_code: failureCode, failure_message: "private provider detail" }),
      generic,
    );
  }
  assert.equal(copy({}), generic);
  assert.equal(copy({ failure_code: "media_probe", failure_message: null }), generic);

  for (const failureCode of [
    "media_probe",
    "unsupported_media",
    "conversion",
    "draft_text_limit",
  ]) {
    assert.equal(
      copy({ failure_code: failureCode, failure_message: "safe actionable copy" }),
      "safe actionable copy",
    );
  }

  assert.match(frontendSource, /请先处理未完成的录音，再保存。/);
  assert.doesNotMatch(frontendSource, /escapeHtml\(segment\.failure_message/);
});
