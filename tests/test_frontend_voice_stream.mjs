import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const testsDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendPath = path.join(testsDirectory, "..", "app", "static", "app.js");
const frontendSource = readFileSync(frontendPath, "utf8");

function jsonResponse(body, status = 200) {
  return { ok: true, status, async json() { return body; } };
}

function draftResponse(draft) {
  return jsonResponse({ draft, voice_available: true });
}

const EMPTY_QUICK_CONFIRMATION = jsonResponse({
  due_reminders: [],
  sortable_items: [],
  needs_confirmation: [],
  pending_inputs: [],
});

async function settleCapture() {
  for (let i = 0; i < 6; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function createCaptureHarness(fetchImpl) {
  const storage = new Map();
  const elements = new Map();
  const documentHandlers = new Map();
  const windowHandlers = new Map();

  function createElement(selector) {
    const children = new Map();
    const handlers = {};
    const element = {
      dataset: {},
      disabled: false,
      handlers,
      hidden: false,
      innerHTML: "",
      isConnected: true,
      scrollTop: 0,
      scrollHeight: 0,
      textContent: "",
      value: "",
      classList: {
        add() {},
        contains() { return false; },
        remove() {},
      },
      addEventListener(name, handler) {
        (handlers[name] ??= []).push(handler);
      },
      focus() {},
      querySelector(childSelector) {
        if (!children.has(childSelector)) {
          children.set(childSelector, createElement(`${selector} ${childSelector}`));
        }
        return children.get(childSelector);
      },
      querySelectorAll() { return []; },
      removeAttribute() {},
      replaceChildren() {},
      setAttribute() {},
    };
    elements.set(selector, element);
    return element;
  }

  const document = {
    body: { classList: { add() {}, remove() {} } },
    cookie: "selfecho_csrf=voice-token",
    visibilityState: "visible",
    addEventListener(name, handler) { documentHandlers.set(name, handler); },
    querySelector(selector) {
      return elements.get(selector) ?? createElement(selector);
    },
    querySelectorAll() { return []; },
  };
  const timers = [];
  const window = {
    addEventListener(name, handler) { windowHandlers.set(name, handler); },
    clearTimeout(id) { delete timers[id - 1]; },
    confirm: () => true,
    location: { origin: "https://selfecho.example", pathname: "/capture", search: "" },
    scrollTo() {},
    setTimeout(callback, delay) {
      timers.push({ callback, delay, fired: false });
      return timers.length;
    },
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
  ).replace(
    "  function scheduleAutosave() {",
    "  globalThis.__captureRefreshForTest = refreshDraft;\n  function scheduleAutosave() {",
  ) + `
    globalThis.__voiceTest = {
      captureDiscardControl,
      capturePrefersWholeDraftDiscard,
      getActiveCaptureController: () => activeCaptureController,
      renderCapture,
      openVoiceStream,
      voiceAudioMarkup,
      voiceSegmentFailureCopy,
      setUser(user) { authentication.user = user; },
    };
  `;
  vm.runInContext(instrumented, context, { filename: frontendPath });
  context.__voiceTest.setUser({ id: 17 });
  return { context, elements, storage, timers, documentHandlers, windowHandlers };
}

function socketHarness(context) {
  const sockets = [];
  class Socket {
    static OPEN = 1;
    static CLOSING = 2;
    constructor(url) {
      this.url = url;
      this.readyState = Socket.OPEN;
      this.sent = [];
      this.bufferedAmount = 0;
      sockets.push(this);
    }
    send(value) { this.sent.push(value); }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  context.WebSocket = Socket;
  return sockets;
}

test("stream socket keeps CSRF in the authenticated hello and previews remain non-authoritative", async () => {
  const { context } = createCaptureHarness(() => EMPTY_QUICK_CONFIRMATION);
  context.window.location.protocol = "https:";
  context.window.location.host = "selfecho.example";
  const sockets = socketHarness(context);
  const previews = [];
  const opening = context.__voiceTest.openVoiceStream({
    csrf: "synthetic-csrf", draft_id: 7, revision: 2,
    client_segment_id: "client-one",
  }, (event) => previews.push(event));
  const socket = sockets[0];
  assert.equal(socket.url, "wss://selfecho.example/api/capture-draft/voice-stream");
  assert.ok(!socket.url.includes("synthetic-csrf"));
  socket.onopen();
  assert.equal(JSON.parse(socket.sent[0]).csrf, "synthetic-csrf");
  socket.onmessage({ data: JSON.stringify({ type: "ready", session_id: 11 }) });
  const session = await opening;
  assert.equal(session.sessionId, 11);
  socket.onmessage({ data: JSON.stringify({ type: "preview", text: "guess" }) });
  socket.onmessage({ data: JSON.stringify({ type: "preview", text: "revision" }) });
  session.stop();
  assert.equal(JSON.parse(socket.sent[1]).type, "stop");
  socket.onmessage({ data: JSON.stringify({ type: "complete", segment_id: 5 }) });
  assert.equal((await session.outcome).segment_id, 5);
  socket.onmessage({ data: JSON.stringify({ type: "preview", text: "late" }) });
  assert.deepEqual(previews.map((event) => event.text), ["guess", "revision"]);
});

test("pre-ready ASR capacity denial reports a temporary limit and never starts recording", async () => {
  const { context } = createCaptureHarness(() => EMPTY_QUICK_CONFIRMATION);
  context.window.location.protocol = "https:";
  context.window.location.host = "selfecho.example";
  const sockets = socketHarness(context);
  const events = [];
  const opening = context.__voiceTest.openVoiceStream({
    csrf: "synthetic-csrf", draft_id: 7, revision: 2,
    client_segment_id: "capacity-denied",
  }, (event) => events.push(event));
  const socket = sockets[0];
  socket.onopen();
  socket.onmessage({ data: JSON.stringify({ type: "failed", reason: "capacity" }) });
  await assert.rejects(opening, /临时使用上限.*尚未开始录音/);
  assert.equal(socket.readyState, 3);
  assert.deepEqual(events, []);
  assert.equal(socket.sent.length, 1);
});

test("pre-ready storage denial states the recording has not begun", async () => {
  for (const [reason, wording] of [
    ["storage_capacity", /存储空间暂时不足.*尚未开始录音/],
    ["storage_pressure", /录音保存暂时过于频繁.*尚未开始录音/],
  ]) {
    const { context } = createCaptureHarness(() => EMPTY_QUICK_CONFIRMATION);
    context.window.location.protocol = "https:";
    context.window.location.host = "selfecho.example";
    const sockets = socketHarness(context);
    const events = [];
    const opening = context.__voiceTest.openVoiceStream({
      csrf: "synthetic-csrf", draft_id: 7, revision: 2,
      client_segment_id: `storage-${reason}`,
    }, (event) => events.push(event));
    const socket = sockets[0];
    socket.onopen();
    socket.onmessage({ data: JSON.stringify({ type: "failed", reason }) });
    await assert.rejects(opening, wording);
    assert.equal(socket.readyState, 3);
    assert.deepEqual(events, []);
    assert.equal(socket.sent.length, 1);
  }
});

test("completed streaming transcript is visible as pending Draft acceptance", async () => {
  const transcript = "识别结果 <仍待确认>";
  const draft = {
    id: 7, revision: 2, current_text: "原来的草稿文字",
    voice_segments: [{
      id: 9, position: 0, transcription_status: "transcribed",
      provider_transcript: transcript, failure_code: null, failure_message: null,
    }],
  };
  const { context, elements } = createCaptureHarness(async (pathName, options) => {
    if (pathName === "/api/capture-draft" && (options?.method ?? "GET") === "GET") {
      return draftResponse(draft);
    }
    return EMPTY_QUICK_CONFIRMATION;
  });
  context.__voiceTest.renderCapture();
  await settleCapture();
  const markup = elements.get("#voice-segment-list").innerHTML;
  assert.match(markup, /已转写，尚未加入草稿/);
  assert.match(markup, /识别结果 &lt;仍待确认&gt;/);
  assert.equal(elements.get("#capture-text").value, "原来的草稿文字");
  assert.equal(elements.get("#capture-form").querySelector(".capture-submit").disabled, true);
  assert.match(elements.get("#capture-status").textContent, /暂不能最终保存/);
});

test("stranded streaming transcript retries acceptance after saving local Draft text", async () => {
  const calls = [];
  let draft = {
    id: 7, revision: 2, current_text: "existing",
    voice_segments: [{ id: 9, position: 0, transcription_status: "transcribed",
      provider_transcript: "final", failure_code: null, failure_message: null }],
  };
  const { context, elements } = createCaptureHarness(async (pathName, options) => {
    if (pathName === "/api/capture-draft" && (options?.method ?? "GET") === "GET") {
      return draftResponse(draft);
    }
    if (pathName === "/api/capture-draft" && options.method === "PUT") {
      calls.push("draft");
      const payload = JSON.parse(options.body);
      draft = { ...draft, current_text: payload.current_text, revision: draft.revision + 1 };
      return draftResponse(draft);
    }
    if (pathName === "/api/voice-segments/9/accept" && options.method === "POST") {
      calls.push("accept");
      draft = { ...draft, revision: draft.revision + 1,
        current_text: `${draft.current_text}\nfinal`,
        voice_segments: [{ ...draft.voice_segments[0], transcription_status: "succeeded" }] };
      return jsonResponse(draft.voice_segments[0]);
    }
    return EMPTY_QUICK_CONFIRMATION;
  });
  context.__voiceTest.renderCapture();
  await settleCapture();
  const textarea = elements.get("#capture-text");
  textarea.value = "user edit";
  textarea.handlers.input[0]();
  const button = { dataset: { segmentId: "9" }, disabled: false };
  await elements.get("#voice-segment-list").handlers.click[0]({
    target: { closest(selector) { return selector === ".voice-segment-accept" ? button : null; } },
  });
  assert.deepEqual(calls, ["draft", "accept"]);
  assert.equal(textarea.value, "user edit\nfinal");
  assert.equal(elements.get("#capture-form").querySelector(".capture-submit").disabled, false);
  assert.match(elements.get("#capture-status").textContent, /可以继续编辑后保存/);
});

test("background acceptance cannot overwrite unsaved user text on Draft refresh", async () => {
  let draft = {
    id: 7, revision: 2, current_text: "existing",
    voice_segments: [{ id: 9, position: 0, transcription_status: "transcribed",
      provider_transcript: "final", failure_code: null, failure_message: null }],
  };
  const { context, elements } = createCaptureHarness(async (pathName, options) => {
    if (pathName === "/api/capture-draft" && (options?.method ?? "GET") === "GET") {
      return draftResponse(draft);
    }
    return EMPTY_QUICK_CONFIRMATION;
  });
  context.__voiceTest.renderCapture();
  await settleCapture();
  const textarea = elements.get("#capture-text");
  textarea.value = "my unsaved edit";
  textarea.handlers.input[0]();
  draft = { ...draft, revision: 3, current_text: "existing\nfinal",
    voice_segments: [{ ...draft.voice_segments[0], transcription_status: "succeeded" }] };
  await context.__captureRefreshForTest();
  assert.equal(textarea.value, "my unsaved edit");
  assert.equal(elements.get("#capture-revision-conflict").hidden, false);
  assert.equal(elements.get("#capture-form").querySelector(".capture-submit").disabled, true);
  assert.match(elements.get("#capture-status").textContent, /本页未确认文字仍保留/);
});

test("older Draft read cannot undo a newer accepted Streaming revision", async () => {
  const oldDraft = {
    id: 7, revision: 2, current_text: "existing",
    voice_segments: [{ id: 9, position: 0, transcription_status: "transcribed",
      provider_transcript: "final", failure_code: null, failure_message: null }],
  };
  let latest = oldDraft;
  let deferRead = false;
  let resolveOldRead;
  const { context, elements } = createCaptureHarness(async (pathName, options) => {
    if (pathName === "/api/capture-draft" && (options?.method ?? "GET") === "GET") {
      if (deferRead) return new Promise((resolve) => { resolveOldRead = resolve; });
      return draftResponse(latest);
    }
    return EMPTY_QUICK_CONFIRMATION;
  });
  context.__voiceTest.renderCapture();
  await settleCapture();
  deferRead = true;
  const oldRequest = context.__captureRefreshForTest();
  await settleCapture();
  latest = { ...oldDraft, revision: 3, current_text: "existing\nfinal",
    voice_segments: [{ ...oldDraft.voice_segments[0], transcription_status: "succeeded" }] };
  deferRead = false;
  await context.__captureRefreshForTest();
  resolveOldRead(draftResponse(oldDraft));
  await oldRequest;
  assert.equal(elements.get("#capture-text").value, "existing\nfinal");
  assert.equal(elements.get("#capture-form").querySelector(".capture-submit").disabled, false);
  assert.match(elements.get("#voice-segment-list").innerHTML, /已加入可编辑文字/);
});

test("bootstrap treats a stale dirty buffer as a conflict after Streaming recovery", async () => {
  const calls = [];
  const serverDraft = {
    id: 7, revision: 3, current_text: "original\naccepted transcript",
    voice_segments: [{ id: 9, position: 0, transcription_status: "succeeded",
      provider_transcript: "accepted transcript", failure_code: null, failure_message: null }],
  };
  const { context, elements, storage } = createCaptureHarness(async (pathName, options) => {
    calls.push({ pathName, method: options?.method ?? "GET" });
    if (pathName === "/api/capture-draft" && (options?.method ?? "GET") === "GET") {
      return draftResponse(serverDraft);
    }
    return EMPTY_QUICK_CONFIRMATION;
  });
  storage.set("selfecho.capture-draft.17", JSON.stringify({
    version: 1, text: "local unsynced edit", dirty: true,
    draftId: 7, revision: 2, savePending: false,
  }));
  context.__voiceTest.renderCapture();
  await settleCapture();
  const textarea = elements.get("#capture-text");
  const panel = elements.get("#capture-revision-conflict");
  assert.equal(textarea.value, "local unsynced edit");
  assert.equal(panel.hidden, false);
  assert.equal(panel.innerHTML.includes(serverDraft.current_text), true);
  assert.match(elements.get("#capture-status").textContent, /请选择要保留的文字版本/);
  assert.equal(elements.get("#capture-form").querySelector(".capture-submit").disabled, true);
  assert.equal(await context.__voiceTest.getActiveCaptureController().prepareNavigation(), false);
  await context.__voiceTest.getActiveCaptureController().flush();
  assert.equal(calls.some((call) => call.method === "PUT"), false);

  const useServer = { dataset: {} };
  panel.handlers.click[0]({
    target: { closest(selector) { return selector === ".conflict-use-server" ? useServer : null; } },
  });
  assert.equal(textarea.value, serverDraft.current_text);
  assert.equal(panel.hidden, true);
  await context.__voiceTest.getActiveCaptureController().flush();
  assert.equal(calls.some((call) => call.method === "PUT"), false);
});

test("release during stream setup rejects via the abort signal and never sends hello", async () => {
  const { context } = createCaptureHarness(() => EMPTY_QUICK_CONFIRMATION);
  context.window.location.protocol = "https:";
  context.window.location.host = "selfecho.example";
  const sockets = socketHarness(context);
  const controller = new AbortController();
  const opening = context.__voiceTest.openVoiceStream({
    csrf: "synthetic-csrf", draft_id: 7, revision: 2,
    client_segment_id: "aborted-setup",
  }, () => {}, controller.signal);
  const socket = sockets[0];
  controller.abort();
  await assert.rejects(opening, /录音尚未开始，实时连接已取消/);
  assert.equal(socket.readyState, 3);
  assert.equal(socket.sent.length, 0);
});

test("batch-only Voice keeps the existing capture path and streaming never auto-falls-back", () => {
  const voiceFlow = frontendSource
    .split("async function beginVoiceGesture", 2)[1]
    .split("segmentList.addEventListener", 1)[0];

  // The routing decision is made before any stream or provider work.
  const batchGate = voiceFlow.indexOf("if (!state.streamingAvailable) {");
  const streamingSetup = voiceFlow.indexOf("openVoiceStream({");
  assert.ok(batchGate >= 0, "batch-only branch missing");
  assert.ok(streamingSetup > batchGate, "batch routing must precede streaming setup");

  // The batch branch uploads through the plain batch endpoint, without any
  // streaming attempt, streaming parameters, or force_failed flag.
  const streamingSetupAnchor = "    try {\n      const session = await openVoiceStream({";
  const batchBranch = voiceFlow.slice(batchGate, voiceFlow.indexOf(streamingSetupAnchor));
  assert.ok(batchBranch.length > 0, "batch-only branch must have a body");
  assert.match(batchBranch, /await uploadRecording\(blob, gesture\.clientSegmentId\);/);
  assert.doesNotMatch(batchBranch, /streaming: true/);
  assert.doesNotMatch(batchBranch, /openVoiceStream/);

  // The streaming branch still drives the streaming upload explicitly.
  assert.match(voiceFlow, /streaming: true, ownerId: gesture\.ownerId/);

  // A failed streaming attempt never re-routes into the batch path: the only
  // plain batch upload call lives inside the batch-only branch.
  const afterBatchBranch = voiceFlow.slice(voiceFlow.indexOf(streamingSetupAnchor));
  assert.equal(
    afterBatchBranch.indexOf("await uploadRecording(blob, gesture.clientSegmentId);"),
    -1,
    "no silent batch fallback after the batch-only branch",
  );

  // The microphone-only capability check no longer requires WebSocket support
  // unless streaming is actually available.
  assert.match(
    voiceFlow,
    /typeof WebSocket === "undefined" \|\| !window\.AudioContext\)\) \{[\s\S]*?此浏览器暂不支持实时录音/,
  );
});
