from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path


REQUIRED_SYMBOLS = {
    "xiaohongshu": (
        ("store/xhs/__init__.py", "update_xhs_note"),
        ("store/xhs/__init__.py", "get_video_url_arr"),
        ("media_platform/xhs/client.py", "get_notes_by_creator"),
        ("media_platform/xhs/playwright_sign.py", "sign_with_xhshow"),
    ),
    "douyin": (
        ("store/douyin/__init__.py", "update_douyin_aweme"),
        ("store/douyin/__init__.py", "_extract_note_image_list"),
        ("store/douyin/__init__.py", "_extract_video_download_url"),
        ("media_platform/douyin/client.py", "get_user_aweme_posts"),
    ),
    "bilibili": (
        ("store/bilibili/__init__.py", "update_bilibili_video"),
        ("media_platform/bilibili/help.py", "parse_video_info_from_url"),
        ("media_platform/bilibili/core.py", "get_video_play_url_task"),
        ("media_platform/bilibili/core.py", "get_video_info_task"),
    ),
    "weibo": (
        ("store/weibo/__init__.py", "update_weibo_note"),
        ("media_platform/weibo/client.py", "get_notes_by_creator"),
        ("media_platform/weibo/core.py", "get_note_info_task"),
        ("media_platform/weibo/core.py", "get_note_images"),
        ("media_platform/weibo/core.py", "launch_browser"),
        ("media_platform/weibo/login.py", "login_by_qrcode"),
    ),
}


def _defined_symbols(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


@lru_cache(maxsize=4)
def verify_upstream_compatibility(root: Path) -> dict:
    platforms: dict[str, dict] = {}
    for platform, requirements in REQUIRED_SYMBOLS.items():
        missing = [
            f"{relative_path}:{symbol}"
            for relative_path, symbol in requirements
            if symbol not in _defined_symbols(root / relative_path)
        ]
        platforms[platform] = {
            "compatible": not missing,
            "missing": missing,
        }
    return {
        "compatible": all(value["compatible"] for value in platforms.values()),
        "platforms": platforms,
    }
