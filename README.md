# beidou-smart-clip

一键式视频筛选与批量 15s 剪辑 · **L0 本地分析层 + L1~L4 流水线骨架**。

为「一键筛选同类型视频 → 批量剪辑 → 15s 成片」流水线提供本地确定性能力：
- **L0 信号层**：运动分析、closed-set 视觉分析、切镜/音频/亮度信号（ffmpeg/ffprobe/numpy，零 API 费用）；
- **L1 规则卡求值**：YAML 规则书加载校验 + 信号匹配（`app/style_profiles.py`）；
- **L2 移交计划**：决定哪些命中送云端 LLM 复核（`app/l2_referral.py`，只规划不调用）；
- **L3 剪辑计划**：带分数窗口 → 15s 片段时间轴（`app/clip_planner.py`，ffmpeg 执行器待二期）；
- **L4 合规门禁**：确定性 block/review/pass 查表（`app/gatekeeper.py`）；
- **平台画像**：各大平台视频类型划分 + 判重规则（调研落地，`configs/platform_profiles.yaml`）；
- **降重分析**：MD5 + 帧感知指纹 + 残留水印/黑边检测，对齐平台多级判重（`app/dedup.py`）。

总体方案见 `docs/../docs/北斗智影新Agent调研与架构方向.md`；
信号层设计见 [`docs/signal-layer-design.md`](docs/signal-layer-design.md)。

## 架构总览

```text
素材视频
   │
   ▼
L0 信号层（本地零成本）
   ├── 切镜率（ffmpeg scene filter）
   ├── 运动强度（批量抽帧 + absdiff + 时序分类）
   ├── 音频能量（volumedetect）
   └── 亮度突变（采样帧均值差分）
   │  输出 SignalValues（signals-1.0）
   ▼
L1 规则卡求值（configs/style_profiles.yaml）──────► L4 合规门禁
   │  命中清单 matches                              （tagging 标签 → block/review/pass）
   ▼
L2 移交计划（always / ambiguous_multi_hit）
   │  referrals（只规划，云端 LLM 二期接入）
   ▼
L3 剪辑计划（贪心挑窗 → 15s 片段时间轴，执行器二期）

平台画像（configs/platform_profiles.yaml）
   ├── 统一类目 → 各平台分类映射（B 站官方 tid）
   └── 降重分析：MD5 / 帧 aHash 指纹 / 残留水印 / 黑边 → pass/review/block
```

典型串联：`/v1/signals/compute` → `/v1/profiles/evaluate` →
`/v1/gatekeeper/check` → `/v1/dedup/analyze` → `/v1/clip/plan`。

## 快速开始

### 1. 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | ≥3.11 | 开发验证环境为 3.12 |
| ffmpeg / ffprobe | 任意近期版本 | 必须在 PATH 中（`brew install ffmpeg`） |
| torch / transformers | 可选 | 仅真实 SigLIP2 视觉分析需要（默认用 Fake provider，无需安装） |

### 2. 安装

```bash
cd beidou-smart-clip
python3 -m pip install -e .            # 基础依赖（fastapi/uvicorn/numpy/pydantic/pyyaml）
python3 -m pip install -e ".[dev]"     # 追加测试依赖（pytest/httpx）
python3 -m pip install -e ".[vision]"  # 可选：真实 SigLIP2（约 2GB 依赖）
```

### 3. 启动服务

```bash
# 最简启动（工作目录须为项目根，以便找到 configs/*.yaml）
python3 -m uvicorn app.main:app --port 8010

# 启用视觉分析（dev 用 Fake provider）
SMARTCLIP_VISUAL_ENABLED=1 python3 -m uvicorn app.main:app --port 8010

# 后台常驻（日志落盘）
SMARTCLIP_VISUAL_ENABLED=1 nohup python3 -m uvicorn app.main:app --port 8010 > /tmp/smartclip.log 2>&1 &
```

- 交互式 API 文档：http://127.0.0.1:8010/docs
- 启动时自动加载两份配置：规则书（`SMARTCLIP_RULE_BOOK`）与平台画像
  （`SMARTCLIP_PLATFORM_PROFILES`）；任一加载失败只降级对应路由为 503，
  不阻塞 L0 分析能力（错误详情见启动日志）。

### 4. 验证

```bash
curl -s localhost:8010/health            # → {"status":"ok"}
curl -s localhost:8010/ready | python3 -m json.tool
# features 中每项为 true 表示对应能力可用：
# motion_analysis / signals 恒为 true；visual_analysis 取决于 SMARTCLIP_VISUAL_ENABLED；
# profiles / gatekeeper 取决于规则书加载；platforms / dedup 取决于平台画像加载。
```

## 运行方法：全路由调用示例

### L0 分析

```bash
# 信号层：一次产出切镜率/运动强度/音频/亮度全套信号
curl -s localhost:8010/v1/signals/compute \
  -H 'Content-Type: application/json' \
  -d '{"video_path": "/path/to/video.mp4"}' | python3 -m json.tool

# 窗口级运动分析（可指定多个窗口与采样档：burst_1s_3fps / window_3s_1fps）
curl -s localhost:8010/v1/analyze/motion \
  -H 'Content-Type: application/json' \
  -d '{"video_path": "/path/to/video.mp4",
       "windows": [{"start_seconds": 12.5, "profile": "window_3s_1fps"}]}'

# closed-set 视觉分析（需 SMARTCLIP_VISUAL_ENABLED=1）
curl -s localhost:8010/v1/analyze/visual \
  -H 'Content-Type: application/json' \
  -d '{"video_path": "/path/to/video.mp4",
       "window_start_seconds": 12.5,
       "question_types": ["shot_size", "mood"]}'
```

### L1~L4 流水线

```bash
# 规则书摘要（标定状态/规则卡清单/门禁维度）
curl -s localhost:8010/v1/profiles

# 规则卡求值 + L2 移交计划（signals 直接粘贴 /v1/signals/compute 的输出）
curl -s localhost:8010/v1/profiles/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"production": false,
       "signals": {"shot_cut_rate_per_min": 20, "scene_count": 4,
                   "mean_motion_intensity": 0.5, "peak_motion_intensity": 0.7,
                   "continuous_motion_window_ratio": 1.0,
                   "luminance_spike_ratio": 0.05, "duration_seconds": 30}}'
# → matches（命中卡 + style_score + hooks）、referrals（L2 移交清单）
# production=true 且规则书未标定时返回 409

# 合规门禁（tagging 14 维标签 → block/review/pass）
curl -s localhost:8010/v1/gatekeeper/check \
  -H 'Content-Type: application/json' \
  -d '{"labels": {"violence": "violence_light"}}'

# 剪辑计划：带分数窗口 → 15s 片段时间轴
curl -s localhost:8010/v1/clip/plan \
  -H 'Content-Type: application/json' \
  -d '{"target_duration_seconds": 15,
       "windows": [{"start_seconds": 0, "duration_seconds": 8, "score": 0.9},
                   {"start_seconds": 20, "duration_seconds": 9, "score": 0.7}]}'
```

### 平台画像与降重

```bash
# 平台画像：统一类目→各平台映射（B 站官方 tid）+ 发布规格 + 判重规则
curl -s localhost:8010/v1/platforms

# 降重体检：MD5 + 帧指纹 + 残留水印/黑边检测
curl -s localhost:8010/v1/dedup/analyze \
  -H 'Content-Type: application/json' \
  -d '{"video_path": "/path/to/video.mp4", "max_frames": 16}'

# 重复率比对：与参考片（原片/已发布成片）比相似度
curl -s localhost:8010/v1/dedup/compare \
  -H 'Content-Type: application/json' \
  -d '{"video_path": "/path/to/output.mp4",
       "reference_paths": ["/path/to/original.mp4"]}'
# → max_similarity ≥0.75 review、≥0.90 block（阈值 provisional）
```

### 错误码约定

| 状态码 | 含义 |
|---|---|
| 400 | 参数语义错误（如视觉分析未指定时间戳、未知 question_type） |
| 404 | 视频/参考片文件不存在 |
| 409 | provisional 规则书请求生产求值（`production=true`） |
| 422 | 媒体处理失败（ffmpeg/ffprobe 报错）或请求体字段校验失败（extra="forbid"） |
| 503 | 能力未启用（视觉开关关闭）或配置未加载（规则书/平台画像） |

## 实现方式

### L0 信号层（`media.py` + `signals.py` + `motion_service.py`）

- **批量抽帧（性能核心）**：放弃逐时间戳 seek，改为单次解码：
  `fps={rate}` 重采样 + `-t {span}` 限长输出 rawvideo 灰度流，采样率自适应
  `rate = min(1.0, max_frames/span)`，时间戳按 `index = round(ts*rate)` 映射
  （误差 ≤ 1/(2*rate) 秒）。帧高按源分辨率计算并对齐 `-2` 偶数约束。
  相比逐帧 seek 全批耗时 531s→111s，且修复了长视频
  `select=eq(n,...)` 表达式过长导致的 ffmpeg filter 初始化失败；
- **切镜率**：`select='gt(scene,{threshold})',showinfo` 全片检测，
  正则解析 pts_time，计数/分钟；
- **运动强度**：全片均匀布 N 个 3s 窗口（默认 4，`window_3s_1fps`），
  相邻帧 absdiff 后按 Framework 同口径阈值分类 changed_pixel，聚合出
  mean/peak 运动强度与连续运动窗比例（时序模式 stable/intermittent/
  continuous/abrupt）；
- **音频能量**：`volumedetect` 取 mean_volume（dB），无音轨返回 null；
- **亮度突变**：全片 1fps 等间隔采样帧（与运动窗合并为一次批量解码），
  相邻帧均值差超阈比例；
- 所有信号统一输出 `SignalValues`（schema `signals-1.0`，Pydantic
  `extra="forbid"` 严格契约）。

### 视觉分析（`visual_service.py`，closed-set）

问题-目录式判定：每个 question_type（如 `shot_size`/`mood`）对应受限候选
目录，SigLIP2 计算图文余弦相似度排序，按 margin 阈值拒答（abstain）防止
开放域幻觉。未安装模型时提供确定性 Fake provider 用于联调；dev 默认
fake 模式，生产置 `SMARTCLIP_VISUAL_FAKE=0`。

### L1 规则书加载器（`style_profiles.py`）

- 配置即代码：`configs/style_profiles.yaml`（schema `style-profiles-1.0`），
  加载时严格校验，任一违反启动即失败（宁可拒绝启动也不静默放行）：
  - 条件信号必须在 `signal_schema.fields` 白名单内（防拼写错误静默失效）；
  - 条件块只支持 `all`(AND)/`any`(OR)，嵌套 ≤2 层；
  - 算子限 `gte/gt/lte/lt/between`；重复 profile id 报错；
  - `calibration.status` 三态；声明 `calibrated` 必须样本数 ≥30；
- 求值语义：信号缺失（None）视为条件不满足并记 notes，绝不默认放行；
  `enabled: false` 的卡跳过并记录；
- 软评分：provisional 阶段按超阈幅度（相对阈值 margin）加权归一到 0~1，
  正式标定后切换为样本分布 min-max 归一；
- 生产门禁：`production=true` 且状态非 `calibrated` 直接抛错（路由返回 409）。

### L2 移交计划（`l2_referral.py`）

只规划不调用云端：按规则卡 `l2_fallback.trigger` 决定移交——
`always`（一期信号不足的卡，如快节奏对白、质量预筛）命中即移交；
`ambiguous_multi_hit` 仅在 ≥2 张卡同时命中时移交。移交受 `max_windows`
限制控制云端成本；`prompts/` 下为三个移交 prompt stub（燃向/对白/质量复核）。

### L3 剪辑计划（`clip_planner.py`）

计划与执行分离（计划可审计、可回放）：输入带分数的候选窗口（如窗口平均
运动强度），**分数降序贪心挑窗 → 按时间排序合并相邻/重叠窗（分数取均值）
→ 目标时长预算裁剪尾部**。输出 15s 成片的片段时间轴；真正的 ffmpeg 拼接、
转场、音频融洽为二期执行器（响应附 `executor_status` 明示能力状态）。

### L4 合规门禁（`gatekeeper.py`）

确定性查表，不做模糊语义判断：输入维度→标签（来自 tagging 14 维合规输出
或 LLM 打标），按规则书 `gatekeeper` 配置查 block/review 集合，
block > review > pass；未知维度/缺失标签忽略并记 notes，绝不因配置缺失误拦。

### 平台画像与降重（`platform_profiles.py` + `dedup.py`）

- **分类体系**：调研结论落地（仅 B 站有公开官方分区表；抖音/快手/小红书
  降级采用创作者标签归纳；Meta/X 仅原创性政策；YouTube 为降级参考）。
  5 个统一类目 → 7 平台映射（B 站带官方 tid）+ 各平台发布规格；
- **降重检测**对齐平台多级判重（MD5 → 关键帧/九宫格抽帧 → AI 同质化）的
  前两级本地等价能力：
  - 文件 MD5（流式计算，平台第一级查重的本地等价物）；
  - 帧感知指纹 aHash（8×8 块均值 + 全局均值阈值 → 64bit），组间相似度
    取「每帧在对方集合中的最佳匹配」均值，顺序无关、对亮度漂移鲁棒；
  - 静态覆盖物分析：全片均匀抽帧后算逐像素时序方差，角落（25%×25%）
    静态占比高 → 疑似残留水印/角标；边缘整行暗且静态 → 黑边（搬运未重制）；
  - 判定层级：相似度 ≥0.90 block、≥0.75 review，水印/黑边命中降级 review，
    并输出针对性降重建议（重编码/裁切/变速/镜像/换 BGM，小红书注意 90 天窗）。
- 加载器同样严格校验：schema 版本、类目 id 唯一、四个必备阈值齐全且
  review < block，违反即启动降级。

### 分镜捕捉与叙事剪辑（`storyboard.py` + `narrative.py`）

剪辑前先捕捉更多画面，再按「风格→画面目标」匹配与混剪：

- **分镜捕捉**：ffmpeg scene 全片切点 → 镜头切分（过滤 ≤0.05s 碎片）→
  每镜均匀多帧采样（单次批量解码、帧预算自适应）→ 镜头级运动/亮度信号；
- **目标匹配**：`configs/narrative_targets.yaml` 定义风格→画面目标
  （日常→波澜起伏对比对、战斗→夸张特效/血液冲击、恋爱→亲密动作、
  群像→人物-行动），可按 `style_id` 只评估归属该风格的目标；
- **叙事模板（混剪）**：linear 顺叙 / peak_first 高潮置顶（事件→结果）/
  contrast_open 对比开场（事件→转折），均带目标时长预算裁剪；
  `interleave` 做群像人物-行动交错（本地以时间段近似，人物/行动语义
  标注交 L2 `prompts/narrative_board.md` 确认）；
- 混剪片段保持各自在原片内的完整、同一镜头不重复使用；目标配置加载
  失败仅叙事路由降级 503，不影响其余能力。

### 成品自动命名（`titler.py`）

`POST /v1/jobs` 不传 `output_name` 时，成片文件名自动生成为可直接发布
的剪辑标题（不再是 job_id 乱码）：

- **模板库**按风格分池（燃向/战斗特效/日常反转/恋爱甜向/快节奏对白/
  群像人物-行动/通用兜底），套路参考公开爆款标题规律（黄金 3 秒钩子、
  悬念问句、「【标签】描述」分区前缀、对话引用开场）；
- **确定性生成**：池路由（风格 hint → 命中目标 → 人物-行动线索 → 兜底）
  + 长度甜点区/钩子符号/人物名加权打分，同输入永远得到同一标题；
- **人物-行动槽位**：L2 `narrative_board` 标注或人工提供
  `character_actions` 时注入（如「宙斯出手的瞬间，全场安静了」）；
- 文件名做平台安全净化（去路径分隔符/Windows 保留字）+ 重名自动加序号；
  任务详情回显 `title` 与备选 `result.title_candidates`；
  `POST /v1/titles/preview` 可在剪辑前独立预览推荐标题。

### 启动降级策略

`main.py` 启动时依次加载规则书、平台画像与叙事目标书：任一失败仅记 error
日志并将对应 `app.state` 置 None，相关路由返回 503（提示检查对应环境
变量），L0 分析、L3 剪辑计划、分镜捕捉等纯计算能力不受影响。

## HTTP 路由

| 路由 | 说明 |
|---|---|
| `GET /health` / `GET /ready` | 存活/就绪（含 feature flags） |
| `POST /v1/analyze/motion` | 窗口级运动分析：采样（burst_1s_3fps / window_3s_1fps）+ absdiff + 时序分类（stable/intermittent/continuous/abrupt），阈值口径对齐 Framework |
| `POST /v1/analyze/visual` | closed-set 视觉分析：受限目录 + 余弦排序 + margin 拒答；内置 `shot_size`/`visual_effect`/`mood` 等风格信号组 |
| `POST /v1/signals/compute` | L0 信号层：切镜率、运动强度、音频能量、亮度突变比例 + 源信息 |
| `GET /v1/profiles` | 规则书摘要：标定状态 + 规则卡清单（含 clip_strategy 剪辑模板倾向）+ 门禁维度 |
| `POST /v1/profiles/evaluate` | L1 规则卡求值 + L2 移交计划：信号值 → 命中清单与云端复核清单（`production=true` 需规则书已标定，否则 409） |
| `POST /v1/gatekeeper/check` | L4 合规门禁：维度标签 → block/review/pass 确定性查表 |
| `POST /v1/clip/plan` | L3 剪辑计划：带分数窗口 → 15s 片段计划（执行器未实现，响应附 `executor_status`） |
| `GET /v1/platforms` | 平台画像：统一类目→各平台映射（含 B 站官方 tid）+ 发布规格 + 判重规则 |
| `POST /v1/dedup/analyze` | 降重体检：MD5 + 帧 aHash 指纹 + 残留水印/黑边检测 → pass/review/block |
| `POST /v1/dedup/compare` | 重复率比对：与参考片（原片/已发布成片）帧指纹相似度 → 判定（≥0.75 review、≥0.90 block） |
| `POST /v1/jobs` | 一键成片任务（202 + job_id，异步跑 L0→L3 全流程） |
| `GET /v1/jobs` / `GET /v1/jobs/{job_id}` | 任务列表 / 详情（status/error/片段时间轴） |
| `GET /v1/jobs/{job_id}/product` | 成片下载（FileResponse） |
| `POST /v1/jobs/{job_id}/cancel` / `retry` | 取消 / 重试任务 |
| `POST /v1/clip/render` | L3 渲染执行：片段时间轴 → ffmpeg 拼接成片 |
| `POST /v1/l2/review` | L2 云端多模态复核（OpenAI-compatible，未启用返回 503） |
| `GET /v1/narrative` | 叙事目标/模板清单摘要（风格→画面对应关系的权威来源） |
| `POST /v1/storyboard/extract` | 分镜捕捉：全片切点分镜 + 逐镜运动/亮度信号 |
| `POST /v1/narrative/plan` | 叙事剪辑计划：目标匹配 + 模板重排（混剪/人物-行动交错） |
| `POST /v1/titles/preview` | 成片标题预览：分镜→目标命中→爆款标题模板，产出可发布的剪辑名 |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SMARTCLIP_MOTION_WIDTH` | 320 | 运动分析抽帧降宽 |
| `SMARTCLIP_SCENE_THRESHOLD` | 0.3 | 场景切点检测阈值 |
| `SMARTCLIP_MOTION_WINDOWS` | 4 | 信号层运动窗口数 |
| `SMARTCLIP_VISUAL_ENABLED` | 0 | 视觉分析开关（未开启时路由返回 503） |
| `SMARTCLIP_VISUAL_FAKE` | 1 | 1=确定性 Fake provider（联调）；0=真实 SigLIP2（需 `pip install .[vision]`） |
| `SMARTCLIP_RULE_BOOK` | `configs/style_profiles.yaml` | 规则书路径；加载失败时 L1/L2/L4 路由降级为 503，不阻塞 L0 |
| `SMARTCLIP_PLATFORM_PROFILES` | `configs/platform_profiles.yaml` | 平台画像路径；加载失败时分类/降重路由降级为 503 |
| `SMARTCLIP_NARRATIVE_TARGETS` | `configs/narrative_targets.yaml` | 叙事剪辑目标与模板；加载失败时叙事路由降级为 503 |
| `SMARTCLIP_L2_ENABLED` | 0 | L2 云端复核开关（另见 `SMARTCLIP_L2_API_KEY` / `_BASE_URL` / `_MODEL` / `_TIMEOUT_SECONDS`） |
| `SMARTCLIP_PRODUCTS_DIR` | `products` | 任务服务成片产物目录 |
| `SMARTCLIP_JOB_WORKERS` | 1 | 任务执行线程数 |

## 测试

```bash
python3 -m pytest          # 115 个用例：纯逻辑 + TestClient 端到端（需 ffmpeg）
```

- 端到端 fixture：`tests/conftest.py` 用 ffmpeg 生成确定性 4s 小视频
  （前 2s testsrc2 高运动 + 后 2s smptebars 静止，中间一次硬切 + 正弦音轨），
  保证信号层/运动/降重测试可复现；
- 纯逻辑覆盖：规则书校验（白名单/深度/样本门禁/重复 id）、求值语义、
  门禁查表、移交触发、剪辑贪心、aHash 鲁棒性、水印/黑边合成帧检测、
  加载器校验与启动降级（503）。

## 平台视频类型划分与降重（2026-08 调研落地）

调研各大平台的视频类型划分标准与重复率判定规则（详见
[`docs/platform-taxonomy-and-dedup.md`](docs/platform-taxonomy-and-dedup.md)）：

- **分类**：仅 B 站有公开官方分区表（tid 体系，已内置参考表）；抖音/快手/
  小红书无公开标准，已按要求降级采用其创作者中心/热门标签体系归纳；
  Meta/X 无分区仅有原创性政策，另补充 YouTube 作为降级参考平台；
- **判重**：各平台普遍为多级漏斗（MD5 → 关键帧/九宫格抽帧 → AI 同质化）；
  小红书另有 90 天时间窗与信誉分（<8 限流 100%）；B 站自制/转载二分 +
  撞车保留高画质；Instagram unoriginal 内容移出推荐流；X 按 spam 限触达；
- **降重能力**：本地覆盖平台前两级判重的等价检测（MD5/帧指纹/水印残留/
  黑边），命中时输出针对性降重建议；阈值 `status: provisional`，
  待样本标定（除 B 站外各平台均无公开数值阈值）。

## 真实素材实测与调节（2026-08）

用 6 支真实短剧素材（`untitled folder`，58~141s，1080p）对 `/v1/signals/compute`
跑通标定：

| 指标 | 调节前（v0） | 调节后（v1） |
|---|---|---|
| 全批耗时 | 531s | **111s** |
| 最长单片（99s 片） | 244.5s | **50.5s** |
| 成功率 | 6/6 | 6/6 |

调节内容：

- 逐帧 seek 抽帧 → **单次解码批量抽帧**（`fps` 重采样 + `-t` 限长，
  帧数预算自适应），修复了长视频 `select=eq(n,...)` 表达式过长导致的
  ffmpeg filter 初始化失败；
- 信号实测分布：短剧素材整体快节奏（cut_rate 17.5~27.9/min、
  mean_motion 0.28~0.50），原示例阈值已按实测上调；
- 剩余耗时瓶颈为场景切点检测的全片解码（二期可降宽解码优化）。

信号矩阵存档：`docs/calibration/signal_matrix_v0.json`（调节前）/
`signal_matrix_v1.json`（调节后）/ `signal_matrix_v2.json`（11 支带标注样本）；
完整记录见 [`docs/signal-layer-design.md`](docs/signal-layer-design.md) §5.1/§5.2。

第二轮（v2）基于 `AIGC/tagging/短剧案例.xlsx` 的人工质量标注（高质量/差质量
两个 sheet）：文件 ID→剧名→标注映射见 `docs/calibration/sample_set.json`，
新下载 5 支 OSS 直链素材后发现 `continuous_motion_window_ratio` 是当前唯一
有区分度的质量信号（已写入规则卡 `quality_flag`，provisional）。

## 风格画像规则库（YAML）与 L1~L4 流水线

- 设计思路与样本集准备必要性：[`docs/style-profiles-yaml-design.md`](docs/style-profiles-yaml-design.md)；
- 示例规则卡：[`configs/style_profiles.yaml`](configs/style_profiles.yaml)
  （燃向/快节奏对白/伤感/质量异常预筛四卡，v0.2，当前 `status: provisional`，
  待 35~50 支标注样本正式标定后才能生产使用）；
- 加载器硬规则：信号白名单、条件嵌套 ≤2 层、缺失信号视为不满足、
  `calibrated` 需样本数 ≥30，任一违反启动即失败；
- L2 移交 prompt stub 见 `prompts/`（燃向/对白/质量复核，二期接入云端 LLM）。

## Chatbot Skill（供网站附带的 agent 调用）

`skills/video-auto-clipper/` 内置一个为网站 chatbot 设计的调用技能，
将全部能力整合为单一技能：

- `SKILL.md`：标准 Agent Skill 格式（name/description frontmatter），
  含触发意图识别、前置探活、三大能力（一键成片 jobs / 单步分析 / 任务管理）、
  结果解读口径与错误处理表；
- `smartclip_call.py`：零依赖（仅 Python 标准库）调用脚本，支持
  `run / signals / dedup / templates / storyboard / narrative / platforms / jobs / status`
  十个子命令；`templates` 输出各视频类型的剪辑模板倾向（目标时长/prefer
  倾向/钩子偏好），`storyboard` 输出镜头级分镜信号，`narrative` 输出
  风格→画面匹配与混剪时间轴（支持 `--interleave` 人物-行动交错），
  `title` 预览可发布的成片标题（`run` 不传 output_name 时成品自动同名）；
- 服务地址由环境变量 `SMARTCLIP_BASE_URL` 指定（默认 `http://127.0.0.1:8010`），
  对本地与部署环境通用；已按 SKILL.md 全流程实测跑通（一键成片 → 成片下载 200）。

## 目录

```text
app/
├── config.py          # Settings（环境变量注入，含 SMARTCLIP_RULE_BOOK）
├── models.py          # Pydantic 契约（extra="forbid"，L0~L4 全层）
├── media.py           # ffmpeg/ffprobe 工具（抽帧/批量解码/切点/音量）
├── motion_service.py  # 运动分析（absdiff + 时序分类）
├── visual_service.py  # closed-set 视觉分析（SigLIP2/Fake provider）
├── signals.py         # L0 信号层（批量解码）
├── style_profiles.py  # L1 规则书加载器（校验 + 求值）
├── l2_referral.py     # L2 云端移交计划（只规划不调用）
├── clip_planner.py    # L3 剪辑计划（贪心挑窗/合并/预算裁剪）
├── gatekeeper.py      # L4 合规门禁（确定性查表）
├── platform_profiles.py # 平台画像加载器（分类体系 + 判重规则）
├── dedup.py           # 降重分析（MD5/帧指纹/水印/黑边）
├── job_service.py     # 文件持久化异步任务（L0→L3 全流程 + cancel/retry）
├── clip_executor.py   # L3 渲染执行器（ffmpeg 拼接成片）
├── l2_service.py      # L2 云端 LLM 复核（OpenAI-compatible，可选）
├── storyboard.py      # 分镜捕捉（切点分镜 + 逐镜采样信号）
├── titler.py          # 成品自动命名（风格标题模板库 + 文件名净化）
├── narrative.py       # 叙事目标匹配 + 模板重排（混剪/交错）
├── cli.py             # smartclip CLI（serve/run/status/watch/jobs）
├── static/index.html  # Web 控制台页面
├── routes.py          # FastAPI 路由
└── main.py            # 应用入口（启动时加载配置，失败降级）
configs/style_profiles.yaml        # 风格规则卡示例（provisional）
configs/platform_profiles.yaml     # 平台分类 + 判重规则（调研落地）
configs/narrative_targets.yaml     # 叙事目标与模板（风格→画面 + 混剪，provisional）
skills/video-auto-clipper/         # chatbot 调用技能（SKILL.md + 零依赖脚本）
prompts/                           # L2 移交 prompt stub（含 narrative_board 人物-行动标注）
docs/signal-layer-design.md        # 信号层设计（含实测标定记录）
docs/style-profiles-yaml-design.md # YAML 设计 + 样本集必要性
docs/platform-taxonomy-and-dedup.md # 平台分类与判重调研
docs/calibration/                  # 信号矩阵存档（v0/v1/v2）+ 样本集
tests/                             # pytest（含 ffmpeg fixture 端到端）
```

## 与历史项目的边界

- 不修改 `~/work/AI/video/*` 与 `~/alibaba/AIGC/*`；本项目将其运动分析/
  场景检测/closed-set 视觉证据语义以同阈值复刻为独立 HTTP 服务；
- 后续若 Framework 上线运动分析 HTTP 路由，本服务可退化为代理。
