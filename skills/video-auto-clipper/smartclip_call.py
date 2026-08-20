#!/usr/bin/env python3
"""video-auto-clipper skill 的零依赖调用脚本（仅 Python 标准库）。

用法：
    python3 smartclip_call.py run <video_path> [--duration 15] [--no-l2]
    python3 smartclip_call.py signals <video_path>
    python3 smartclip_call.py dedup <video_path>
    python3 smartclip_call.py templates          # 各类型视频的剪辑模板倾向
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
                print(f"\n成片地址：{BASE}/v1/jobs/{job_id}/product", file=sys.stderr)
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
