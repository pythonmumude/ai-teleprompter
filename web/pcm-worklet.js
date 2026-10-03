/**
 * PCM 采集：把麦克风音频重采样成 16k 单声道 Int16，每 100ms 打一包。
 *
 * 为什么放在 AudioWorklet 而不是主线程：
 *   重采样是逐采样点的循环，48k 就是每秒 48000 次。放主线程会和 rAF 渲染抢时间，
 *   提词滚动就掉帧。AudioWorklet 跑在独立音频线程，不受主线程卡顿影响。
 *
 * 为什么 100ms 一包：
 *   包越短延迟越低，但 WebSocket 帧开销和主线程唤醒越频繁。
 *   100ms 是平衡点，也正好等于后端停顿检测的时间粒度。
 *
 * 重采样算法（线性插值，分数累加器）：
 *   step = 输入采样率 / 16000。每读一个输入点就把累加器 +1，
 *   超过 step 就吐一个输出点；吐出的位置落在 prev→cur 这一段里，
 *   用 t = 1 - frac/step 做线性插值。
 *   不用「每 3 个取 1 个」是因为那等于没有抗混叠，高频会折回语音频段变成噪声。
 *   44.1k 时 step = 2.75625 不是整数，所以必须走插值这条路。
 */
class Pcm16kWorklet extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opt = options.processorOptions || {};
    this.targetRate = opt.targetRate || 16000;   // sampleRate 是 worklet 的全局变量
    this.frameMs = opt.frameMs || 100;
    this.step = sampleRate / this.targetRate;
    this.frameSamples = Math.round(this.targetRate * this.frameMs / 1000);

    this.frac = 0;
    this.prev = 0;                 // 上一个输入采样点，插值用
    this.frame = new Int16Array(this.frameSamples);
    this.frameN = 0;
    this.seq = 0;
    this.stopped = false;

    this.port.onmessage = (e) => {
      const d = e.data || {};
      if (d.cmd === 'stop') this.stopped = true;
      if (d.cmd === 'reset') { this.frameN = 0; this.frac = 0; this.prev = 0; }
    };
    // 把真实采样率报给主线程，页面上显示出来便于排查"为什么音质怪"
    this.port.postMessage({ type: 'ready', inputRate: sampleRate,
                            outputRate: this.targetRate,
                            step: Math.round(this.step * 1000) / 1000 });
  }

  process(inputs) {
    if (this.stopped) return true;
    const input = inputs[0];
    if (!input || !input.length) return true;
    const nch = input.length;
    const len = input[0] ? input[0].length : 0;
    if (!len) return true;

    // 多声道降混成单声道再重采样。
    // 原来只取 input[0]：桌面浏览器给单声道没问题，但 iOS 常忽略
    // channelCount:1（真机实测返回 2ch），一旦信号主要落在别的声道上，
    // 取 input[0] 就得到近乎静音的数据 —— 表现为"手机没声音、ASR 一个字都不出"。
    // 取平均既不怕选错声道，也是多麦阵列的正确降混方式。
    for (let i = 0; i < len; i++) {
      let x = 0;
      for (let c = 0; c < nch; c++) {
        const cc = input[c];
        if (cc) x += cc[i];
      }
      if (nch > 1) x /= nch;

      this.frac += 1;
      while (this.frac >= this.step) {
        this.frac -= this.step;
        const t = 1 - this.frac / this.step;        // 0 = 靠近 prev，1 = 正好是当前点
        this.push(this.prev + (x - this.prev) * t);
      }
      this.prev = x;
    }
    return true;
  }

  push(v) {
    const clamped = v < -1 ? -1 : (v > 1 ? 1 : v);
    this.frame[this.frameN++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    if (this.frameN >= this.frameSamples) this.flush();
  }

  flush() {
    const out = this.frame;
    this.frame = new Int16Array(this.frameSamples);
    this.frameN = 0;
    this.seq++;

    // dBFS 用和电脑端完全一样的口径算，两边门限才能对得上
    let sumSq = 0;
    for (let i = 0; i < out.length; i++) {
      const v = out[i] / 32768;
      sumSq += v * v;
    }
    const rms = Math.sqrt(sumSq / out.length);
    const dbfs = rms <= 1e-9 ? -96 : 20 * Math.log10(rms);

    // transferable 转移所有权，避免每 100ms 一次的内存拷贝
    this.port.postMessage(
      { type: 'pcm', seq: this.seq, pcm: out.buffer,
        dbfs: Math.round(dbfs * 10) / 10 },
      [out.buffer]);
  }
}

registerProcessor('pcm-16k', Pcm16kWorklet);
