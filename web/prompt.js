/**
 * 提词渲染：把「读到第几个字」变成一个连续、平稳移动的光标。
 *
 * 这里有三个必须讲清楚的设计决定：
 *
 * 1) 为什么要外推（extrapolation）
 *    ASR 每 600ms 才给一次结果。如果只在收到结果时把光标跳一下，
 *    视觉上是"一顿一顿"地蹦字。所以用最近 1.5 秒估计「字/秒」速度，
 *    在两个结果之间让光标连续前进；新结果到达时**只校正不跳回**。
 *
 * 2) 为什么有 lead（预读提前量），而且它不是"延迟补偿"
 *    实测（server/replay.py --truth）：光标中位落后说话人 2.5 字 ≈ 633ms。
 *    这是链路物理决定的，改不掉。但口播时人本来就是边读边想、眼睛要领先，
 *    所以把高亮**整体往前推 lead 个字**（默认 3 字 ≈ 760ms），
 *    正好抵消这 633ms，光标就和嘴基本同步了。
 *    它不是补偿，它是"让眼睛舒服地领先"。
 *
 * 3) 为什么滚动是按句跳、而不是逐字连续滚
 *    屏幕随着每个字连续移动，看久了会晕，而且换气时会抖。
 *    所以**滚动只在换句时移动**，句子内部的进度用一条白色扫光表达。
 *    这样画面是静的、进度是动的 —— 更稳、也更省性能。
 */
(function () {
  const UNSUPPORTED = !(window.CSS && CSS.supports &&
    (CSS.supports('-webkit-background-clip', 'text') ||
     CSS.supports('background-clip', 'text')));

  // 当前句的扫光下限，见 paintWipe 的注释
  const WIPE_FLOOR = 0.12;

  class Prompter {
    constructor(cfg, els) {
      this.cfg = Object.assign({
        leadChars: 3,
        extrapolateAlpha: 0.8,
        velocityWindowMs: 1500,
        minCharsPerSec: 1.0,
        maxCharsPerSec: 12.0,
        gateMarginDb: 9,
        gateFloorMinDb: -50,
        gateFloorMaxDb: -32,
        stopFrames: 3,
        resumeFrames: 2,
      }, cfg || {});
      this.els = els;                 // {band, lines}
      this.clauses = [];
      this.lineHeight = 38;
      this.pitch = 0;                 // 实测句间距（line-height + margin），定位用
      this.disp = 0;                  // 外推出来的连续位置（归一化字序号）
      this.p = 0;                     // 后端确认的位置
      this.lead = this.cfg.leadChars;
      this.velocity = 0;              // 字/秒
      this.hist = [];                 // [{t, p}] 估速用
      this.lastCursorAt = 0;
      this.curIdx = -1;
      this.curEl = null;
      this.clEls = [];                // 缓存的句子元素，批量打「已读」标记用

      // 本地停顿检测（和后端同一套公式，见 server/audio.py 的 LevelMeter）
      this.dbfsHist = [];
      this.silentRun = 0;
      this.speechRun = 0;
      this.speaking = false;
      this.frozenLocal = true;
      this.frozenRemote = true;
      this.lastDbfs = -96;
      this.gateDb = -45;

      this.running = false;
      this._raf = 0;
      this._lastT = 0;
      this.stats = { frames: 0, jumps: 0, maxV: 0 };
    }

    // ---------------- 稿件 ----------------

    loadScript(scriptText, clauses) {
      this.clauses = clauses || [];
      this.disp = this.p = 0;
      this.hist = [];
      this.velocity = 0;
      this.curIdx = -1;
      this.curEl = null;

      const wrap = this.els.lines;
      wrap.innerHTML = "";
      const frag = document.createDocumentFragment();
      for (const c of this.clauses) {
        const d = document.createElement("div");
        d.className = "cl";
        d.dataset.i = String(c.i);
        const sp = document.createElement("span");
        sp.className = "wipe";
        sp.textContent = c.text;
        d.appendChild(sp);
        frag.appendChild(d);
      }
      wrap.appendChild(frag);
      // 缓存句子元素：换句时要批量打「已读」标记，每次 querySelectorAll 太浪费
      this.clEls = Array.prototype.slice.call(wrap.querySelectorAll(".cl"));
      if (UNSUPPORTED) wrap.classList.add("no-wipe");
      this.measure();
      this.scrollTo(0, true);
      return this.clauses.length;
    }

    measure() {
      const cls = this.els.lines.querySelectorAll(".cl");
      const first = cls[0];
      if (!first) return;
      const lh = parseFloat(getComputedStyle(first).lineHeight);
      if (lh > 0) this.lineHeight = lh;
      // 行距要用**实测句间距**，不能用 line-height：
      // 每句还带 margin-bottom，两者差 6px，用它定位会让当前句慢慢漂移。
      if (cls.length > 1) {
        const p = cls[1].offsetTop - cls[0].offsetTop;
        if (p > 0) this.pitch = p;
      }
      if (!this.pitch) this.pitch = first.offsetHeight || lh;
    }

    // ---------------- 输入 ----------------

    /** 后端推来的权威位置。 */
    onCursor(msg) {
      const now = performance.now();
      const prevP = this.p;
      let p = Number(msg.p) || 0;
      const rewound = !!msg.rewound;

      if (rewound || p < this.p) {
        // 回读：这是唯一允许光标往前的场合，直接重置外推状态
        this.disp = p;
        this.velocity = 0;
        this.hist = [{ t: now, p }];
        this.p = p;
        this.render(true);
        return;
      }

      this.p = p;
      const dt = now - this.lastCursorAt;
      if (this.lastCursorAt && dt > 30 && p > prevP) {
        const v = (p - prevP) / (dt / 1000);
        if (v > 0 && v < this.cfg.maxCharsPerSec * 4) {
          // 估计速度：对新样本做一阶平滑，避免单块抖动带歪光标
          this.velocity = this.cfg.extrapolateAlpha * this.velocity +
                          (1 - this.cfg.extrapolateAlpha) * v;
          this.stats.maxV = Math.max(this.stats.maxV, this.velocity);
        }
      }
      this.lastCursorAt = now;
      this.hist.push({ t: now, p });
      if (this.hist.length > 24) this.hist.shift();
      this.frozenRemote = !!msg.frozen;
    }

    /** 本地电平（worklet 每 100ms 一次）——停顿检测走这条路最快。 */
    onLevel(dbfs) {
      this.lastDbfs = dbfs;
      this.dbfsHist.push(dbfs);
      if (this.dbfsHist.length > 30) this.dbfsHist.shift();
      const floor = Math.min.apply(null, this.dbfsHist);
      this.gateDb = Math.max(this.cfg.gateFloorMinDb,
                    Math.min(floor + this.cfg.gateMarginDb, this.cfg.gateFloorMaxDb));

      if (dbfs > this.gateDb) {
        this.speechRun++;
        this.silentRun = 0;
        if (!this.speaking && this.speechRun >= this.cfg.resumeFrames) this.speaking = true;
      } else {
        this.silentRun++;
        this.speechRun = 0;
        if (this.speaking && this.silentRun >= this.cfg.stopFrames) this.speaking = false;
      }
      this.frozenLocal = !this.speaking;
    }

    get frozen() { return this.frozenLocal || this.frozenRemote; }

    // ---------------- 渲染循环 ----------------

    start() {
      if (this.running) return;
      this.running = true;
      this._lastT = performance.now();
      const tick = () => {
        if (!this.running) return;
        this.render(false);
        this._raf = requestAnimationFrame(tick);
      };
      this._raf = requestAnimationFrame(tick);
    }

    stop() {
      this.running = false;
      if (this._raf) cancelAnimationFrame(this._raf);
      this._raf = 0;
    }

    setLead(n) {
      this.lead = Math.max(-10, Math.min(15, n | 0));
      this.cfg.leadChars = this.lead;
    }

    render(force) {
      const now = performance.now();
      let dt = (now - this._lastT) / 1000;
      this._lastT = now;
      if (!(dt > 0) || dt > 0.5) dt = 0.016;      // 掉帧/切后台后不要冲动

      const normTotal = this.normTotal();
      const lo = this.p;
      const hi = Math.min(normTotal, this.p + Math.max(0, this.lead));

      if (this.frozen) {
        // 人停了 → 光标停在原地（不动，也不回退）
        this.velocity = 0;
        this.disp = Math.max(lo, Math.min(this.disp, hi));
      } else {
        let v = this.velocity;
        if (v < this.cfg.minCharsPerSec) v = this.cfg.minCharsPerSec;
        if (v > this.cfg.maxCharsPerSec) v = this.cfg.maxCharsPerSec;
        this.disp += v * dt;
      }
      // 下界是后端确认的位置，上界是"确认位置 + 预读量"
      if (this.disp < lo) this.disp = lo;
      if (this.disp > hi) this.disp = hi;

      const { idx, frac } = this.locate(this.disp);
      if (idx !== this.curIdx || force) {
        this.stats.jumps += this.curIdx >= 0 ? 1 : 0;
        this.setCurrent(idx);
        this.scrollTo(idx, true);
      }
      this.paintWipe(idx, frac);
      this.stats.frames++;
    }

    normTotal() {
      const n = this.clauses.length;
      return n ? this.clauses[n - 1].normEnd : 0;
    }

    locate(pos) {
      const cs = this.clauses;
      if (!cs.length) return { idx: -1, frac: 0 };
      if (pos <= cs[0].normStart) return { idx: 0, frac: 0 };
      for (let i = 0; i < cs.length; i++) {
        const c = cs[i];
        if (pos < c.normEnd || i === cs.length - 1) {
          const span = Math.max(1, c.normEnd - c.normStart);
          return { idx: i, frac: Math.max(0, Math.min(1, (pos - c.normStart) / span)) };
        }
      }
      return { idx: cs.length - 1, frac: 1 };
    }

    setCurrent(idx) {
      if (this.curEl) this.curEl.classList.remove("cur");
      this.curIdx = idx;
      this.curEl = this.els.lines.querySelector('.cl[data-i="' + idx + '"]');
      if (this.curEl) this.curEl.classList.add("cur");
      this.markRead(idx);
    }

    /**
     * 全量对齐句子的三种状态（为什么全量：光标回读 rewound 时标记必须能收回）。
     *
     *   done = 序号 < 当前句        → 熄灭（读过了，不用再看）
     *   next = 序号 == 当前句 + 1   → 黄色（下一段，眼睛的落点）
     *
     * 黄色给「下一段」而不是「已读」，是因为人读的时候眼睛要**提前**找位置：
     * 识别确认天生滞后约 2.5 字，黄色若跟确认走就成了「读完才标黄」；
     * 而 next 由外推位置（领先于确认）直接推出，是确定性预告，永远跑在声音前面。
     * 先用 classList.contains 探一下，避免无谓地写 DOM。
     */
    markRead(idx) {
      const els = this.clEls || [];
      for (let i = 0; i < els.length; i++) {
        const done = i < idx;
        const next = i === idx + 1;
        if (els[i].classList.contains("done") !== done) {
          els[i].classList.toggle("done", done);
        }
        if (els[i].classList.contains("next") !== next) {
          els[i].classList.toggle("next", next);
        }
      }
    }

    paintWipe(idx, frac) {
      if (!this.curEl || UNSUPPORTED) return;
      const span = this.curEl.querySelector(".wipe");
      if (!span) return;
      // 保底 12%：句子刚开头时扫光为 0，整句会显示成"未读灰"，
      // 那就看不出哪句是当前句了 —— 违背"当前分句纯白加粗"的规范。
      // 所以始终让开头一小段是纯白。
      const pct = Math.max(frac, WIPE_FLOOR) * 100;
      span.style.setProperty("--wipe", pct.toFixed(1) + "%");
    }

    /**
     * 滚动只在换句时动，而且**当前句固定在带内第 1 行**。
     *
     * 为什么锚第 1 行而不是中间某行：
     *   锚中间行需要把内容往下推，带底那行就被切掉半个（看着像 bug）。
     *   锚第 1 行则内容几乎不动，4 行全在带内。
     *   而且当前句紧贴挖孔下沿 —— 离摄像头最近，眼神最"正"。
     *   下方留灰字是即将要读的，正好当预告。
     */
    scrollTo(idx, animate) {
      const el = this.els.lines.querySelector('.cl[data-i="' + idx + '"]');
      if (!el) return;
      const pitch = this.pitch || this.lineHeight;
      const center = el.offsetTop + el.offsetHeight / 2;
      const target = center - 0.5 * pitch;
      this.els.lines.style.transition = animate
        ? "transform .28s cubic-bezier(.22,.61,.36,1)" : "none";
      this.els.lines.style.transform = "translate3d(0," + (-target).toFixed(1) + "px,0)";
    }

    // ---------------- 调试 ----------------

    info() {
      return {
        p: this.p, disp: Math.round(this.disp * 10) / 10,
        clause: this.curIdx, velocity: Math.round(this.velocity * 10) / 10,
        lead: this.lead, frozen: this.frozen, speaking: this.speaking,
        dbfs: this.lastDbfs, gateDb: Math.round(this.gateDb * 10) / 10,
        jumps: this.stats.jumps, frames: this.stats.frames,
      };
    }
  }

  window.Prompter = Prompter;
  window.PrompterUnsupported = UNSUPPORTED;
})();
