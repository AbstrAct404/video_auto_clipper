---
name: video-auto-clipper
description: >
  北斗智影智能剪辑（video_auto_clipper）能力调用技能。当用户要求一键剪辑、
  短剧切片、视频筛选、15s 成片、运动/信号分析、降重检测、平台发布规格查询、
  分镜捕捉/叙事混剪计划或任务管理时使用。通过 HTTP 调用本地或部署的
  smartclip 服务完成，无需本地安装任何依赖。
---

# video_auto_clipper · 智能剪辑技能

为网站 chatbot 设计的调用技能：所有能力由 smartclip HTTP 服务提供
（FastAPI，默认 `http://127.0.0.1:8010`，可用环境变量 `SMARTCLIP_BASE_URL`
覆盖）。本技能只发 HTTP 请求，不依赖任何第三方库。

## 何时使用

用户说出以下意图时触发：

- 「把这个视频剪成 15 秒」「一键剪辑」「批量切片」→ **一键成片（jobs）**
- 「燃向/伤感/对白类视频怎么剪」「剪辑模板是什么」→ **剪辑模板倾向**
- 「先看看有哪些镜头/分镜」「捕捉更多画面」→ **分镜捕捉**
- 「日常→波澜起伏、战斗→特效这类对应画面」「人物行动交替混剪」「打乱顺序先放高潮」→ **叙事剪辑计划**
- 「给成片起个标题」「取个能直接发布的名字」→ **成品自动命名 / 标题预览**
- 「分析这个视频的节奏/运动/信号」→ **L0 信号分析**
- 「会不会被判搬运/重复」「发布前查重」→ **降重检测**
- 「B 站/抖音/小红书发什么分区」「竖屏还是横屏、最长多少秒」→ **平台画像**
- 「合规检查」「能不能发」→ **L4 门禁**
- 「任务跑到哪了/取消任务/重试」→ **任务管理**

## 调用前置检查（必做）

先探活，失败则告知用户服务未启动并给出启动命令：

```bash
curl -s --max-time 3 "${SMARTCLIP_BASE_URL:-http://127.0.0.1:8010}/ready"
```

- `200` 且 `features` 中所需能力为 `true` → 继续；
- 连不上 → 回复：「智能剪辑服务未启动，请在项目根目录执行
  `python3 -m uvicorn app.main:app --port 8010` 后重试」。

## 能力一：一键成片（推荐主流程）

一次调用自动跑 L0 信号 → L1 规则 → （可选 L2）→ L3 计划 → ffmpeg 渲染，
成片写入服务侧 `products/` 目录。

```bash
BASE="${SMARTCLIP_BASE_URL:-http://127.0.0.1:8010}"

# 1) 提交任务（返回 202 + job_id）
curl -s -X POST "$BASE/v1/jobs" -H 'Content-Type: application/json' \
  -d '{"video_path": "<用户提供的视频绝对路径>",
       "target_duration_seconds": 15,
       "motion_window_count": 8,
       "request_l2": false}'

# 2) 轮询直至 status ∈ {completed, failed, cancelled}（建议间隔 3s，最长 5 分钟）
curl -s "$BASE/v1/jobs/<job_id>"

# 3) 完成后把成片链接交给用户（浏览器可直接预览/下载）
echo "$BASE/v1/jobs/<job_id>/product"
```

要点：

- `video_path` 必须是**服务所在机器**上可访问的绝对路径；用户在网页上传的
  文件需先落到服务器目录再传路径；
- `target_duration_seconds` 范围 (0, 60]，默认 15；
- `request_l2: true` 需服务端配置了 LLM（`SMARTCLIP_L2_ENABLED=1` +
  API Key），未配置时保持 `false`，流水线照常完成；
- 失败时响应 `error` 字段给出原因；可 `POST /v1/jobs/<job_id>/retry` 重试、
  `POST /v1/jobs/<job_id>/cancel` 取消、`GET /v1/jobs` 列最近任务。

## 剪辑模板倾向（按视频类型）

每种视频风格对应一张规则卡的 `clip_strategy`，是权威来源：

```bash
# 动态获取（推荐：规则书升级后自动跟随，不会过时）
curl -s "$BASE/v1/profiles"   # 每个 profile 含 clip_strategy 与 hooks
# 或零依赖脚本（含 prefer 语义中文对照）
python3 skills/video-auto-clipper/smartclip_call.py templates
```

当前规则书（v0.2，provisional）静态兜底表：

| 风格 | 目标时长 | 剪辑倾向 prefer | 钩子偏好 | 状态 |
|---|---|---|---|---|
| 燃向 `ran_xiang` | 15s（单片最短 1.0s） | `high_motion_segments`：优先高运动片段（打斗/追逐/快速推拉），保留能量顶点 | C 视觉张力、D 情绪爆发 | 启用 |
| 快节奏对白 `fast_dialogue` | 15s | `hook_first_3s`：前 3 秒必须上钩子（悬念/冲突直给） | E 悬念前置 | 启用（L2 强制复核） |
| 伤感 `shang_gan` | 15s | `close_up_slow_pace`：特写镜头 + 慢节奏情绪段落，长镜头不切碎 | A 情感冲突、D 情绪爆发 | **停用**（缺慢节奏标定样本） |
| 质量异常 `quality_flag` | — | `flagged_for_review`：仅标记转人工复核，不自动剪 | — | 启用 |

chatbot 使用约定：

1. 用户指明风格时，提交任务带上 `"profile_ids": ["<风格 id>"]`（如 `["ran_xiang"]`）；
2. 回复剪辑方向时引用该风格的 prefer 语义，并提醒规则书处于 `provisional`
   标定阶段，结果仅供试运行参考；
3. 用户要求伤感类剪辑时，如实告知该模板未标定停用，可先按通用模板剪；
4. 当前 L3 选窗按分数贪心挑选，`prefer` 是策略提示与解释口径，二期才参与选窗重排。

## 能力一·扩展：分镜捕捉 + 叙事剪辑计划（混剪）

剪辑前先按镜头捕捉全片画面，再按「风格→画面目标」匹配镜头，最后按叙事
模板重排（允许混剪打乱时间顺序：事件→结果 / 对比开场）：

```bash
# 1) 分镜捕捉：切点分镜 + 逐镜运动/亮度信号
curl -s -X POST "$BASE/v1/storyboard/extract" -H 'Content-Type: application/json' \
  -d '{"video_path": "<绝对路径>", "frames_per_shot": 3}'

# 2) 叙事计划：风格→画面目标匹配 + 模板重排
#    模板：linear 顺叙 / hook_first 高潮置顶 / climax_first / twist_bridge 对比开场
#    interleave=true：群像人物-行动交错（宙斯→释放闪电，jimmy→教扫码机器）
curl -s -X POST "$BASE/v1/narrative/plan" -H 'Content-Type: application/json' \
  -d '{"video_path": "<绝对路径>", "style_id": "ran_xiang",
       "template_id": "hook_first", "target_duration_seconds": 15,
       "interleave": false}'

# 画面目标/模板清单（风格→画面对应关系的权威来源）
curl -s "$BASE/v1/narrative"
```

当前画面目标（provisional 标定，ZEUS 群像素材实测）：

| 风格 | 画面目标 | 本地信号口径 |
|---|---|---|
| 日常 | 波澜起伏（平静→爆发相邻镜头对） | 前镜低运动 → 后镜高运动 |
| 恋爱 | 亲密动作 | 低运动长镜（语义交 L2 确认） |
| 战斗 | 夸张特效/血液冲击 | 高运动 + 亮度剧变（雷电/爆炸） |
| 群像 | 人物-行动交错 | 高运动长镜 + interleave |

chatbot 使用约定：

1. 建议先 `signals` + `profiles/evaluate` 判断整体风格，再传 `style_id`；
2. `segments` 是混剪后的成片时间轴，每段保持原片内完整、不重复使用镜头；
3. `notes` 会标注降级/近似情况（如 contrast_open 无对比对回退 peak_first）；
4. 人物/行动等语义由本地时间段近似，正式交错需 L2（`prompts/narrative_board.md`）确认，回复用户时需说明。

## 成品自动命名（可直接发布的剪辑标题）

`POST /v1/jobs` 不传 `output_name` 时，成片文件名自动生成为有吸引力的
发布标题（如「这爆发力直接封神！.mp4」），依据风格命中 + L0 信号从爆款
标题模板库（黄金 3 秒钩子/悬念问句/【标签】前缀/人物-行动句式）确定性
选取；`output_name` 显式给出时仍以用户为准。任务详情含 `title` 与
`result.title_candidates` 备选。发布前可先预览：

```bash
# 标题预览：分镜→画面目标命中→风格标题（style_id 可选；人物-行动线索可选）
curl -s -X POST "$BASE/v1/titles/preview" -H 'Content-Type: application/json' \
  -d '{"video_path": "<绝对路径>", "style_id": "ran_xiang",
       "character_actions": [{"subject": "宙斯", "action": "释放闪电"}]}'
# → recommended（推荐）/ candidates（备选）/ filename（建议文件名）
```

约定：

1. 标题为本地确定性生成（同输入同输出），无需联网；`character_actions`
   来自 L2 `narrative_board` 标注或用户提供，可显著提升个性化（含人物名）；
2. 文件名已做平台安全净化（去路径分隔符/保留字），可直接用于发布；
3. 回复用户时给出推荐标题 + 2 条备选供挑选，不要只给一条。

## 能力二：单步分析（轻量问答场景）

```bash
# L0 信号：切镜率/运动强度/音频能量/亮度突变（1080p 一分钟视频约 5~10s）
curl -s -X POST "$BASE/v1/signals/compute" -H 'Content-Type: application/json' \
  -d '{"video_path": "<绝对路径>"}'

# 规则卡命中（signals 直接粘贴上一步的 signals 字段；production 恒传 false）
curl -s -X POST "$BASE/v1/profiles/evaluate" -H 'Content-Type: application/json' \
  -d '{"production": false, "signals": <上一步的 signals>}'

# 降重体检：MD5 + 帧指纹 + 残留水印/黑边 → verdict ∈ {pass, review, block}
curl -s -X POST "$BASE/v1/dedup/analyze" -H 'Content-Type: application/json' \
  -d '{"video_path": "<绝对路径>", "max_frames": 16}'

# 与原片/已发布成片比相似度（≥0.75 review、≥0.90 block）
curl -s -X POST "$BASE/v1/dedup/compare" -H 'Content-Type: application/json' \
  -d '{"video_path": "<成片路径>", "reference_paths": ["<参考片路径>"]}'

# 合规门禁：tagging 标签 → block/review/pass
curl -s -X POST "$BASE/v1/gatekeeper/check" -H 'Content-Type: application/json' \
  -d '{"labels": {"violence": "violence_light"}}'

# 平台画像：统一类目→各平台映射（含 B 站官方 tid）+ 发布规格 + 判重规则
curl -s "$BASE/v1/platforms"
```

## 能力三：任务管理

| 操作 | 请求 |
|---|---|
| 列出任务 | `GET /v1/jobs` |
| 查看详情 | `GET /v1/jobs/{job_id}` |
| 取消 | `POST /v1/jobs/{job_id}/cancel` |
| 重试 | `POST /v1/jobs/{job_id}/retry` |
| 下载成片 | `GET /v1/jobs/{job_id}/product` |

## 结果解读口径（回复用户时使用）

- `verdict=pass`：可放心发布；`review`：附 notes 中的降重建议提示用户修改；
  `block`：高度疑似重复，建议重剪；
- 命中规则卡 `quality_flag`：提示画面存在连续大幅运动，可能被平台判低质；
- 平台规格提醒：小红书 90 天查重窗 + 信誉分；Instagram 9:16 ≤90s；
  B 站撞车保留高画质版本。

## 错误处理

| HTTP 状态 | 含义 | 应对 |
|---|---|---|
| 404 | 视频路径不存在 | 请用户核对路径 |
| 409 | provisional 规则书被要求生产求值 | 改传 `production: false` |
| 422 | 参数校验失败 / ffmpeg 处理失败 | 读取响应 `detail` 转述用户 |
| 503 | 对应能力未启用或配置未加载 | 查看 `/ready` 定位缺失 feature |

## 备用调用方式（无 curl 环境）

仓库自带零依赖 Python 脚本（仅需标准库）：

```bash
python3 skills/video-auto-clipper/smartclip_call.py run <视频绝对路径>        # 一键成片并等待
python3 skills/video-auto-clipper/smartclip_call.py signals <视频绝对路径>    # L0 信号
python3 skills/video-auto-clipper/smartclip_call.py dedup <视频绝对路径>      # 降重体检
python3 skills/video-auto-clipper/smartclip_call.py templates                 # 剪辑模板倾向
python3 skills/video-auto-clipper/smartclip_call.py storyboard <视频绝对路径>  # 分镜捕捉
python3 skills/video-auto-clipper/smartclip_call.py narrative <视频绝对路径> \
  --style ran_xiang --template hook_first --interleave                         # 叙事混剪计划
python3 skills/video-auto-clipper/smartclip_call.py title <视频绝对路径> \
  --style ran_xiang --character 宙斯:释放闪电                                  # 成片标题预览
python3 skills/video-auto-clipper/smartclip_call.py platforms                 # 平台画像
python3 skills/video-auto-clipper/smartclip_call.py jobs                      # 任务列表
```

服务地址通过环境变量 `SMARTCLIP_BASE_URL` 指定，默认 `http://127.0.0.1:8010`。
