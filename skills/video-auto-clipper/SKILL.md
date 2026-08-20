---
name: video-auto-clipper
description: >
  北斗智影智能剪辑（video_auto_clipper）能力调用技能。当用户要求一键剪辑、
  短剧切片、视频筛选、15s 成片、运动/信号分析、降重检测、平台发布规格查询或
  任务管理时使用。通过 HTTP 调用本地或部署的 smartclip 服务完成，无需本地
  安装任何依赖。
---

# video_auto_clipper · 智能剪辑技能

为网站 chatbot 设计的调用技能：所有能力由 smartclip HTTP 服务提供
（FastAPI，默认 `http://127.0.0.1:8010`，可用环境变量 `SMARTCLIP_BASE_URL`
覆盖）。本技能只发 HTTP 请求，不依赖任何第三方库。

## 何时使用

用户说出以下意图时触发：

- 「把这个视频剪成 15 秒」「一键剪辑」「批量切片」→ **一键成片（jobs）**
- 「燃向/伤感/对白类视频怎么剪」「剪辑模板是什么」→ **剪辑模板倾向**
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
python3 skills/video-auto-clipper/smartclip_call.py platforms                 # 平台画像
python3 skills/video-auto-clipper/smartclip_call.py jobs                      # 任务列表
```

服务地址通过环境变量 `SMARTCLIP_BASE_URL` 指定，默认 `http://127.0.0.1:8010`。
