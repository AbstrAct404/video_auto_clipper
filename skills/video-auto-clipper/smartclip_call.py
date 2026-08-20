#!/usr/bin/env python3
"""video-auto-clipper skill 的零依赖调用脚本（仅 Python 标准库）。

用法：
    python3 smartclip_call.py run <video_path> [--duration 15] [--no-l2]
    python3 smartclip_call.py signals <video_path>
    python3 smartclip_call.py dedup <video_path>
    python3 smartclip_call.py templates          # 各类型视频的剪辑模板倾向
    python3 smartclip_call.py storyboard <video_path>   # 剪辑前分镜捕捉
    python3 smartclip_call.py narrative <video_path> [--style ran_xiang] [--template hook_first] [--interleave]
    python3 smartclip_call.py platforms
    python3 smartclip_call.py jobs
    python3 smartclip_call.py status <job_id>

服务地址由环境变量 SMARTCLIP_BASE_URL 指定，默认 http://127.0.0.1:8010。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = os.environ.get("SMARTCLIP_BASE_URL", "http://127.0.0.1:8010").rstrip("/")


def _api(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    request = Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"服务返回 {exc.code}：{detail}")
    except URLError as exc:
        raise SystemExit(
            f"无法连接 {BASE}：{exc.reason}\n"
            "请先启动服务：python3 -m uvicorn app.main:app --port 8010"
        )


def _dump(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


# 剪辑模板倾向的 prefer 语义对照（与 configs/style_profiles.yaml clip_strategy 对齐）
_PREFER_EXPLAIN = {
    "high_motion_segments": "优先挑高运动片段（打斗/追逐/快速推拉），保留能量顶点",
    "hook_first_3s": "前 3 秒必须上钩子（悬念/冲突直给），后接快节奏对白",
    "close_up_slow_pace": "优先特写镜头 + 慢节奏情绪段落，保留长镜头不切碎",
    "flagged_for_review": "仅作异常标记转人工复核，不自动选窗剪辑",
}


def cmd_templates() -> int:
    data = _api("GET", "/v1/profiles")
    print(f"规则书标定状态：{data['calibration']['status']}\n")
    for profile in data["profiles"]:
        strategy = profile.get("clip_strategy") or {}
        state = "启用" if profile["enabled"] else "停用（缺标定样本）"
        print(f"◆ {profile['display_name']}（{profile['profile_id']}） [{state}]")
        if strategy:
            prefer = strategy.get("prefer", "")
            print(
                f"  目标时长：{strategy.get('target_duration_seconds', 15)}s"
                f"{' · 单片最短 ' + str(strategy['min_segment_seconds']) + 's' if strategy.get('min_segment_seconds') else ''}"
            )
            print(f"  剪辑倾向：{prefer} —— {_PREFER_EXPLAIN.get(prefer, '见规则书 notes')}")
        if profile.get("hooks"):
            print(f"  钩子偏好：{'、'.join(profile['hooks'])}")
        print()
    return 0


def cmd_storyboard(args: argparse.Namespace) -> int:
    """剪辑前分镜捕捉：全片切点分镜 + 逐镜采样信号。"""
    data = _api(
        "POST",
        "/v1/storyboard/extract",
        {"video_path": args.video_path, "frames_per_shot": args.frames_per_shot},
        timeout=300,
    )
    print(
        f"时长 {data['duration_seconds']}s，捕捉到 {data['shot_count']} 个镜头\n",
        file=sys.stderr,
    )
    for shot in data["shots"]:
        print(
            f"镜头{shot['index']:>2} {shot['start_seconds']:6.2f}-{shot['end_seconds']:6.2f}"
            f"  运动={shot['mean_motion_intensity']:.2f}"
            f"  亮度突变={shot['luminance_spike_ratio']:.2f}"
        )
    return 0


def cmd_narrative(args: argparse.Namespace) -> int:
    """叙事剪辑计划：风格→画面目标匹配 + 混剪重排（人物-行动交错可选）。"""
    body = {
        "video_path": args.video_path,
        "template_id": args.template,
        "target_duration_seconds": args.duration,
        "frames_per_shot": args.frames_per_shot,
        "interleave": args.interleave,
    }
    if args.style:
        body["style_id"] = args.style
    data = _api("POST", "/v1/narrative/plan", body, timeout=300)
    print(
        f"模板 {data['template_id']}：命中 {len(data['matches'])} 个目标，"
        f"排布 {len(data['segments'])} 段，共 {data['total_duration_seconds']}s"
    )
    for note in data["notes"]:
        print(f"  注：{note}")
    print("\n命中目标（按分数）：")
    for match in data["matches"][:8]:
        print(
            f"  {match['target_id']} 镜头{match['shot_indexes']}"
            f" {match['start_seconds']:.2f}-{match['end_seconds']:.2f}s"
            f" 分数={match['score']}"
        )
    print("\n成片时间轴（混剪顺序）：")
    for segment in data["segments"]:
        tag = "开场钩子" if segment["role"] == "opening" else "        "
        print(
            f"  {tag} 原片 {segment['start_seconds']:.2f}-{segment['end_seconds']:.2f}s"
            f" ← {'、'.join(segment['target_ids'])}"
        )
    if args.interleave:
        print("\n提示：人物-行动交错为本地时间段近似，人物/行动语义需 L2 标注确认。")
    return 0


def cmd_title(args: argparse.Namespace) -> int:
    """成片标题预览：风格→画面命中 → 可直接发布的剪辑名（成品自动同名）。"""
    body = {
        "video_path": args.video_path,
        "target_duration_seconds": args.duration,
    }
    if args.style:
        body["style_id"] = args.style
    if args.character:
        body["character_actions"] = [
            {"subject": subject, "action": action}
            for subject, action in (pair.split(":", 1) for pair in args.character)
        ]
    data = _api("POST", "/v1/titles/preview", body, timeout=300)
    print(f"推荐标题：{data['recommended']}")
    print(f"建议文件名：{data['filename']}")
    print(f"命中画面目标：{'、'.join(data['matched_target_ids']) or '无'}")
    print("备选标题：")
    for title in data["candidates"]:
        print(f"  - {title}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    job = _api(
        "POST",
        "/v1/jobs",
        {
            "video_path": args.video_path,
            "target_duration_seconds": args.duration,
            "motion_window_count": args.windows,
            "request_l2": not args.no_l2,
        },
    )
    job_id = job["job_id"]
    print(f"已提交任务：{job_id}", file=sys.stderr)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        detail = _api("GET", f"/v1/jobs/{job_id}")
        status = detail["status"]
        print(f"状态：{status}", file=sys.stderr)
        if status in {"completed", "failed", "cancelled"}:
            _dump(detail)
            if status == "completed":
                if detail.get("title"):
                    print(f"\n成片标题：{detail['title']}", file=sys.stderr)
                print(f"成片地址：{BASE}/v1/jobs/{job_id}/product", file=sys.stderr)
            return 0 if status == "completed" else 1
        time.sleep(3)
    raise SystemExit(f"等待超时（{args.timeout}s），可用 status {job_id} 继续查询")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="smartclip skill 调用脚本")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="一键成片并等待完成")
    run.add_argument("video_path")
    run.add_argument("--duration", type=float, default=15)
    run.add_argument("--windows", type=int, default=8)
    run.add_argument("--no-l2", action="store_true", help="跳过云端 LLM 复核")
    run.add_argument("--timeout", type=int, default=300, help="最长等待秒数")

    signals = sub.add_parser("signals", help="L0 信号分析")
    signals.add_argument("video_path")

    dedup = sub.add_parser("dedup", help="降重体检")
    dedup.add_argument("video_path")
    dedup.add_argument("--max-frames", type=int, default=16)

    sub.add_parser("templates", help="各类型视频的剪辑模板倾向")

    storyboard = sub.add_parser("storyboard", help="剪辑前分镜捕捉（镜头级信号）")
    storyboard.add_argument("video_path")
    storyboard.add_argument("--frames-per-shot", type=int, default=3)

    narrative = sub.add_parser("narrative", help="叙事剪辑计划（风格→画面 + 混剪重排）")
    narrative.add_argument("video_path")
    narrative.add_argument("--style", help="风格 id（如 ran_xiang），只评估归属该风格的目标")
    narrative.add_argument(
        "--template",
        default="hook_first",
        help="linear/hook_first/climax_first/twist_bridge（默认 hook_first）",
    )
    narrative.add_argument("--duration", type=float, default=15)
    narrative.add_argument("--frames-per-shot", type=int, default=3)
    narrative.add_argument("--interleave", action="store_true", help="人物-行动交错（群像混剪）")

    title = sub.add_parser("title", help="成片标题预览（可发布的剪辑名）")
    title.add_argument("video_path")
    title.add_argument("--style", help="风格 id（如 ran_xiang）")
    title.add_argument("--duration", type=float, default=15)
    title.add_argument(
        "--character",
        action="append",
        metavar="人物:行动",
        help="人物-行动线索（可多次，如 宙斯:释放闪电），来自 L2 标注或人工提供",
    )

    sub.add_parser("platforms", help="平台画像")
    sub.add_parser("jobs", help="任务列表")

    status = sub.add_parser("status", help="任务详情")
    status.add_argument("job_id")

    args = parser.parse_args(argv)

    # 前置探活
    _api("GET", "/ready", timeout=5)

    if args.command == "run":
        return cmd_run(args)
    if args.command == "templates":
        return cmd_templates()
    if args.command == "storyboard":
        return cmd_storyboard(args)
    if args.command == "narrative":
        return cmd_narrative(args)
    if args.command == "title":
        return cmd_title(args)
    if args.command == "signals":
        _dump(_api("POST", "/v1/signals/compute", {"video_path": args.video_path}))
        return 0
    if args.command == "dedup":
        _dump(
            _api(
                "POST",
                "/v1/dedup/analyze",
                {"video_path": args.video_path, "max_frames": args.max_frames},
            )
        )
        return 0
    if args.command == "platforms":
        _dump(_api("GET", "/v1/platforms"))
        return 0
    if args.command == "jobs":
        _dump(_api("GET", "/v1/jobs"))
        return 0
    _dump(_api("GET", f"/v1/jobs/{args.job_id}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
