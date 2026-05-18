# CLAUDE.md

本文件指导 Claude Code 在此仓库中工作。

## 项目概览

LiveTranslate 是 Windows 实时音频翻译系统。通过 WASAPI 环回采集系统音频，经过语音识别和 LLM API 翻译，在透明悬浮窗上显示结果。

**当前阶段**: Phase 0 Python 原型（Phase 1 将是 C++ DirectShow 音频 Tap 过滤器）。

## 运行

```bash
# 必须使用项目虚拟环境（系统 Python 缺少依赖）
.venv/Scripts/python.exe main.py
```

Linter: `ruff`（全局安装）。运行 `python -m ruff check --select F,E,W --ignore E501,E402 *.py`。E402 被忽略因为 `main.py` 需要在 PyQt6 之前 import torch。

## 架构

管道在后台线程运行: **音频采集 (32ms 块) -> VAD -> ASR -> 翻译 (异步) -> 悬浮窗**

```
main.py (LiveTranslateApp)
  |-- model_manager.py       模型检测、下载、缓存工具
  |-- audio_capture.py       WASAPI 环回 + 麦克风混音，自动重连
  |-- vad_processor.py       Silero VAD / 能量 / 禁用三种模式，渐进静音 + 回溯切分
  |-- asr_engine.py          faster-whisper (Whisper) 后端
  |-- asr_sensevoice.py      FunASR SenseVoice 后端（日语更好）
  |-- asr_funasr_nano.py     FunASR Nano 后端
  |-- asr_anime_whisper.py   Anime-Whisper 后端（litagin/anime-whisper, 日语动画/Galgame）
  |-- translator.py          OpenAI 兼容 API 客户端，流式、JSON schema、上下文
  |-- transcript_writer.py   每会话转录持久化（原文/译文/全部）
  |-- subtitle_overlay.py    PyQt6 透明悬浮窗（2 行标题栏：控件 + 模型/语言组合框）
  |-- subtitle_window.py     独立字幕窗口（供 OBS 采集，描边文字，动画）
  |-- subtitle_settings.py   字幕窗口设置 UI（网格布局，文本行编辑器）
  |-- control_panel.py       设置 UI（7 标签页：VAD/ASR, 翻译, 样式, 字幕, 基准, 缓存, 更新日志）
  |-- dialogs.py             设置向导、模型下载/加载对话框、模型编辑对话框
  |-- benchmark.py           翻译基准测试 (BENCH_SENTENCES, run_benchmark())
  |-- log_window.py          实时日志查看器
  |-- i18n.py                UI 国际化（zh/en yaml, LANGUAGES 列表）
  |-- funasr_nano/           本地 FunASR Nano 包（model.py, ctc.py）
```

### 线程模型

- **主线程**: Qt 事件循环（全 UI）
- **采集线程**: `_capture_loop` — 读音频块、喂 VAD、塞 ASR 队列
- **ASR 线程**: `_asr_loop` — 出队、跑 ASR（阻塞）、提交翻译到 ThreadPoolExecutor
- **ASR 加载**: `_switch_asr_engine` 后台线程（~3-8s），配合 `_ModelLoadDialog` 轮询
- **翻译**: `ThreadPoolExecutor(max_workers=8+)` 跑 `_translate_async` — 非阻塞 LLM API
- 跨线程 UI 更新用 **Qt 信号**（如 `add_message_signal`、`update_translation_signal`）
- `_asr_queue`（Queue, maxsize=16）解耦采集→ASR；满时丢弃最旧条目
- `_asr_ready` 标记 ASR 就绪状态；加载中管道丢弃音频段

### 配置

- `config.yaml` — 基础配置（音频、ASR、翻译、字幕默认值）
- `user_settings.json` — 运行时设置（模型、VAD 参数、ASR 引擎选择、可选 `cache_path`），加载时优先级高于 config.yaml
- `i18n/{lang}.yaml` — UI 字符串翻译（zh.yaml / en.yaml），`i18n.py` 在 import 时根据系统 locale 加载

### 模型配置

`user_settings.json` 中每个模型包含: `name`, `api_base`, `api_key`, `model`, `proxy`（"none"/"system"/自定义 URL），以及可选的模型级标志:

- `no_system_role` (bool): 将 system prompt 合并到 user message 中，用于拒绝 system role 的 API（如 Qwen-MT）
- `no_think` (bool, 默认 True): 传入 `extra_body={"enable_thinking": False}` 禁用思维链（Qwen3 等思考模型）
- `streaming` (bool, 默认 True): 逐模型流式开关；流式模式通过 `translate_iter()` 生成器输出部分文本
- `json_response` (bool, 默认 False): 使用 `response_format={"type": "json_schema"}` 配合 schema `{"t": "string"}` 作结构化输出；与流式 UI 互斥
- `context_turns` (int, 默认 0): 在消息中包含最近 N 组（原文, 译文）作为多轮上下文
- `input_price`/`output_price` (float, 每 1M tokens): 用于 MonitorBar 显示的成本估算
- `overrides` (dict, 可选): 覆盖默认的 OpenAI chat-completion 参数 — 支持 `temperature`, `top_p`, `max_tokens`, `frequency_penalty`, `presence_penalty`, `seed`。只在 dict 中的键会被发送；缺失的键回退到构造函数默认值
- `extra_body` (dict, 可选): 提供商特定参数（如 `thinking_budget`, `reasoning_effort`）合并到请求的 `extra_body`。与 `no_think` 的 `enable_thinking=false` 同时设置时会合并
- Proxy 处理: `proxy="none"` 用 `httpx.Client(trust_env=False)` 绕过系统代理；`proxy="system"` 用默认 httpx 行为（环境变量）
- `Translator._build_request_kwargs()` 是唯一组装点 — 注入 `overrides`，合并 `extra_body` 与 `no_think`，设置 `response_format`。三条代码路径（`_translate_sync`, `_translate_streaming`, 流式 `translate_iter`）都通过它
- ModelEditDialog "高级参数" 组使用 `[Override] 复选框 + 值控件` 模式 — 未勾选的行不会写入 `overrides` dict，保持向后兼容。`extra_body` 是多行 JSON 文本字段，确认时校验
- 在 ModelEditDialog 中编辑当前激活的模型会触发 `model_changed` 信号，立即应用更改

### 字幕窗口 (subtitle_window.py)

独立透明窗口，供 OBS 采集，与主悬浮窗分离:
- `SubtitleWindow`: 无边框、透明、中键拖动、超时自动隐藏、默认位置 (100, 100)
- `_SubtitleTextWidget`: 基于 QPainterPath 的逐行描边文字渲染，自动换行，入场/退场动画
- 文字渲染到缓存的 QPixmap；动画只 blit 缓存图像（不逐帧渲染路径）
- 每行独立字体、颜色、描边、对齐、动画设置
- 长文本在标点/单词边界自动换行，按换行行数扩展控件高度
- `desired_height()` 基于换行行数返回高度（不会返回 0），防止窗口缩到 0
- 窗口高度在换行数变化时平滑动画（150ms OutCubic），保持垂直中心稳定
- 动画顺序: 退场动画 → 高度变化 → 入场动画
- 设置 UI 在 `subtitle_settings.py`: 网格布局，文本行列表，双击编辑对话框

### 悬浮窗 UI (subtitle_overlay.py)

DragHandle 是 2 行标题栏:
- **第 1 行**: 可拖动标题 + 操作按钮（隐藏, 字幕, 暂停/运行, 清空, 完整/精简, 设置, 退出）
- **第 2 行**: 复选框（点击穿透, 置顶, 自动滚动, 任务栏）+ 模型组合框 + 目标语言组合框

MonitorBar 显示: ASR 设备、CPU/内存/GPU 用量、ASR/翻译计数、token 用量及费用估算（根据 UI 语言显示 ¥/$）。

内存监控: `LiveTranslateApp._check_memory_threshold()` 在 RSS 超 4GB 时记警告并触发托盘通知。每段处理后输出 MEM 日志（RSS 增量、GPU 分配/预留），30s 周期 tick。

### 转录持久化 (transcript_writer.py)

`TranscriptWriter` 将 ASR→翻译 写入 `./transcripts/livetrans_{session_ts}_{kind}.txt`:
- **每会话三文件**: `original`、`translation`、`all`（原文+译文结对）
- 行缓冲（`buffering=1`），追加模式 — `tail -f` 可用，崩溃不丢数据
- 运行时通过 `auto_save_transcript` 开关（设置 → ASR 选项卡）
- `write_original()` 在 `_process_segment` 调用；`write_translation()` 在 `_translate_async` 调用
- `finalize_no_translation()` 处理同语言/错误段
- `close()` 刷新并关闭所有文件（`stop()` 时调用）

### 导出

悬浮窗右键 + 托盘菜单 → 导出消息为 `.txt` 文件:
- `subtitle_overlay.py:export_messages(mode)` — 模式 `"original"` / `"translation"` / `"both"`
- `QFileDialog.getSaveFileName` 带时间戳默认文件名
- 与 TranscriptWriter 相同时间戳格式

样式系统:
- `DEFAULT_STYLE` 和 `STYLE_PRESETS` 定义在 `subtitle_overlay.py` — 14 个预设，包括终端主题（Dracula, Nord, Monokai, Solarized, Gruvbox, Tokyo Night, Catppuccin, One Dark, Everforest, Kanagawa）
- 默认样式是高对比度（纯黑背景、白译文、14pt）
- 原文和译文有独立的 `font_family` 字段（`original_font_family`, `translation_font_family`）
- `SubtitleOverlay.apply_style(style)` 更新容器/标题栏背景、窗口透明度，重建所有消息 HTML
- 样式 dict 存在 `user_settings.json` 的 `"style"` 键下；通过 `settings_changed` 信号 → `main.py` → `overlay.apply_style()`
- 向后兼容: `apply_style()` 自动将旧 `font_family` 键迁移为拆分字段

关键悬浮窗功能:
- **置顶**: 切换 `WindowStaysOnTopHint`；需要 `setWindowFlags()` + `show()` 才生效
- **点击穿透**: 在滚动区域使用 Win32 `WS_EX_TRANSPARENT`，保持标题栏可交互
- **自动滚动**: 控制新消息/翻译是否自动滚到底部
- **模型组合框**: 来自 `user_settings.json` 的模型列表；切换时发射 `model_switch_requested` 信号
- **目标语言组合框**: 发射 `target_language_changed`；启动时从设置同步
- **精简模式动画**: 在 full 和 minimumHeight 之间切换，200ms 尺寸动画；用 `frameGeometry()` 获取实际窗口大小避免 Windows MINMAXINFO 不匹配；高度差 < 10px 跳过动画
- **位置持久化**: `moveEvent`/`resizeEvent` 500ms 防抖保存 `overlay_x/y/w/h` 到 `user_settings.json`；启动时恢复
- **重置位置**: ControlPanel 的 `reset_positions` 信号；字幕窗口 → (100, 100)，悬浮窗 → 屏幕右下角

### 设置 UX

- **防抖自动保存**: 所有控制面板设置（组合框、数字选择框）300ms 防抖自动保存，无需手动保存按钮
- **滑块特殊处理**: VAD/能量滑块实时更新标签，但只在 `sliderReleased`（鼠标）时或键盘输入立即（通过 `isSliderDown()` 检查）触发保存
- **提示词自动应用**: 系统提示词 TextEdit 600ms 防抖，无需手动应用按钮
- **缓存路径**: 默认 `./models/`（不是 `~/.cache`）。启动时在 `main.py` 中通过 `model_manager.apply_cache_env()` 在 `import torch` 前设置
- **Whisper 组可见性**: 切换 ASR 引擎时显示/隐藏下载组；窗口通过 `sizeHint()` 自动调整大小
- **QDoubleSpinBox 精度**: 所有浮点值保存时 `round()` 到 2 位小数，避免浮点漂移（如 `0.9999999999999992`）

### 启动流程

1. `main.py` 读取 `user_settings.json`，在 `import torch` 前调用 `apply_cache_env()`
2. 首次启动（无 `user_settings.json`）→ `SetupWizardDialog`: 选择下载源 + 路径 + 下载 Silero+SenseVoice
3. 非首次但模型缺失 → `ModelDownloadDialog`: 自动下载缺失模型
4. 模型就绪 → 创建主 UI（悬浮窗、面板、管道）
5. 运行时切换 ASR 引擎: 未缓存 → `ModelDownloadDialog`，然后 `_ModelLoadDialog` 进行 GPU 加载

### 增量 ASR

持续语音增量处理以降低延迟（由 `incremental_asr` 设置控制）:

- **管道循环** 在 VAD 积累语音时每隔 ~1s 检查 `_do_interim_asr()`（缓冲区 > `interim_interval`）
- **句子拆分** 使用 `pysbd` 库（基于规则，23 种语言，~0.08ms/次），辅以逗号回退:
  - pysbd 处理句尾标点（。！？!?.）和语言特定规则（Mr./Dr.、缩写）
  - CJK 顿号 `、` 回退在 25 字符阈值（日语从句分隔符）
  - 西文逗号 `,，;；` 回退在 60 字符阈值（长句未拆分时）
  - 两种回退都要求 `前文 > 15 字符` 且 `后文 > 3 字符` 避免碎片
- **修剪加安全边距**: 提交句子后按比例修剪音频加 0.3s 边距减少回声；保留 ≥0.5s 剩余
- **回声去重**: `_strip_committed_overlap()` 通过匹配已提交尾部与新文本前缀去除重复识别内容
- **短话语缓冲**: ≤8 个字母数字字符的碎片缓冲在 `_interim_pending`，追加到下一句

### VAD 行为

- **渐进静音**: 缓冲越长接受越短的停顿切分（<3s=完整, 3-6s=一半, 6-10s=四分之一 silence_limit）
- **自适应静音**: 跟踪最近停顿时长，设阈值为 P75 × 1.2，在 0.3s~2.0s 间自动调整
- **回溯切分**: 达到最大时长时回溯平滑置信度历史找最低谷切分，剩余部分保留到下一段
- **语音密度过滤**: `_flush_segment()` 丢弃 <25% 块高于置信度阈值的段
- **短段合并**: 低于 `min_speech_duration` 的段不会丢弃 — VAD 做软重置（`_is_speaking=False`）但保留缓冲区，自然与下次语音起始合并
- **修剪段处理**: `_was_trimmed` 标志（由 `trim_front` 设置）确保增量 ASR 剩余部分通过 `force_flush()` 输出，不被 min_speech 检查丢弃

### 关键模式

- `torch` 必须在 PyQt6 之前 import，避免 Windows DLL 冲突（PyTorch 2.9.0+ bug，见 `main.py` 和 [pytorch#166628](https://github.com/pytorch/pytorch/issues/166628)）
- 缓存环境变量在 `main.py` 的模块级别、`import torch` 之前设置，确保 `TORCH_HOME` 被尊重
- 延迟初始化: ASR 模型加载和设置应用在 UI 显示后通过 `QTimer.singleShot(100)` 执行，避免启动卡死
- `translator.py` 的 `make_openai_client()` 是唯一支持代理的 OpenAI 客户端创建函数（翻译和基准测试都使用）
- `main.py` 的 `create_app_icon()` 生成应用图标；通过 `app.setWindowIcon()` 全局设置，所有窗口继承
- 模型缓存检测（`is_asr_cached`, `get_local_model_path`）同时检查 ModelScope 和 HuggingFace 路径，避免切换源时重复下载
- 设置日志输出（`_apply_settings`）过滤掉 `models` 和 `system_prompt`，避免泄露 API 密钥
- FunASR Nano: `asr_funasr_nano.py` 在 `AutoModel()` 前执行 `os.chdir(model_dir)`，使 config.yaml 中的相对路径（如 `Qwen3-0.6B`）在本地解析，不触发 HuggingFace Hub 网络请求
- `Translator` 通过 `make_openai_client()` 默认 10s 超时，防止 API 调用无限挂起
- 日志窗口在启动时创建但隐藏；通过托盘菜单"显示日志"展示
- 音频块时长 32ms（16kHz 下 512 采样点），匹配 Silero VAD 原生窗口，实现最低延迟
- FunASR 所有 `generate()` 调用必须设置 `disable_pbar=True` — tqdm 在 GUI 进程刷新 stderr 时会崩溃
- ASR 引擎生命周期: 每个引擎暴露 `unload()`（移到 CPU + 释放）和 `to_device(device)`（原地迁移）。设备切换对 PyTorch 引擎（SenseVoice/FunASR）用 `to_device()`，对 ctranslate2（Whisper）完全重载。释放顺序: `unload()` → `del` → `gc.collect()` → `torch.cuda.empty_cache()`
- Whisper (ctranslate2) 只接受 `device="cuda"` 不接受 `"cuda:0"`；设备索引通过 `device_index` 参数传递。在 `_switch_asr_engine` 中从组合框文本如 `"cuda:0 (RTX 4090)"` 解析
- ASR 文本密度过滤: ≥2s 段只产生 ≤3 个字母数字字符时作为噪声丢弃
- 设置文件使用原子写入（先写 `.tmp` 再 `os.replace`），防止崩溃时损坏
- `stop()` 先 join 管道线程再刷新 VAD，防止并发 `_process_segment` 调用
- 取消 ASR 下载/加载失败时，如果旧引擎仍可用则恢复 `_asr_ready`
- `Translator._build_system_prompt` 捕获用户提示词模板中的格式错误，回退到 DEFAULT_PROMPT
- 翻译提示词预设: `translator.py` 中的 `PROMPT_PRESETS`（日常/电竞/动画），通过控制面板组合框选择
- `translate_iter()` 是生成器，为流式 UI 输出累积的部分文本；`translate()` 是阻塞等效
- 流式 UI: `update_streaming_signal` → `ChatMessage.update_streaming()` 配合 50ms QTimer 节流；`set_translation()` 完成最终显示
- `RepetitionError` 在模型输出包含重复循环时抛出（模式长度 8+）；在 `_translate_async` 中捕获，显示用户级警告
- 更新日志: `i18n/CHANGELOG_{lang}.md` 文件通过 `_load_latest_changelog()` 渲染为 HTML 显示在设置 → 更新日志标签页

## 语言与风格

- 用中文回复
- 代码注释只在必要时用英文
- 提交消息不带 Co-Authored-By
