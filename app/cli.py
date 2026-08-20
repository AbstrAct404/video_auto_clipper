"""简洁的 smartclip 命令行：启动、提交、查看与等待任务。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _api(method: str, url: str, body: dict | None = None) -> dict:
    request = Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"API {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接服务：{exc.reason}") from exc


def _watch(base: str, job_id: str) -> int:
    while True:
        job = _api("GET", f"{base}/v1/jobs/{job_id}")
        print(f"{job['status']:10} {job['updated_at']}")
        if job["status"] in {"completed", "failed", "cancelled"}:
            if job.get("output_path"):
                print(f"成片：{job['output_path']}")
            if job.get("error"):
                print(f"失败：{job['error']}", file=sys.stderr)
            return 0 if job["status"] == "completed" else 1
        time.sleep(1.2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smartclip", description="北斗智影智能剪辑控制台 CLI")
    parser.add_argument("--api", default="http://127.0.0.1:8010", help="服务地址")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="启动本地 Web 控制台与 API")
    submit = sub.add_parser("run", help="提交视频并等待成片")
    submit.add_argument("video_path")
    submit.add_argument("--name", help="products 下的成片文件名")
    submit.add_argument("--duration", type=float, default=15)
    submit.add_argument("--windows", type=int, default=8)
    submit.add_argument("--no-l2", action="store_true")
    status = sub.add_parser("status", help="查看任务状态")
    status.add_argument("job_id")
    watch = sub.add_parser("watch", help="持续查看任务直至结束")
    watch.add_argument("job_id")
    sub.add_parser("jobs", help="列出最近任务")
    args = parser.parse_args(argv)

    if args.command == "serve":
        return subprocess.call([sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8010"])
    if args.command == "run":
        payload = {
            "video_path": args.video_path,
            "target_duration_seconds": args.duration,
            "motion_window_count": args.windows,
            "request_l2": not args.no_l2,
        }
        if args.name:
            payload["output_name"] = args.name
        job = _api("POST", f"{args.api}/v1/jobs", payload)
        print(f"任务：{job['job_id']}")
        return _watch(args.api, job["job_id"])
    if args.command == "status":
        print(json.dumps(_api("GET", f"{args.api}/v1/jobs/{args.job_id}"), ensure_ascii=False, indent=2))
        return 0
    if args.command == "watch":
        return _watch(args.api, args.job_id)
    print(json.dumps(_api("GET", f"{args.api}/v1/jobs"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
