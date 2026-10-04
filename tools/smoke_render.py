"""模板渲染冒烟：用插件真实的 payload 形状渲一张图，核对排版与几何。

    python tools/smoke_render.py
    python tools/smoke_render.py --base 底卡.png --avatar 头像.png

不依赖 AstrBot：直接读 templates/standalone/mingpian.html，用 Playwright 截图。
断言与线上渲染一致：右栏元素必须在诗句面板里水平居中，各区块不得溢出卡片。
"""

from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# core 模块从 astrbot.api 取 logger，本工具在 AstrBot 之外运行，补一个占位模块
if "astrbot" not in sys.modules:
    from unittest.mock import MagicMock

    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = MagicMock()
    _astrbot.api = _api
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

from core.card import build_payload

CARD_W, CARD_H = 1280, 720
SCALE = 2
TEMPLATE = ROOT / "templates" / "standalone" / "mingpian.html"


def _placeholder(kind: str) -> bytes:
    """没有真图时造一张纯色 PNG，只为把模板跑起来。"""
    from io import BytesIO

    from PIL import Image

    color = (90, 60, 110) if kind == "base" else (200, 170, 210)
    buffer = BytesIO()
    Image.new("RGB", (2048, 1152) if kind == "base" else (1024, 1024), color).save(
        buffer, "PNG"
    )
    return buffer.getvalue()


def render(html: str, out: Path) -> Path:
    from playwright.sync_api import sync_playwright

    out = Path(out).resolve()
    tmp = out.with_suffix(".html")
    tmp.write_text(html, encoding="utf-8")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": CARD_W, "height": CARD_H}, device_scale_factor=SCALE
        )
        page.goto(tmp.as_uri())
        page.wait_for_timeout(400)

        box = lambda sel: (
            page.locator(sel).bounding_box() if page.locator(sel).count() else None
        )
        side, card = box(".panel--side"), box(".card")

        if side:
            for sel, label in (
                (".side__emblem", "门派徽记"),
                (".side__logo", "剑网3标识"),
                (".side__bars", "条码"),
            ):
                b = box(sel)
                if not b:
                    continue
                gap_l = b["x"] - side["x"]
                gap_r = (side["x"] + side["width"]) - (b["x"] + b["width"])
                assert abs(gap_l - gap_r) <= 2, (
                    f"{label} 没在诗句面板里居中：左 {gap_l:.0f}px 右 {gap_r:.0f}px"
                )

        if side and card:
            assert (
                side["y"] >= -1 and side["y"] + side["height"] <= card["height"] + 1
            ), "诗句面板溢出卡片"

        page.locator(".card").screenshot(path=str(out))
        browser.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=Path, default=None, help="无字底卡（不填用占位图）")
    ap.add_argument("--avatar", type=Path, default=None, help="头像（不填用占位图）")
    ap.add_argument("--school", default="唐门")
    ap.add_argument("--out", type=Path, default=ROOT / "tmp" / "smoke.png")
    args = ap.parse_args()

    base = (
        args.base.read_bytes()
        if args.base and args.base.exists()
        else _placeholder("base")
    )
    avatar = (
        args.avatar.read_bytes()
        if args.avatar and args.avatar.exists()
        else _placeholder("avatar")
    )

    payload = build_payload(
        base_bytes=base,
        avatar_bytes=avatar,
        school=args.school,
        nickname="小螺卜头",
        server="飞龙在天",
        body="萝莉",
        camp="恶人谷",
        tong="英雄长乐坊",
    )

    from jinja2 import Environment

    html = (
        Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
        .from_string(TEMPLATE.read_text(encoding="utf-8"))
        .render(**payload)
    )
    leftovers = [m for m in ("{{", "{%") if m in html]
    assert not leftovers, f"模板有没解析的标记: {leftovers}"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    render(html, args.out)

    from PIL import Image

    with Image.open(args.out) as im:
        size = im.size
    assert size == (CARD_W * SCALE, CARD_H * SCALE), f"截图尺寸异常: {size}"
    print(f"OK {args.out} {size[0]}x{size[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
