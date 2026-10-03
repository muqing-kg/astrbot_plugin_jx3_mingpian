"""区服名表与归一。

绑定与查询都要求完整区服名。常见简称会归一到正式名，
正式名之外的输入一律拒绝。
"""

from __future__ import annotations

OFFICIAL_SERVERS: tuple[str, ...] = (
    "飞龙在天",
    "天鹅坪",
    "破阵子",
    "眉间雪",
    "山海相逢",
    "蝶恋花",
    "剑胆琴心",
    "斗转星移",
    "乾坤一掷",
    "长安城",
    "龙争虎斗",
    "唯我独尊",
    "梦江南",
    "绝代天骄",
    "幽月轮",
)

# 常见简称。键为正式区服名
ALIASES: dict[str, tuple[str, ...]] = {
    "破阵子": ("念破",),
    "蝶恋花": ("蝶服",),
    "斗转星移": ("姨妈",),
    "乾坤一掷": ("华乾",),
    "龙争虎斗": ("龙虎",),
    "唯我独尊": ("唯满侠",),
    "梦江南": ("双梦",),
}

_ALIAS_TO_SERVER = {
    alias: server for server, aliases in ALIASES.items() for alias in aliases
}


def canonical_server(name: object) -> str:
    """把区服名归一为正式名。无法识别时返回空串。"""
    text = str(name or "").strip()
    if not text:
        return ""
    if text in OFFICIAL_SERVERS:
        return text
    return _ALIAS_TO_SERVER.get(text, "")


def server_list_text() -> str:
    """正式区服名，用于提示。"""
    return "、".join(OFFICIAL_SERVERS)
