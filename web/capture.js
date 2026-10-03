/**
 * 采集端：一次拿到摄像头+麦克风、推音频、录视频。
 *
 * 三条铁律（iOS 上踩过就知道疼）：
 *
 * 1) **只能调用一次 getUserMedia**。iOS 上第二次调用会把上一条音频轨干掉，
 *    结果就是「视频还在、声音没了」，而且不报错，极难排查。
 *    所以摄像头和麦克风必须在同一次调用里一起拿。
 *
 * 2) **必须由用户手势同步触发**。放在 setTimeout / Promise.then 里调用会被拒
 *    （Safari 的"用户意图连续性"检查），而且错误类型是 NotAllowedError，
 *    看起来像权限问题，其实不是。
 *
 * 3) **必须 HTTPS**。非安全上下文下 navigator.mediaDevices 直接是 undefined。
 *    这也是为什么整套东西要走 Tailscale 的 ts.net 域名。
 */
(function () {
  const CANDS = [
    "video/mp4;codecs=avc1.42E01E,mp4a.40.2",
    "video/mp4;codecs=h264,aac",
    "video/mp4",
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm",
  ];

  // iPadOS 13+ 的 Safari 会把自己伪装成 Mac，所以要连 maxTouchPoints 一起判。
  const IS_IOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  function pickMime() {
    if (typeof MediaRecorder === "undefined") return "";
    for (const c of CANDS) {
      try { if (MediaRecorder.isTypeSupported(c)) return c; } catch (_) {}
    }
    return "";
  }

  class Capture {
    constructor(cfg, hooks) {
      this.cfg = cfg;                       // /api/config 的返回值
      this.hooks = hooks || {};
      this.stream = null;
      this.video = null;
      this.audioCtx = null;
      this.worklet = null;
      this.ws = null;
      this.rec = null;
      this.recSid = "";
      this.recSeq = 0;
      this.recQueue = [];
      this.recSending = false;
      this.recDropped = 0;
      this._finishing = false;      // 「队列发完之后要拼接」的挂起标志
      this.running = false;
      this.wakeLock = null;
      this.settings = {};
      this._reconnect = 0;
      this._wsUrl = "";
      this._script = "";
      this._shouldRun = false;
    }

    // ---------------- 启动（必须在用户手势里同步调用） ----------------

    async start(videoEl, scriptText) {
      if (this.running) return this.settings;
      this._script = scriptText || "";
      this.video = videoEl;

      const r = this.cfg.record || {};
      const wantW = r.width || 1440;
      const wantH = r.height || 1080;

      // 用 ideal 而不是 exact：一次调用就要成功，不合适的分辨率让系统自己退
      const constraints = {
        video: {
          facingMode: { ideal: "user" },              // 前置
          width: { ideal: wantW },
          height: { ideal: wantH },
          frameRate: { ideal: r.fps || 30 },
          aspectRatio: { ideal: wantW / wantH },      // 1440/1080 = 4:3
        },
        // 拿裸麦：提词只需要"能不能听清字"，不需要通话级的降噪，
        // 而且 AGC 会把停顿时的底噪抬起来，干扰停顿检测。
        //
        // ⚠️ iOS 是例外，必须保留 AGC：iPhone 麦克风的原始输出电平极低，
        // 系统平时全靠 AGC 把它放大。显式关掉之后真机实测录音 mean_volume 只有
        // -77dB、峰值 -44.9dB（近乎静音），服务端 dbfs≈-74 过不了噪声门，
        // asr.chars 一直是 0 —— 一个字都识别不出来。桌面浏览器继续关掉。
        // 不必担心停顿检测：音频门是自适应的（audio.py 的 gate_adaptive +
        // gate_floor_window_frames，3 秒窗口学习底噪），门限会跟着抬上去。
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: IS_IOS,
          channelCount: 1,
        },
      };

      this.stream = await navigator.mediaDevices.getUserMedia(constraints);
      this._shouldRun = true;
      this.running = true;

      const vt = this.stream.getVideoTracks()[0];
      const at = this.stream.getAudioTracks()[0];
      this.settings = {
        video: vt ? vt.getSettings() : {},
        audio: at ? at.getSettings() : {},
        mime: pickMime(),
        label: vt ? vt.label : "",
      };

      this.video.srcObject = this.stream;
      this.video.muted = true;          // 绝不把麦克风回放出来，会啸叫
      this.video.setAttribute("playsinline", "");
      try { await this.video.play(); } catch (_) {}

      await this._startAudio();
      this._openWs();
      this._startRecorder();
      this._keepAwake();
      return this.settings;
    }

    async _startAudio() {
      const AC = window.AudioContext || window.webkitAudioContext;
      this.audioCtx = new AC();
      // iOS 上 AudioContext 常常是 suspended，必须显式 resume
      if (this.audioCtx.state === "suspended") await this.audioCtx.resume();

      const url = new URL("pcm-worklet.js", location.href).href;
      await this.audioCtx.audioWorklet.addModule(url);

      const src = this.audioCtx.createMediaStreamSource(this.stream);
      this.worklet = new AudioWorkletNode(this.audioCtx, "pcm-16k", {
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
        processorOptions: { targetRate: 16000, frameMs: (this.cfg.audio || {}).frameMs || 100 },
      });
      this.worklet.port.onmessage = (e) => {
        const d = e.data || {};
        if (d.type === "ready") {
          this.settings.worklet = d;
          if (this.hooks.onStatus) {
            this.hooks.onStatus({ kind: "audio-ready",
              text: `采集 ${d.inputRate}Hz → ${d.outputRate}Hz（步长 ${d.step}）` });
          }
          return;
        }
        if (d.type === "pcm") {
          if (this.hooks.onLevel) this.hooks.onLevel(d.dbfs, d.seq);
          this._sendPcm(d.pcm);
        }
      };

      // 必须连到 destination 才会被驱动，但 gain 设 0 ——
      // 不连就没声音处理，连大声就会啸叫，所以中间垫一个静音增益
      const mute = this.audioCtx.createGain();
      mute.gain.value = 0;
      src.connect(this.worklet);
      this.worklet.connect(mute);
      mute.connect(this.audioCtx.destination);
    }

    // ---------------- WebSocket ----------------

    wsUrl() {
      const https = location.protocol === "https:";
      const port = https
        ? ((this.cfg.wsPortPublic) || 8444)
        : ((this.cfg.wsPort) || 8788);
      return (https ? "wss://" : "ws://") + location.hostname + ":" + port + "/";
    }

    _openWs() {
      const url = this.wsUrl();
      this._wsUrl = url;
      try {
        this.ws = new WebSocket(url);
      } catch (exc) {
        this._status("ws-error", "WebSocket 建不起来：" + exc.message);
        return;
      }
      this.ws.binaryType = "arraybuffer";
      this.ws.onopen = () => {
        this._reconnect = 0;
        this._status("ws-open", "已连接 " + url);
        this.send({ t: "hello", sid: this.recSid,
                    mime: this.settings.mime, video: this.settings.video,
                    audio: this.settings.audio });
        if (this._script) this.send({ t: "script", text: this._script });
      };
      this.ws.onmessage = (e) => {
        if (typeof e.data !== "string") return;
        let msg;
        try { msg = JSON.parse(e.data); } catch (_) { return; }
        if (msg.t === "cursor" && this.hooks.onCursor) this.hooks.onCursor(msg);
        else if (msg.t === "chunk" && this.hooks.onChunk) this.hooks.onChunk(msg);
        else if (msg.t === "state" && this.hooks.onState) this.hooks.onState(msg);
        else if (msg.t === "script" && this.hooks.onScriptAck) this.hooks.onScriptAck(msg);
        else if (msg.t === "rec" && this.hooks.onRec) this.hooks.onRec(msg);
        else if (msg.t === "ready") this._status("asr-ready",
          `识别就绪：${msg.model} @ ${msg.device}，chunk ${msg.chunkMs}ms`);
        else if (msg.t === "error") this._status("error", msg.msg);
      };
      this.ws.onclose = () => {
        this._status("ws-closed", "连接断开");
        if (this._shouldRun) {
          // 退避重连。断开期间音频帧直接丢 —— 宁可丢几秒音频，
          // 也不要在内存里堆一堆积压帧、重连之后一次性灌进去
          const wait = Math.min(5000, 300 * Math.pow(2, this._reconnect++));
          setTimeout(() => { if (this._shouldRun) this._openWs(); }, wait);
        }
      };
      this.ws.onerror = () => this._status("ws-error", "连接出错 " + url);
    }

    _sendPcm(buf) {
      const ws = this.ws;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      try {
        ws.send(buf);
      } catch (_) {
        this._status("ws-error", "发送音频失败");
      }
    }

    send(obj) {
      const ws = this.ws;
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      ws.send(JSON.stringify(obj));
      return true;
    }

    setScript(text) {
      this._script = text || "";
      return this.send({ t: "script", text: this._script });
    }

    resetTo(clause) {
      return this.send({ t: "reset", clause: clause || 0 });
    }

    endUtterance() {
      return this.send({ t: "end" });
    }

    // ---------------- 录像 ----------------

    _startRecorder() {
      const mime = this.settings.mime;
      if (!mime || typeof MediaRecorder === "undefined") {
        this._status("rec-off", "浏览器不支持 MediaRecorder，本次只提词不录像");
        return;
      }
      const r = this.cfg.record || {};
      const opts = { mimeType: mime };
      // Safari 默认就是 10Mbps（实测 1080p 约 9.75Mbps），够用；
      // 只有配置里显式给了才覆盖，免得画蛇添足
      if (r.videoBitsPerSecond) opts.videoBitsPerSecond = r.videoBitsPerSecond;

      this.recSid = "rec" + Date.now().toString(36);
      this._finishing = false;       // 新一场开始，清掉上一场的挂起标志
      try {
        this.rec = new MediaRecorder(this.stream, opts);
      } catch (exc) {
        this._status("rec-off", "MediaRecorder 起不来：" + exc.message);
        return;
      }
      this.rec.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) this._enqueuePart(e.data);
      };
      this.rec.onerror = (e) => this._status("rec-error",
        "录制出错：" + ((e.error && e.error.name) || "unknown"));
      this.rec.onstop = () => {
        // 停下来先把队列发完，再让后端拼接
        this._pumpParts(true);
      };

      fetch("/api/rec/" + this.recSid + "/begin?mime=" + encodeURIComponent(mime),
            { method: "POST" })
        .then(() => this._status("rec-on", "开始录像 " + mime))
        .catch(() => this._status("rec-error", "录像会话建不起来"));

      const ts = r.timesliceMs || 3000;
      this.rec.start(ts);          // 每 ts 毫秒出一个分片
      this._status("rec-on", `分片 ${ts}ms`);
    }

    _enqueuePart(blob) {
      this.recSeq++;
      this.recQueue.push({ seq: this.recSeq, blob, tries: 0 });
      if (this.hooks.onRec) {
        this.hooks.onRec({ t: "local", seq: this.recSeq, bytes: blob.size,
                           queued: this.recQueue.length });
      }
      this._pumpParts(false);
    }

    async _pumpParts(finishing) {
      // ⚠️ finishing 不能只靠参数传：如果点「停止」时正好有一个分片在上传
      //    （recSending=true），这次调用会被下面的 return 直接挡掉，finishing
      //    就丢了 —— 队列发完也不会去拼接，成品 mp4 永远出不来
      //    （2026-10-03 真机连踩 6 场）。改成实例字段：谁说 finishing 都记下来，
      //    等队列清空的那次递归一定兑现。
      if (finishing) this._finishing = true;
      if (this.recSending) return;
      const item = this.recQueue.shift();
      if (!item) {
        if (this._finishing) {
          this._finishing = false;
          this._finalize();
        }
        return;
      }
      this.recSending = true;
      const url = "/api/rec/" + this.recSid + "/" + item.seq;
      try {
        const res = await fetch(url, { method: "POST", body: item.blob });
        if (!res.ok) throw new Error("HTTP " + res.status);
      } catch (exc) {
        item.tries++;
        if (item.tries < 4) {
          this.recQueue.unshift(item);                 // 重试
          this.recSending = false;
          setTimeout(() => this._pumpParts(false), 200 * item.tries);
          return;
        }
        this.recDropped++;
        this._status("rec-error",
          `分片 ${item.seq} 上传失败（已丢 ${this.recDropped} 片）：${exc.message}`);
      }
      this.recSending = false;
      // 队列积压说明上行带宽不够，报出来
      if (this.recQueue.length > 6 && this.hooks.onStatus) {
        this.hooks.onStatus({ kind: "rec-slow",
          text: `分片积压 ${this.recQueue.length} 个，上行带宽可能不够` });
      }
      this._pumpParts(false);
    }

    async _finalize() {
      let j = null;
      try {
        const res = await fetch("/api/rec/" + this.recSid + "/finalize",
                                { method: "POST" });
        j = await res.json();
        this._status("rec-done",
          `录像完成：${j.parts} 片 / ${(j.bytes / 1048576).toFixed(0)}MB / ` +
          `${j.durationS}s` + (j.error ? `（${j.error}）` : ""));
      } catch (exc) {
        this._status("rec-error", "拼接失败：" + exc.message);
      }
      // 不论成败都回调一次：调用方要据此决定是弹「保存/丢弃」还是报失败
      if (this.hooks.onRecDone) this.hooks.onRecDone(j);
    }

    // ---------------- 常亮 / 生命周期 ----------------

    async _keepAwake() {
      try {
        if ("wakeLock" in navigator) {
          this.wakeLock = await navigator.wakeLock.request("screen");
          this._status("wake", "屏幕常亮已开启");
        } else {
          this._status("wake", "不支持屏幕常亮，请用「设置-显示与亮度-自动锁定-永不」");
        }
      } catch (exc) {
        this._status("wake", "屏幕常亮申请失败：" + exc.message);
      }
    }

    async stop() {
      this._shouldRun = false;
      this.running = false;
      try { if (this.rec && this.rec.state !== "inactive") this.rec.stop(); } catch (_) {}
      try { if (this.worklet) { this.worklet.port.postMessage({ cmd: "stop" }); this.worklet.disconnect(); } } catch (_) {}
      try { if (this.audioCtx) await this.audioCtx.close(); } catch (_) {}
      try { if (this.ws) this.ws.close(); } catch (_) {}
      try { if (this.wakeLock) await this.wakeLock.release(); } catch (_) {}
      try {
        if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
      } catch (_) {}
      this.worklet = this.audioCtx = this.ws = this.rec = this.stream = null;
    }

    _status(kind, text) {
      if (this.hooks.onStatus) this.hooks.onStatus({ kind, text });
    }
  }

  window.Capture = Capture;
})();
