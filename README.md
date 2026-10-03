# AI 实时跟读提词器（本地 FunASR · 局域网版）

iPhone 当提词屏 + 摄像头 + 麦克风，电脑本地跑 FunASR 流式识别，
**读到哪高亮哪、停口就停滚**。音频和视频全程只在局域网内流转，不上公网，零 API 费用。

---

## 一分钟开始

```bash
cd ai-teleprompter

# 1) 起服务（首次会加载流式模型，约 3 秒）
./run.sh

# 2) Mac 上先自测（localhost 算安全上下文，直接用本机摄像头麦克风）
open http://127.0.0.1:8899/          # 提词页
open http://127.0.0.1:8899/probe.html  # 能力探针（第一次务必先跑它）
open http://127.0.0.1:8899/debug.html  # 调试台

# 3) 手机上用（需要 HTTPS，见下一节；地址用 tailscale serve status 查）
```

- **`--dry`**：只起网页不加载模型，秒开，调界面用。
- **`--script 稿子.txt`**：预置稿件并锁定，网页端就不能再改。
- 端口在 `config.json` 里：HTTP `8899`、WebSocket `8900`
  （**8787/8788 已被别的东西占了**，别再改回去）。

---

## 手机要用，必须先解决 HTTPS

iPhone Safari **强制** secure context：`http://192.168.x.x` 下
`navigator.mediaDevices` 直接不存在，权限框都不会弹，没有开关能绕。
所以走局域网也得是 https。两条路：

### 路线 A：Tailscale（推荐，省掉手机装证书）

Mac 和 iPhone 都装 Tailscale、登录**同一个账号**，然后挂两条映射：

```bash
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
$TS serve --bg --yes --https=8443 http://127.0.0.1:8899
$TS serve --bg --yes --https=8444 http://127.0.0.1:8900
$TS serve status        # 这里会打出手机端地址
```

`serve status` 的输出形如 `https://<机器名>.<你的tailnet>.ts.net:8443` ——
iPhone 用 Safari 打开它，就能拿到摄像头和麦克风。

`--bg` 是**持久化**的：重启设备、`tailscale down/up` 之后映射都会自动恢复，不用重挂。
（前提是 Tailscale 本身随开机启动，macOS 客户端默认就开着。）

> ⚠️ **域名里的机器名可能不等于本机主机名。** Tailscale 会把名字规范化成合法 DNS 标签
> —— 实测 `scutil --get LocalHostName` 给 `myMac-mini`，域名里却是 `mymac-mini`。
> 一律以 `$TS status --json` 里的 `Self.DNSName` 为准，别自己拼。

#### 换机 / 重装时的三件事

**1. 后台有三个独立开关，少一个都失败，而且报错不会指向真正缺的那个：**

| 开关 | 位置 | 没开时的报错 |
| --- | --- | --- |
| MagicDNS | `admin/dns` | 拿不到 `<机器>.<tailnet>.ts.net` 域名 |
| HTTPS Certificates | `admin/dns` → Enable HTTPS | `your Tailscale account does not support getting TLS certs`（措辞像账号问题，其实只是开关） |
| **Serve** | `login.tailscale.com/f/serve?node=<nodeid>` | `Serve is not enabled on your tailnet` |

**2. macOS 上 `brew install --cask tailscale-app` 可能装不上**：Homebrew 解压 pkg 要调
`sandbox-exec`，而它可能被系统限制（`sandbox_apply: Operation not permitted`），
`HOMEBREW_NO_SANDBOX=1` 也救不了。绕法 —— 官方 pkg 手动展开：

```bash
curl -fL -o /tmp/ts.pkg https://pkgs.tailscale.com/stable/Tailscale-latest-macos.pkg
pkgutil --expand-full /tmp/ts.pkg /tmp/ts-x
ditto /tmp/ts-x/Distribution.pkg/Payload /Applications/Tailscale.app
xattr -dr com.apple.quarantine /Applications/Tailscale.app
codesign --verify --deep --strict /Applications/Tailscale.app   # 必须通过
printf '#!/bin/sh\nexec /Applications/Tailscale.app/Contents/MacOS/Tailscale "$@"\n' > /usr/local/bin/tailscale
chmod +x /usr/local/bin/tailscale
```

**3. 挂映射**

```bash
$TS serve --bg --yes --https=8443 http://127.0.0.1:8899
$TS serve --bg --yes --https=8444 http://127.0.0.1:8900
```

iPhone 打开那个地址 → 允许摄像头+麦克风 → 分享 → 添加到主屏幕。

顺带一个好处：**域名固定**，摄像头权限只授权一次。用局域网 IP 的话
路由器一变地址就得重新授权。

> 确认走的是局域网直连而不是 Tailscale 的中继（中继会多 20~80ms 且绕出内网）：
> `$TS ping <iPhone>`，看输出是 direct 还是 relay。

### 路线 B：mkcert 自签（不依赖任何账号）

```bash
brew install mkcert && mkcert -install
mkcert -cert-file cert.pem -key-file key.pem 192.168.x.x
```

用证书起 uvicorn/uvicorn 之外的方式挂 TLS，或者前面垫一个 Caddy/nginx。
iPhone 上还要装根证书并在「设置 → 通用 → 关于本机 → 证书信任设置」里信任。
麻烦点，但完全离线。

---

## 它怎么工作

```
iPhone Safari ──(Tailscale，局域网直连)── Mac mini M1
  ① 前置摄像头全屏预览          HTTP 127.0.0.1:8899  页面/稿件/录像分片
  ② 顶部悬浮提词层（挖孔下方）◀── cursor JSON        WS  127.0.0.1:8900
  ③ AudioWorklet 48k→16k ────▶ 音频 Int16（每 100ms 一帧）
  ④ MediaRecorder 3s 分片 ───▶ POST 分片（独立连接）
                                  RingBuffer → 静音门控 → ASR 线程
                                  paraformer-zh-streaming（MPS）
                                  → 增量文本 → align.py 增量对齐 → p / 句子
```

### 四条决定了架构的约束

1. **视频分片不能走音频那条 WebSocket。** 一个 3 秒 1080p 分片约 4MB，
   TCP 先进先出会把后面的音频帧堵死，ASR 直接卡住。所以音频走 WS，
   视频走独立 HTTP POST。
2. **摄像头和麦克风必须在同一次 `getUserMedia` 里拿到。** iOS 上第二次调用
   会静默干掉前一条音轨 —— 视频还在、声音没了，还不报错。
3. **ASR 是阻塞调用，必须独立线程。** 放在 asyncio 里每 600ms 冻住 54ms，
   手机端会看到一卡一卡的心跳。
4. **必须 MPS，不能 CPU。** 见下面实测。

---

## 实测数据（Mac mini M1 / 16GB）

### 识别引擎：必须走 MPS

| device | chunk | 单块均值 | p95 | RTF | 结论 |
|---|---|---|---|---|---|
| **mps** | 600ms | **54ms** | 56ms | **0.090** | ✓ 余量 10 倍 |
| mps | 480ms | 46ms | 48ms | 0.096 | ✓ |
| cpu | 600ms | 556ms | 631ms | 0.927 | ✗ 跟不上 |
| cpu | 480ms | 507ms | 554ms | 1.057 | ✗ 跟不上 |

RTF = 推理耗时 / 音频时长，**必须 < 1**，否则队列只进不出、延迟无限增长。
M1 的 CPU 只有 ~0.93（勉强平手，实际会越积越多），**MPS 快 10 倍**。

> 顺带纠正一个常见误解：流式模型的 600ms 是**算法延迟**（chunk 边界），
> 换 GPU 也压不下来。真正能调的是 `chunk_size`。

### 端到端延迟（`--realtime --truth` 实测）

用「逐句合成 + 记录每句精确起点」造的带真值素材测出来的：

| 指标 | 实测 |
|---|---|
| **光标落后说话人** | 中位 **2.5 字 ≈ 633ms**，p90 4.0 字 ≈ 1026ms |
| **逐句命中延迟**（开口 → 光标进该句） | 中位 **590ms**，p90 940ms |
| 光标跑到说话人前面的比例 | 0%（从不超前） |
| 模型 RTF（实时节奏下） | 0.106 |
| 丢块 | 0 |

**这 633ms 改不掉，但可以抵消。** 口播时人本来就是眼睛领先、边读边想，
所以把高亮整体往前推 `lead` 个字：**设 `+3 字`（≈760ms）几乎正好抵消**，
光标就和嘴基本同步。这就是「预读提前量」而不是「延迟补偿」的原因 ——
它不是修 bug，是让眼睛舒服。

界面上拖动「预读量」滑杆即可，正负都能调。

### 识别质量（48.7 秒连续口播）

整段只错了 2 处，而且**两处都是严格同音字**（它们→他们、意志力→益智力），
正是拼音对齐专门要吸收的错误类型。对齐结果：覆盖率 100%、低置信 0/72、
重定位 0、回读 0。

---

## 对齐是怎么做的（`server/align.py`）

流式 Paraformer **不输出字级时间戳**（只有增量文本），所以「读到哪」只能靠对齐算：

1. **归一化**：NFKC → 只留汉字/字母/数字 → 中文数字逐字读法转阿拉伯数字
   （两边都转，保持一致：稿件写 `2024`、识别吐 `二零二四` 也能对上）
2. **拼音主键**：不带声调的拼音（去声调是为了避开 ASR 的调值误差），
   用来吸收同音错字
3. **滑窗 LCS + 单调指针**：在 `[p-6, p+24)` 里找最像缓冲区的落点，
   把指针推到「确实对上过的最后一个字」
4. **低置信就不动**：`<0.50` 完全不推进，`[0.50,0.75)` 只推进「从缓冲区第一个字
   开始连续命中」的那一段，绝不按比例猜 —— **宁可慢半拍，也不跳错句**
5. **跳读重定位**：连续 3 次低分后在 220 字范围里重搜，要求 ≥0.80 才接受
6. **回读回退**：人把上一句重读，走独立的回退路径 + **连续两次确认**才退，
   不污染主路径的单调性

屏幕上永远显示**稿件原文**，ASR 文本只用来定位 —— 错字不会上屏。

---

## 调参

参数集中在 `config.json`。改完重启服务。

| 想改善 | 调什么 | 方向 |
|---|---|---|
| 想更快 | `audio.chunk_ms` 600 → 480 | 少 120ms 延迟，准确率略降 |
| 光标太跳 | `prompt.extrapolate_alpha` 0.8 → 0.9 | 外推更平滑（更信历史速度） |
| 高亮偏慢/偏快 | 界面上拖「预读量」 | 或改 `prompt.lead_chars` |
| 停顿判不准 | `audio.gate_margin_db`（默认 9） | 环境吵就调大 |
| 错过跳读 | `align.reacquire_look`（默认 220） | 调大 |
| 光标乱跳 | `align.hi` 0.75 → 0.85 | 更保守，更少动 |

**改完一定先跑回放验证，别直接上手机：**

```bash
PY=~/.venvs/funasr/bin/python
$PY server/replay.py --realtime --wav tests/fixtures/say_clauses.wav \
    --truth tests/fixtures/truth.json       # 有真值，能量出光标滞后
```

---

## 音源：手机麦 / 电脑麦

默认手机收音。也可以让 **Mac 本机麦克风**收音 —— 提词跟随和成品音轨都用它，
手机只当摄像头（`capture.js` 会改用 video-only 的 MediaStream 录制，省掉一半上行流量）。

切换方式：设置面板顶部「音源」下拉（`POST /api/audio/source?src=mac&device=:<idx>`）。

实现要点：

- `server/macmic.py` 用 `ffmpeg -f avfoundation -i ":<设备>" -f s16le -` 采 raw PCM，
  **一份喂识别、一份写 wav**：PCM 直接交给 `Session.feed_pcm(source="mac")`
  （和手机上传走同一个入口，所以电平/停顿检测/对齐/ASR 全都不用改），
  wav 落在录像目录里，finalize 时由 `recorder._remux(ext_audio=...)` 替换掉视频原音轨。
- 设备列表由 `GET /api/audio/devices` 提供，**必须让用户选**，不能硬编码索引 ——
  本机的默认输入就是 BlackHole 虚拟声卡，采它等于采静音（前端会标出「虚拟设备」）。
- 想让别人也能用这个能力，注意本机要**有物理麦克风**：Mac mini 没有内置麦。

---

## 录像

Safari 的 MediaRecorder 默认就是 **10 Mbps H.264**（实测 1080p 约 9.75Mbps），
正好对上 ~10Mbps 的素材标准，所以默认不设 `videoBitsPerSecond`。

坑在容器：Safari 出的是 **fragmented MP4**，而且 MediaRecorder 从不回头写
duration，所以原始文件是 0:00、不能拖拽。收工时：

1. 所有分片**按字节序拼接**成 `merged.mp4`
2. `ffmpeg -i merged.mp4 -c copy final.mp4` 把 moov 挪到前面并补时长（不重编码）
3. 失败兜底：concat demuxer → 再不行重编码

产物在 `recordings/<sid>/final.mp4`，也可以从
`http://127.0.0.1:8899/api/rec/<sid>/file` 下载。

> ⚠️ **`final.mp4` 只在点「停止」按钮的那一刻生成。**
> 触发链：`capture.js` 的 `rec.onstop → _pumpParts(true) → _finalize()` →
> `POST /api/rec/<sid>/finalize`。**直接关页面、切后台、锁屏都不会触发**，
> 只会留下一堆分片。补拼办法：
>
> ```bash
> cd recordings/<sid>
> cat $(ls -v part_*.bin) > merged.mp4
> ffmpeg -y -i merged.mp4 -c copy final.mp4
> ```
>
> 注意 `/api/rec/<sid>/finalize` 只作用于**当前**会话（后端 `recorder.finalize` 用的是
> `self.status`，并不校验你传进去的 sid），所以补历史会话必须手工 ffmpeg，别调接口。

**分片本身就是保险**：已落盘的片段不会因为网页崩掉而丢。
缺片会直接报出来（`gaps`），不会默默给你一个断掉的视频。

> 不过 `/api/state` 里的 `rec.parts` / `rec.gaps` 是**错报** —— 实测 24 片全在，
> 它却报 `parts=1 / gaps=[1..23]`。以磁盘上的文件为准。

### 停止后的审阅面板

点「停止」→ 等 `capture.js` 的 `onRecDone` 回调 → 弹审阅面板，内嵌 `<video>`
直接播放 `GET /api/rec/<sid>/file`（不用等文件下载完）：

- **保存** —— 保留，关面板。文件仍在 `recordings/<sid>/final.mp4`。
- **丢弃** —— `POST /api/rec/<sid>/discard` → `Recorder.discard()` 删掉整个会话目录。

> `discard` 与 `finalize` 的校验强度**故意不同**：`discard` 会先做 sid 正则白名单、
> 再要求与当前会话一致、最后确认目录落在 `recordings` 根下且末级名字就是 sid。
> 删除不可逆，不能让一个传错的 sid 把别的会话删掉。

---

## 文件地图

```
ai-teleprompter/
├── run.sh                  启动脚本（会提示 tailscale 怎么挂）
├── config.json             所有参数
├── requirements.txt        依赖（其实只差 pypinyin）
├── server/
│   ├── main.py             入口：组装 HTTP + WS + ASR 线程
│   ├── session.py          会话（单例）：稿件/对齐/电平/录像/订阅广播
│   ├── align.py            ★ 增量对齐（拼音 + 滑窗 LCS + 单调指针）
│   ├── asr.py              流式引擎（阻塞推理关进独立线程，MPS）
│   ├── audio.py            16k 缓冲、dBFS、自适应静音门控
│   ├── web.py              HTTP（标准库，含目录穿越防护）
│   ├── recorder.py         录像分片落盘 + ffmpeg 拼接（含响度归一 / 换音轨）
│   ├── macmic.py           本机麦克风采集（电脑收音：一份喂识别、一份写 wav）
│   ├── protocol.py         WS 消息定义
│   └── replay.py           ★ 离线回放（不用手机就能调参）
├── web/
│   ├── index.html          手机提词页（三态配色 / 四角拖拽缩放 / 运行时字号 / 录后审阅）
│   ├── prompt.js           ★ 游标外推 / 停顿冻结 / 渲染
│   ├── capture.js          采集：一次 getUserMedia + WS + 分片录像
│   ├── pcm-worklet.js      重采样 16k + 100ms 打包
│   ├── probe.html          能力探针（先跑它）
│   └── debug.html          Mac 上的调试台
├── tools/
│   ├── fetch_stream_model.py   拉流式模型 + 空跑验证
│   ├── bench_stream.py         性能基准（cpu/mps × chunk）
│   ├── make_truth_fixture.py   造带真值的回放素材
│   ├── ws_test_client.py       服务端整链路集成测试
│   └── browser_check.py        无头 Chrome 跑前端（含 4 个坑的注释）
└── tests/
    ├── test_align.py       对齐单测（39 项断言）
    └── fixtures/           稿件、真值素材、回放记录
```

---

## 自检与测试

```bash
PY=~/.venvs/funasr/bin/python

$PY tests/test_align.py          # 对齐算法（39 项断言，含错字/漏读/跳读/回读）
$PY server/audio.py              # 音频缓冲与静音门控
$PY -m server.web                # HTTP 层
$PY server/asr.py                # ASR 引擎（真音频跑 48.7s）
$PY server/replay.py             # 整条后端链路（全速）
$PY tools/ws_test_client.py --realtime   # 服务端 WS 集成（需先起服务）
$PY tools/browser_check.py       # 前端整链路（无头 Chrome，需先起服务）
$PY tools/bench_stream.py --all  # 性能对比矩阵
```

## 手机端验收

1. 先跑 **`probe.html`**，量出这台 iPhone 的真实分辨率/帧率/码率/实际 mimeType
   （4:3 1440×1080 给不给，只有量了才知道）
2. 提词页点「开始提词」→ 授权 → 应该看到：摄像头全屏 + 顶部半透黑提词带
3. 念稿：当前句纯白加粗、句子内部有一条白色扫光推进；停口 → 扫光立刻停
4. 底部跑道条：白条是后端确认位置，绿点是你眼睛看到的位置（两者差距 = 预读量）

---

## 排障

| 现象 | 原因 / 办法 |
|---|---|
| 手机上权限框根本不弹 | 不是 HTTPS。见上面「HTTPS」一节 |
| `NotAllowedError` | 非安全上下文 / 权限被拒 / 不在用户手势里。检查 HTTPS 与设置里的 Safari 相机权限 |
| `NotReadableError` | 摄像头被别的 App 占着（微信、相机）。全关掉 |
| 视频在跑但没声音/没字幕 | getUserMedia 被调了两次，音轨被杀。确认没在别处再调 |
| 服务连不上、ping 也不通 | 先看服务日志有没有 `Address already in use`（8899/8900 被占） |
| RTF ≈ 1 或一直丢块 | 掉到 CPU 了。看调试台的 device 字段，应该是 `mps` |
| 每次换台设备权限都要重授权 | 用了局域网 IP。换 Tailscale 域名，权限按 origin 记 |
| 屏幕自动锁了导致中断 | 用了「设置 → 显示与亮度 → 自动锁定 → 永不」，或确认 wakeLock 生效 |
| 网页白屏 | 打开 `/api/state` 看服务端是否活着；`/api/config` 看配置是否可读 |
| 录像时长 0:00 / 不能拖 | remux 失败。看 `recordings/<sid>/` 里的 ffmpeg 输出 |
| **提词完全不跟读、HUD 电平一直趴在 -70 附近** | **iPhone 采集电平过低**（2026-10-03 真机踩过）。原因是 iOS 上 `autoGainControl` 被关掉：iPhone 麦克风原始输出极弱，系统全靠 AGC 放大，关掉后实录 `mean_volume` 只有 **-77dB**。`capture.js` 已按 `IS_IOS` 分支保留 AGC；另外 iOS 会忽略 `channelCount:1` 返回 2ch，`pcm-worklet.js` 已改成多声道平均降混 |
| 手机上有画面、有电平，但一个字都不识别 | 先看 `/api/state` 的 `asr.chars` 是否恒为 0，再拿 `recordings/<sid>/part_00001.bin` 去量电平：`ffmpeg -i part_00001.bin -map 0:a -af volumedetect -f null -`（**只有第一个分片带 moov 能独立解码**） |
| **找不到 `final.mp4`** | 没点「停止」；或分片都在但 `merged.mp4` 不存在（= finalize 从未执行，旧版有竞态 bug：点停止时撞上分片上传就丢标志，2026-10-03 已修，刷新页面即生效）。补拼见「录像」一节 |

---

## 关于无头浏览器测试的一个坑（别踩第二遍）

`tools/browser_check.py` 里必须带这几个开关：

```
--no-sandbox --disable-gpu-sandbox --disable-gpu --enable-unsafe-swiftshader
```

否则在本环境下 Chrome 的子进程沙箱会初始化失败：

```
sandbox initialization failed: Operation not permitted
GPU process exited unexpectedly: exit_code=5      （连挂 6 次）
FATAL: GPU process isn't usable. Goodbye.
```

Chrome 直接自杀，表现出来却是 DevTools 的 WebSocket「无关闭帧被掐断」，
看着像协议问题，会白查半天。**另外别把 Chrome 的 stderr 丢进 DEVNULL** ——
上面那几行就是答案。
