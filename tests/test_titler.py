"""成品自动命名（titler）单测 + 任务链路/标题预览端到端。"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.titler import (
    TitleContext,
    generate_titles,
    product_filename,
    sanitize_title,
    title_for_path,
)


# ---------- 纯逻辑 ----------


def test_generate_titles_style_pool_and_determinism():
    context = TitleContext(style_hint="ran_xiang", duration_seconds=15.0)
    first = generate_titles(context)
    second = generate_titles(context)
    assert first == second  # 同输入确定性输出
    assert first, "至少应给出一个候选标题"
    assert len(first) <= 3
    # 推荐标题应落在发布友好的长度甜点区
    assert 4 <= len(first[0]) <= 30


def test_generate_titles_character_action_slot():
    with_pair = TitleContext(
        style_hint="group_action",
        character_actions=(("宙斯", "释放闪电"),),
        duration_seconds=15.0,
    )
    titles = generate_titles(with_pair)
    assert any("宙斯" in title for title in titles)
    # 无线索时 {subject} 模板不得泄漏槽位
    without = TitleContext(style_hint="group_action")
    assert all("{subject}" not in title for title in generate_titles(without))


def test_generate_titles_fallback_generic():
    titles = generate_titles(TitleContext())
    assert titles, "无任何线索时应回退通用池"
    assert all("{" not in title and "}" not in title for title in titles)


def test_sanitize_title_removes_path_and_reserved():
    assert "/" not in sanitize_title("高燃/混剪：第一集")
    assert '"' not in sanitize_title('标题"带引号"')
    assert sanitize_title("con") == "untitled_clip"
    assert sanitize_title("   ") == "untitled_clip"
    assert sanitize_title("x" * 120).count("x") <= 60


def test_product_filename_dedup():
    existing = {"这爆发力直接封神！.mp4"}
    name = product_filename("这爆发力直接封神！", existing)
    assert name == "这爆发力直接封神！_2.mp4"
    assert product_filename("全新标题", set()) == "全新标题.mp4"


def test_title_for_path_roundtrip():
    assert title_for_path("/tmp/products/当雷霆落下.mp4") == "当雷霆落下"


# ---------- 端到端：任务自动命名 ----------


def _wait_terminal(client: TestClient, job_id: str) -> dict:
    detail: dict = {}
    for _ in range(120):
        detail = client.get(f"/v1/jobs/{job_id}").json()
        if detail["status"] in {"completed", "failed", "cancelled"}:
            return detail
        time.sleep(0.1)
    return detail


def test_job_auto_names_product_with_publishable_title(small_video, tmp_path):
    client = TestClient(
        create_app(Settings(products_dir=str(tmp_path / "products"), job_workers=1))
    )
    created = client.post(
        "/v1/jobs",
        json={
            "video_path": str(small_video),
            "motion_window_count": 4,
            "request_l2": False,
            # 不给 output_name → 走自动命名
        },
    )
    assert created.status_code == 202
    detail = _wait_terminal(client, created.json()["job_id"])
    assert detail["status"] == "completed", detail
    output = Path(detail["output_path"])
    assert output.is_file()
    # 文件名不再是 job_id 乱码，而是可发布的中文标题
    assert output.stem != created.json()["job_id"]
    assert detail["title"] and detail["title"] == output.stem
    assert detail["result"]["title_candidates"], "应保留备选标题"
    assert all(char not in output.name for char in '<>:"/\\|*')


def test_job_explicit_output_name_wins(small_video, tmp_path):
    client = TestClient(
        create_app(Settings(products_dir=str(tmp_path / "products"), job_workers=1))
    )
    created = client.post(
        "/v1/jobs",
        json={
            "video_path": str(small_video),
            "motion_window_count": 4,
            "request_l2": False,
            "output_name": "custom.mp4",
        },
    )
    detail = _wait_terminal(client, created.json()["job_id"])
    assert detail["status"] == "completed", detail
    assert Path(detail["output_path"]).name == "custom.mp4"
    assert detail["title"] == "custom"


# ---------- 端到端：标题预览路由 ----------


def test_title_preview_route(small_video):
    client = TestClient(create_app())
    response = client.post(
        "/v1/titles/preview",
        json={"video_path": str(small_video), "style_id": "ran_xiang"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "title-preview-1.0"
    assert body["recommended"] == body["candidates"][0]
    assert body["filename"].endswith(".mp4")


def test_title_preview_with_character_actions(small_video):
    client = TestClient(create_app())
    response = client.post(
        "/v1/titles/preview",
        json={
            "video_path": str(small_video),
            "style_id": "ran_xiang",
            "character_actions": [{"subject": "宙斯", "action": "释放闪电"}],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert any("宙斯" in title for title in body["candidates"])
    # 含人物名的个性化标题应被优先推荐
    assert "宙斯" in body["recommended"]


def test_title_preview_rejects_bad_character_actions(small_video):
    client = TestClient(create_app())
    response = client.post(
        "/v1/titles/preview",
        json={
            "video_path": str(small_video),
            "character_actions": [{"subject": "宙斯"}],
        },
    )
    assert response.status_code == 400
