"""FastAPI 应用入口。

运行：
    uvicorn app.main:app --port 8010
环境变量见 app/config.py（SMARTCLIP_*）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .job_service import JobStore
from .narrative import NarrativeConfigError, load_narrative_targets
from .platform_profiles import PlatformProfilesError, load_platform_profiles
from .routes import router
from .style_profiles import RuleBookError, load_rule_book

logger = logging.getLogger("smartclip")


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="beidou-smart-clip 分析服务",
        description=(
            "一键式视频筛选与批量 15s 剪辑 · L0 本地零成本分析层 + "
            "L1 规则卡求值 / L2 移交计划 / L3 剪辑计划 / L4 合规门禁"
        ),
        version="0.3.0",
    )
    app.state.settings = settings or Settings.from_env()
    # 启动时加载规则书；失败时降级（相关路由 503）并告警，不阻塞 L0 分析能力
    try:
        app.state.rule_book = load_rule_book(app.state.settings.rule_book_path)
    except RuleBookError as exc:
        logger.error("规则书加载失败，L1/L2/L4 路由不可用：%s", exc)
        app.state.rule_book = None
    # 平台画像（分类体系 + 判重规则）同样降级不阻塞
    try:
        app.state.platform_profiles = load_platform_profiles(
            app.state.settings.platform_profiles_path
        )
    except PlatformProfilesError as exc:
        logger.error("平台画像加载失败，分类/降重路由不可用：%s", exc)
        app.state.platform_profiles = None
    # 叙事剪辑目标与模板同样降级不阻塞
    try:
        app.state.narrative_book = load_narrative_targets(
            app.state.settings.narrative_targets_path
        )
    except NarrativeConfigError as exc:
        logger.error("叙事目标加载失败，分镜/叙事路由不可用：%s", exc)
        app.state.narrative_book = None
    app.state.job_store = JobStore(app.state.settings, app.state.rule_book)
    app.include_router(router, tags=["analysis"])
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    return app


app = create_app()
