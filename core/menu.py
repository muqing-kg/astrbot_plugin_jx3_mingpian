"""序号选择菜单：发送清单，等待发送人回复序号，超时执行第 1 项。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.utils.session_waiter import SessionController, session_waiter

CHOICE_TIMEOUT = 15

RunChoice = Callable[[int, AstrMessageEvent], Awaitable[None]]


async def ask_choice(
    event: AstrMessageEvent,
    text: str,
    count: int,
    run: RunChoice,
    *,
    timeout: int = CHOICE_TIMEOUT,
) -> None:
    """发送清单 text，等待发送人回复 1..count 的序号。

    只接受原发送人的回复；超时按第 1 项执行。
    """
    body = str(text).rstrip()
    if "发送序号即可" not in body:
        body += f"\n\n发送序号即可，{timeout} 秒后自动选 1"

    # 直发消息链：不经 AstrBot 结果装饰，避免附加引用与 @
    await event.send(MessageChain(chain=[Plain(body)]))
    event.stop_event()

    sender = event.get_sender_id()
    resolved = False

    async def fail(target: AstrMessageEvent, message: str) -> None:
        await target.send(MessageChain(chain=[Plain(message)]))
        target.stop_event()

    @session_waiter(timeout=timeout)
    async def waiter(controller: SessionController, new_event: AstrMessageEvent):
        nonlocal resolved
        if new_event.get_sender_id() != sender:
            return

        raw = new_event.get_message_str().strip()
        if raw.startswith("/"):
            raw = raw[1:].strip()
        if not raw.isdigit():
            await fail(new_event, "输入异常，结束会话")
            controller.stop()
            return

        choice = int(raw)
        if choice < 1 or choice > count:
            await fail(new_event, "无效序号，结束会话")
            controller.stop()
            return

        resolved = True
        try:
            await run(choice, new_event)
        except Exception:  # noqa: BLE001
            logger.exception("选择后执行失败")
        controller.stop()

    try:
        await waiter(event)
    except TimeoutError:
        if resolved:
            return
        try:
            await run(1, event)
        except Exception:  # noqa: BLE001
            logger.exception("默认选项执行失败")
    except Exception:  # noqa: BLE001
        logger.exception("选择等待异常")
        try:
            await fail(event, "选择等待异常，请重新发送命令")
        except Exception:  # noqa: BLE001
            logger.exception("异常提示发送失败")
