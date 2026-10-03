"""白名单：控制哪些用户或会话可以使用本插件命令。

名单项支持两种写法：

    用户 ID                 如 123456789
                            匹配发送者，仅该用户可用

    统一消息来源（UMO）      如 aiocqhttp:GroupMessage:123456789
                            匹配整个会话，该会话内的用户都可用

UMO 格式为 ``平台适配器ID:消息类型:会话ID``，其中消息类型为
``GroupMessage``、``FriendMessage`` 或 ``OtherMessage``。含冒号的项按 UMO 处理，
其余按用户 ID 处理，两项均为完全匹配。
"""

from __future__ import annotations

DENIED_MESSAGE = "本命令仅限白名单用户使用。"


def load_whitelist(raw: object) -> set[str]:
    """把配置中的白名单规整为去重后的集合，忽略空项。"""
    if not isinstance(raw, list):
        return set()
    return {text for item in raw if (text := str(item or "").strip())}


def split_whitelist(whitelist: set[str]) -> tuple[set[str], set[str]]:
    """按写法拆成 (用户 ID, UMO) 两组。含冒号的项按 UMO 处理。"""
    users = {item for item in whitelist if ":" not in item}
    origins = {item for item in whitelist if ":" in item}
    return users, origins


def is_allowed(
    enabled: object,
    whitelist: set[str],
    sender_id: object = "",
    umo: object = "",
) -> bool:
    """白名单未启用时一律放行；启用时发送者或其所在会话命中名单才放行。"""
    if not bool(enabled):
        return True
    users, origins = split_whitelist(whitelist)
    sender = str(sender_id or "").strip()
    origin = str(umo or "").strip()
    return (bool(sender) and sender in users) or (bool(origin) and origin in origins)
