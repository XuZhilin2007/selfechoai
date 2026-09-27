// Microphone monitor: downsample to 16 kHz mono PCM16 for live recognition.
// The MediaRecorder stream remains the separate preserved Original Audio.
class SelfEchoPcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.position = 0;
    this.samples = [];
  }

  process(inputs, outputs) {
    const channels = inputs[0];
    const channel = channels?.[0];
    const output = outputs[0]?.[0];
    if (output) output.fill(0);
    if (!channel) return true;
    const ratio = sampleRate / 16000;
    for (let i = 0; i < channel.length; i += 1) {
      this.position += 1;
      if (this.position >= ratio) {
        this.position -= ratio;
        let sample = 0;
        for (const inputChannel of channels) sample += inputChannel[i];
        const value = Math.max(-1, Math.min(1, sample / channels.length));
        this.samples.push(value < 0 ? Math.round(value * 32768) : Math.round(value * 32767));
      }
    }
    if (this.samples.length >= 320) {
      const pcm = new Int16Array(this.samples);
      this.samples = [];
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}

registerProcessor("selfecho-pcm-16k", SelfEchoPcmProcessor);
