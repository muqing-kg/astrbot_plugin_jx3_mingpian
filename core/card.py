"""名片卡构建：取名片形象图，生成底卡与头像，组装模板 payload。

底卡与头像并发生成，各自按配置重试。任一张失败则整体失败。
"""

from __future__ import annotations

import asyncio
import base64
import random
import tempfile
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .image_api import ImageAPIClient, ImageAPIError
from .jx3api import JX3APIClient, JX3APIError
from .mingpian_data import (
    AVATAR_PROMPT,
    BASE_PROMPT,
    sect_accent,
    sect_card_fields,
    sect_scene,
)
from .prompts import Prompt
from .sizes import DEFAULT_CARD_RATIO, RATIO_SIZES, prompt_size

ASSET_ROOT = Path(__file__).resolve().parent.parent / "templates"
FONT_DIR = ASSET_ROOT / "font"
IMG_DIR = ASSET_ROOT / "img"

# 名片卡默认出图尺寸。底图实际比例由配置项 mingpian_card_ratio 决定，
# 这里只是调用方漏传时的兜底默认值
CARD_SIZE = RATIO_SIZES[DEFAULT_CARD_RATIO]  # 无字底卡，默认 16:9
AVATAR_SIZE = "1024x1024"  # 正面头像，固定 1:1


def _data_uri(path: Path, mime: str) -> str:
    if not path.exists():
        return ""
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


@lru_cache(maxsize=1)
def card_font_uri() -> str:
    """正文宋体，随模板下发以保证字形一致。"""
    return _data_uri(FONT_DIR / "名片字体.woff2", "font/woff2")


@lru_cache(maxsize=1)
def brush_font_uri() -> str:
    """诗句用行楷。"""
    return _data_uri(FONT_DIR / "STXINGKA.TTF", "font/ttf")


@lru_cache(maxsize=1)
def logo_uri() -> str:
    return _data_uri(IMG_DIR / "剑网3标识.png", "image/png")


@lru_cache(maxsize=1)
def ref_card_path() -> str:
    """卡面样式参考图，生成底卡时作为 Image 1 传入。"""
    path = IMG_DIR / "mingpian" / "卡面参考.png"
    return str(path) if path.exists() else ""


def build_payload(
    *,
    base_bytes: bytes,
    avatar_bytes: bytes,
    school: str,
    nickname: str,
    server: str,
    body: str,
    camp: str,
    tong: str,
    level: str = "130",
) -> dict[str, Any]:
    """组装模板变量。字段名与 templates/standalone/mingpian.html 一致。"""
    payload: dict[str, Any] = {
        "cardFont": card_font_uri(),
        "brushFont": brush_font_uri(),
        "logo": logo_uri(),
        # 底卡作为背景，文字、徽记与条码由模板叠加
        "bgImage": "data:image/png;base64,"
        + base64.b64encode(base_bytes).decode("ascii"),
        "avatar": "data:image/png;base64,"
        + base64.b64encode(avatar_bytes).decode("ascii"),
        "nickname": nickname,
        "server": server,
        "level": level,
        "body": body,
        "camp": camp,
        "tong": tong,
        "cardDate": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y/%m/%d"),
        "panelShift": 0,
        "school": school,
    }
    if school:
        payload.update(sect_card_fields(school))
    return payload


@dataclass(frozen=True)
class CardSource:
    """一次出卡所需的角色信息。"""

    server: str
    nickname: str
    school: str
    body: str
    camp: str
    tong: str
    art: bytes
    card_no: int
    card_total: int
    accent: str
    scene: str

    @property
    def note(self) -> str:
        return (
            f"{self.server} · {self.nickname} · 第 {self.card_no}/{self.card_total} 张"
        )


async def resolve_card(
    jx3: JX3APIClient,
    *,
    server: str,
    name: str,
    index: int | None = None,
    with_detail: bool = True,
) -> CardSource:
    """取名片形象图；with_detail 为真时再取一次角色详情。

    index 省略时随机取一张，从 1 起。名片特写只用形象图，提示词里不含占位符时
    无需再花一次令牌取角色信息。
    """
    records = await jx3.card_records(server, name)
    if not records:
        raise JX3APIError(f"没拿到 {server} · {name} 的名片记录")

    total = len(records)
    if index is None:
        picked_no = random.randint(1, total)
    else:
        if index < 1 or index > total:
            raise JX3APIError(f"这个角色共 {total} 张名片，序号要填 1~{total}")
        picked_no = index

    art = await jx3.get_bytes(str(records[picked_no - 1].get("showAvatar") or ""))
    if not art:
        raise JX3APIError("名片形象图下载失败，请稍后再试")

    detail = await jx3.role_detail(server, name) if with_detail else {}
    school = str(detail.get("forceName") or "").strip()
    accent, _ = sect_accent(school) if school else ("#8a6aa8", "#4a3560")
    return CardSource(
        server=server,
        nickname=str(detail.get("roleName") or name).strip(),
        school=school,
        body=str(detail.get("bodyName") or "").strip(),
        camp=str(detail.get("campName") or "").strip(),
        tong=str(detail.get("tongName") or "").strip(),
        art=art,
        card_no=picked_no,
        card_total=total,
        accent=accent,
        scene=sect_scene(school),
    )


async def generate_base(
    image: ImageAPIClient,
    source: CardSource,
    prompt: str,
    *,
    size: str = CARD_SIZE,
    with_card_ref: bool = True,
) -> bytes:
    """按提示词生成图片。

    with_card_ref 为真时，参考图顺序为「卡面样式参考、角色形象图」；
    为假时只传角色形象图。
    """
    with tempfile.TemporaryDirectory(prefix="jx3_mingpian_") as tmpdir:
        art_path = Path(tmpdir) / "art.png"
        art_path.write_bytes(source.art)
        ref = ref_card_path() if with_card_ref else ""
        refs = [Path(ref), art_path] if ref else [art_path]
        return await image.edit(prompt, refs, size=size, quality="high")


async def generate_avatar(image: ImageAPIClient, source: CardSource) -> bytes:
    """生成正面头像。"""
    with tempfile.TemporaryDirectory(prefix="jx3_mingpian_") as tmpdir:
        art_path = Path(tmpdir) / "art.png"
        art_path.write_bytes(source.art)
        return await image.edit(
            AVATAR_PROMPT, [art_path], size=AVATAR_SIZE, quality="high"
        )


async def build_card(
    jx3: JX3APIClient,
    image: ImageAPIClient,
    *,
    server: str,
    name: str,
    index: int | None = None,
    size: str = CARD_SIZE,
) -> tuple[dict[str, Any], str]:
    """名片卡：取名片形象图，生成底卡与头像，组装模板 payload。

    size 为底图尺寸，由调用方按配置的名片卡比例给出。
    失败时抛出 JX3APIError 或 ImageAPIError，message 可直接返回给用户。
    """
    if not image.configured:
        raise ImageAPIError("还没配置生图接口，请填写「生图接口地址 / 密钥 / 模型名」")

    source = await resolve_card(jx3, server=server, name=name, index=index)
    prompt = BASE_PROMPT.format(
        school=source.school or "剑网3", scene=source.scene, accent=source.accent
    )

    # 底卡与头像并发生成
    base_result, avatar_result = await asyncio.gather(
        generate_base(image, source, prompt, size=size),
        generate_avatar(image, source),
        return_exceptions=True,
    )
    for result in (base_result, avatar_result):
        if isinstance(result, BaseException):
            raise (
                result
                if isinstance(result, (ImageAPIError, JX3APIError))
                else ImageAPIError(str(result))
            )
    if not isinstance(base_result, bytes) or not isinstance(avatar_result, bytes):
        raise ImageAPIError("生图接口没有返回图片")

    payload = build_payload(
        base_bytes=base_result,
        avatar_bytes=avatar_result,
        school=source.school,
        nickname=source.nickname,
        server=source.server,
        body=source.body,
        camp=source.camp,
        tong=source.tong,
    )
    return payload, source.note


async def build_closeup(
    image: ImageAPIClient,
    source: CardSource,
    prompt: Prompt,
) -> bytes:
    """名片特写：按配置的提示词生成图片，不做文字叠加。

    只传角色形象图，不传卡面样式参考：提示词描述的是纯海报，
    卡面版式（面板、齿孔、条码）不属于其中，作为参考图传入会干扰构图。
    出图尺寸取该提示词自己配置的比例；未指定时交给接口决定。
    """
    if not image.configured:
        raise ImageAPIError("还没配置生图接口，请填写「生图接口地址 / 密钥 / 模型名」")
    return await generate_base(
        image, source, prompt.text, size=prompt_size(prompt.ratio), with_card_ref=False
    )
