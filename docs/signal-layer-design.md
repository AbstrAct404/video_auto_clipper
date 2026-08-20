# L0 信号层设计（本地零成本确定性信号）

> 版本：v1（2026-08-19）· 实现：`app/signals.py` + `app/motion_service.py` + `app/media.py`
> 定位：四层流水线的第一层。在**不调用任何付费 API** 的前提下，为每个视频产出一组
> 可量化、可复现、可标定的信号，供「风格画像规则库」做类型粗筛与粗排。

## 1. 设计原则

1. **零 API 成本**：只用 ffmpeg/ffprobe/numpy，本地 CPU 可跑；
2. **确定性可复现**：同一视频同一参数永远得到同一结果（无随机、无模型漂移），可入回归测试；
3. **只测量不解释**：信号层只输出数值测量（运动幅度、切镜率…），"这算燃向"的判断属于风格画像规则库，不在本层；
4. **漏斗入口**：信号层是成本闸门，只有被它粗排进 top 的视频/窗口才进入 L2 LLM 精细理解；
5. **可标定**：所有阈值都是配置项，留好校准流程接口（见 §5）。

## 2. 一期信号清单（已实现）

| 信号 | 字段 | 计算方法 | 对应能力 |
|---|---|---|---|
| 切镜率 | `shot_cut_rate_per_min`、`scene_count` | FFmpeg `select='gt(scene,θ)',showinfo` 切点计数 / 分钟；θ 默认 0.3，与 Framework 场景检测同滤镜 | 能力 2：快节奏量化（燃向高、伤感低） |
| 运动强度 | `mean_motion_intensity`、`peak_motion_intensity`、`continuous_motion_window_ratio` | 全片均匀布 N 个 `window_3s_1fps` 窗口，逐窗 absdiff（320px 灰度降采样），聚合 `mean_changed_pixel_ratio`；阈值/分类口径对齐 Framework TemporalChangeAnalyzer（active 0.05 / abrupt 0.65+0.35） | 能力 2：燃向"快速移动"的核心信号 |
| 音频能量 | `audio_mean_volume_db` | FFmpeg `volumedetect` 全片平均音量（dB）；无音轨为 null | 能力 2：伤感类通常配乐轻/对白多；也可用于静音片过滤 |
| 亮度突变比例 | `luminance_spike_ratio` | 全片 1fps（上限 120 帧）抽帧，相邻帧平均亮度差 ≥0.12 的比例 | 能力 2：特效/爆炸/闪白的**粗**信号（高亮突变常伴随特效与转场） |
| 源信息 | `duration_seconds`、`width`、`height`、`fps` | ffprobe | 能力 1：格式预筛（平台规格 ≥540×960 等） |

**输出契约**：`SignalsResponse`（schema_version=`signals-1.0`，`extra="forbid"`），见 `app/models.py`。

**HTTP 入口**：`POST /v1/signals/compute {video_path, scene_threshold?, motion_window_count?}`。

### 2.1 运动分析 HTTP 路由（独立于信号层的细粒度入口）

`POST /v1/analyze/motion`：调用方指定任意窗口列表（`start_seconds` + profile
`burst_1s_3fps`/`window_3s_1fps` + 可选 ROI），返回逐窗 `TemporalSummary`
（stable/intermittent/continuous/abrupt）与逐帧对 `PixelDifference` 证据。
用途：片段级分析（能力 3）时对候选 15s 窗口做细粒度运动画像，以及剪辑前对
切片边界做流畅度检查。

### 2.2 视觉分析 HTTP 路由（closed-set）

`POST /v1/analyze/visual`：SigLIP2 图文相似度 closed-set 分类，受限目录 +
score/margin 双阈值**拒答**。内置目录在 Framework 五组之上为新任务扩展三组：
`shot_size`（特写/中景/远景——伤感类特写信号）、`visual_effect`（爆炸/光效/速度线）、
`mood`（伤感/紧张/温馨）。目录允许按请求受限扩展（≤8 组 × ≤12 值）。
部署形态：默认 fake provider（开发联调）；生产需 `pip install .[vision]` +
`SMARTCLIP_VISUAL_ENABLED=1` + `SMARTCLIP_VISUAL_FAKE=0`。

## 3. 风格画像规则库消费方式（设计约定）

规则卡 = 一组信号阈值条件 + 钩子类型偏好 + L2 策略 prompt。下表为**基于 6 支
真实素材首轮实测调节后的初值（v0.1，样本仅 6 支且同属快节奏短剧，必须经
§5 完整标定后才能用于生产）**：

| 风格 | 一期信号判据（v0.1） | 钩子偏好 |
|---|---|---|
| 燃向 | `mean_motion_intensity ≥ 0.42` 且 `peak_motion_intensity ≥ 0.60` 且 `shot_cut_rate_per_min ≥ 18` | C 视觉张力 / D 情绪爆发 |
| 快节奏对白 | `shot_cut_rate_per_min ≥ 25` 且 `mean_motion_intensity < 0.40`（切点密但运动低，对话向） | E 悬念前置 |
| 伤感 | 本批样本无覆盖，阈值待负样本标定；方向：`mean_motion_intensity ≤ 0.20` 且 `shot_cut_rate_per_min ≤ 12` 且（二期）特写占比 ≥ 0.4 | A 情感冲突 / D 情绪爆发 |
| 分析类 | 台词密度高（二期 ASR 词速）+ 前 3s 信息钩子（L2 判定） | E 悬念前置 |

实测依据（docs/calibration/signal_matrix_v1.json）：短剧素材整体快节奏
（cut_rate 17.5~27.9/min、mean_motion 0.28~0.50），原示例值 0.12/8 完全
脱离真实分布；高燃向样本（motion ≥ 0.44 且 peak ≥ 0.73）与相对平静样本
（motion 0.28、luminance_spike 0.02）在运动/亮度信号上可分。

规则卡以 YAML 配置存放（具体 schema 见 [`style-profiles-yaml-design.md`](style-profiles-yaml-design.md)，
示例见 `configs/style_profiles.yaml`）；多风格命中时输出全部标签 +
置信度，由上层决定展示或 L2 复核。

## 4. 二期信号扩展路线（设计预留，未实现）

| 信号 | 方法 | 依赖 | 说明 |
|---|---|---|---|
| 特写占比 | 人脸检测框面积 / 画面面积分布 | mediapipe face（本地免费）或 SigLIP2 shot_size | 伤感类核心判据；优先 mediapipe（CPU 快） |
| 台词密度/语速 | faster-whisper 转录词级时间戳 → 词/秒 | faster-whisper small（本地，Framework 已验证） | 分析类核心判据 |
| 音频节奏（BPM/能量包络） | librosa 频谱分析 | librosa（本地） | 燃向 BGM 节奏判定 |
| 特效帧占比（精） | SigLIP2 `visual_effect` closed-set 滑窗 | SigLIP2（本地但慢，需批量规划） | 替代一期亮度突变的粗信号 |
| 色彩情绪 | 主色温/饱和度统计 + SigLIP2 mood | numpy + SigLIP2 | 伤感冷色调辅助判据 |

扩展信号一律**新增字段进 `SignalValues`（schema_version 升版）**，不破坏现有消费方。

## 5. 阈值标定流程

信号层输出的阈值目前是"经验初值"，上线前必须标定：

1. **样本集**：从客户素材库抽 30~50 支视频，人工标注风格（燃向/伤感/分析类/其他，各 ≥10 支）；样本集为何必须、怎么准备，见 [`style-profiles-yaml-design.md`](style-profiles-yaml-design.md) §4；
2. **跑批**：对全部样本调 `/v1/signals/compute`，落盘信号矩阵；
3. **标定**：按风格分组看信号分布（箱线图），取分位数定阈值；重叠严重的信号组合提交 L2 复核策略；
4. **回归**：标定后的阈值写入规则卡并附样本清单，作为回归测试基线（与 Framework holdout 签核机制同思路：人工签核后才允许生产启用）；
5. **持续**：每月用新素材复标一次，防止内容分布漂移。

### 5.1 首轮实测记录（2026-08，6 支素材）

- **样本**：`untitled folder` 内 5 支真实短剧（58~141s，1080p）+ 1 支 5s AI 生成参考片；
- **性能调节**：逐帧 seek 抽帧改为单次解码批量抽帧（fps 重采样 + -t 限长），
  全批耗时 531s → 111s；最长单片 244.5s → 50.5s（剩余瓶颈为场景切点全片解码）；
- **信号分布**：短剧素材整体快节奏，mean_motion 0.28~0.50、cut_rate 17.5~27.9/min；
  原设计文档示例阈值（motion 0.12）已按实测上调（见 §3 表）；
- **结论**：本批全部是"快节奏剧情向"，**无法标定伤感/分析类的下界阈值**，
  必须补充慢节奏负样本；矩阵存档于 `docs/calibration/signal_matrix_v0.json`
  （调节前）与 `signal_matrix_v1.json`（调节后）。

### 5.2 第二轮实测（2026-08，11 支样本，9 支带质量标注）

依据 `AIGC/tagging/短剧案例.xlsx`（差质量/高质量两个 sheet）建立文件 ID→剧名→
质量标注映射（`docs/calibration/sample_set.json`），新增下载 5 支 OSS 直链素材
（3 高质量 + 2 差质量），全部 11 支跑信号层，矩阵存档
`signal_matrix_v2.json`。

**关键发现**：

1. **continuous_motion_window_ratio 是目前唯一有区分度的质量信号**：9 支带标注
   样本中 8 支为 0.75，唯一差质量低分样本（Love Died When He Chose Her）仅 0.25；
   已写入规则卡 `quality_flag`（阈值 0.5，仅提示不拦截，待 30+ 支样本后复评）；
2. **切镜率与质量标签无关**：高质量 17.98~36.07/min、差质量 17.47~21.45/min，
   区间完全重叠，不作为质量判据；
3. **风格假设需数据驱动修正**：预期慢节奏的蛇妖爱情剧 THE SERPENT'S VOW
   切点率全批最高（36.07/min）——本土 AI 短剧不论题材普遍快切，"伤感=慢节奏"
   的假设在这类素材上不成立，伤感卡继续禁用；
4. **样本量仍是硬约束**：9 支带标注样本不足以置 `status: calibrated`（需 ≥30），
   也不足以支撑多风格分类，样本集扩充仍是下一步关键依赖；
5. **性能**：新增 5 支单片耗时 8.1~43.3s（60~100s 片），批量解码方案在
   更多素材上稳定。

## 6. 性能预算

实测（1080p，M 系 CPU，v1 批量解码后）：

| 片长 | 实测单片耗时 | 主导成本 |
|---|---|---|
| 5s | 0.8s | 固定开销 |
| 58~60s | 7.8~14.0s | 场景切点解码 |
| 99~141s | 11.5~50.5s | 场景切点解码（码率高时放大） |

| 操作 | 预算（1080p / 60s 片，M 系 CPU） |
|---|---|
| ffprobe + 切镜检测 | ≤ 25s（解码 1 次，主导项；二期可降宽到 480px 再解码） |
| 运动分析 + 亮度突变 | ≤ 10s（合并为单次解码批量抽帧，≤152 帧 @320px） |
| volumedetect | ≤ 5s |
| **合计单片** | **目标 ≤ 40s（60s 片已达标）**；批量用进程池并行，N 路并发按 CPU 核数 |

超时保护：所有 ffmpeg 调用有 `ffmpeg_timeout_seconds`（默认 300s）上限，失败抛
`MediaError` → HTTP 422，不把超时伪装成零值。

## 7. 与既有资产的边界

- **不修改** Framework 仓库：本项目把其运动分析/场景检测语义以同阈值复刻为
  HTTP 服务（Framework 原实现无 HTTP 路由，且其架构门禁不适合快速迭代）；
  若后续 Framework 上线运动 HTTP 路由，本服务可退化为代理；
- 信号层结果将来入库后，与 Framework 的 `VideoManifest`（scene/transcript
  时间戳）对齐引用同一片源，避免重复解码；
- 合规剔除（血腥暴力）**不在信号层**：属 L1/L4 门禁，走 tagging 14 维合规。
