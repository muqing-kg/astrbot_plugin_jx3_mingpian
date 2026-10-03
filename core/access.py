"""白名单：控制哪些用户可以使用本插件命令。"""

from __future__ import annotations

DENIED_MESSAGE = "本命令仅限白名单用户使用。"


def load_whitelist(raw: object) -> set[str]:
    """把配置中的白名单规整为去重后的 ID 集合，忽略空项。"""
    if not isinstance(raw, list):
        return set()
    return {text for item in raw if (text := str(item or "").strip())}


def is_allowed(enabled: object, whitelist: set[str], sender_id: object) -> bool:
    """白名单未启用时一律放行；启用时仅名单内的用户放行。"""
    if not bool(enabled):
        return True
    return str(sender_id or "").strip() in whitelist
