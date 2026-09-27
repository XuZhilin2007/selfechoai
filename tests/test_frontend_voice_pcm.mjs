import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const directory = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(directory, "..", "app", "static", "voice-pcm-worklet.js"), "utf8");

test("microphone worklet downmixes and bounds realtime PCM frames", () => {
  let Processor;
  const frames = [];
  class WorkletProcessor {
    constructor() {
      this.port = { postMessage(buffer) { frames.push(new Int16Array(buffer)); } };
    }
  }
  vm.runInNewContext(source, {
    AudioWorkletProcessor: WorkletProcessor,
    Int16Array,
    sampleRate: 48000,
    registerProcessor(name, constructor) {
      assert.equal(name, "selfecho-pcm-16k");
      Processor = constructor;
    },
  });
  const processor = new Processor();
  for (let block = 0; block < 10; block += 1) {
    const output = new Float32Array(128).fill(1);
    assert.equal(processor.process(
      [[new Float32Array(128).fill(1), new Float32Array(128).fill(0)]],
      [[output]],
    ), true);
    assert.ok(output.every((sample) => sample === 0));
  }
  assert.equal(frames.length, 1);
  assert.ok(frames[0].length >= 320 && frames[0].length <= 426);
  assert.ok(frames[0].every((sample) => sample === 16384));
});
