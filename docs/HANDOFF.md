# beidou-smart-clip · 开发交接（v0.3.0）

更新日期：2026-08-20

## 当前可用链路

```text
视频路径
  → POST /v1/signals/compute（视频级 signals + 排序 candidates）
  → POST /v1/profiles/evaluate（规则命中与 L2 移交）
  → POST /v1/l2/review（可选，真实多模态复核）
  → POST /v1/clip/plan（时间轴）
  → POST /v1/clip/render（MP4）
```

L4 门禁与降重接口仍独立调用：`/v1/gatekeeper/check`、`/v1/dedup/*`。

## 本轮交付

1. **L3 已可执行。** `app/clip_executor.py` 将确认的片段重编码为 H.264/AAC，使用
   FFmpeg concat 输出 MP4；`/v1/clip/render` 为实际渲染入口。
2. **L0 已输出窗口候选。** `SignalsResponse.candidates` 含候选窗口、运动/亮度指标、
   时序模式、分数与分数分量，并按 `score DESC, start ASC` 排序。
3. **L2 已接入真实服务。** `app/l2_service.py` 使用 OpenAI-compatible Chat
   Completions，按规则卡的 `prompt_ref`、`max_windows` 执行；支持外部 `image_urls`
   或从 `video_path` 自动抽 JPEG data URL。
4. **剪辑选择算法已修正。** 重叠窗口按新增有效时长计预算，不再重复扣减。
5. **质量防护。** 已补充 ROI、视觉目录、规则书、平台配置、帧指纹与媒体尺寸校验。
6. **P0/P1 最小闭环已实现。** `app/job_service.py` 提供文件持久化任务、后台 worker、
   状态事件、取消与失败重试；任务会保存 L0/L1/L2/L3 的完整中间结果。
7. **交互入口已补齐。** `/` 提供任务控制台（创建、轮询、真实记录、成片播放），
   `smartclip` 提供 `serve/run/status/watch/jobs` 简化命令。

## 验证命令

在项目根目录执行：

```bash
python3 -m pytest -q
python3 -m compileall -q app
python3 -m uvicorn app.main:app --port 8010
curl -s http://127.0.0.1:8010/ready | python3 -m json.tool
```

当前测试集：88 个用例，覆盖渲染、候选排序和 L2 HTTP 适配器的 mock 调用。

## 真实 L2 配置

```bash
export SMARTCLIP_L2_ENABLED=1
export SMARTCLIP_L2_API_KEY='...'
export SMARTCLIP_L2_BASE_URL='https://<provider>/v1/chat/completions'
export SMARTCLIP_L2_MODEL='qwen-vl-plus'
```

未配置上述三项时，`/v1/l2/review` 返回 503；不会影响 L0、L1、L3、L4。
真实供应商凭据未写入仓库，尚未进行线上实际调用验收。

## 关键文件

| 文件 | 责任 |
|---|---|
| `app/signals.py` | L0 信号计算、候选排序 |
| `app/clip_planner.py` | 候选窗口 → 时间轴 |
| `app/clip_executor.py` | 时间轴 → MP4 |
| `app/l2_service.py` | L2 HTTP 适配与 JSON 解析 |
| `app/routes.py` | API 编排与错误码 |
| `configs/style_profiles.yaml` | 风格、评分、L2 prompt 规则 |
| `prompts/*.md` | L2 受控复核提示词 |

## 当前限制

- 后台任务使用进程内 `ThreadPoolExecutor` + JSON 持久化；已有进度状态、取消和失败重试，
  但服务重启后不会自动重新调度 queued/running 任务，也未接入外部队列。
- 转场、响度归一、音频 crossfade、字幕烧录尚未实现。
- 窗口候选只使用运动、连续性和亮度的初始权重，尚未基于人工选片数据标定。
- L2 返回 JSON 已解析，但未校验不同 prompt 的专属 JSON schema，也未把结果自动回写为
  剪辑计划或合规标签。
- 生产服务仍接受本地 `video_path` / `output_path`；在多租户部署前必须改为受控素材 ID 或
  对象存储 URI。
