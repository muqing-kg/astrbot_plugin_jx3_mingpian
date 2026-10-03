"""剑网3 名片卡插件。

命令：
    名片卡   服务器 角色名 [序号]
    名片特写 服务器 角色名 [序号]

处理流程：
    取 /card/records 中的名片形象图
    → 生成无字底卡与正面头像（并发）
    → 叠加文字、门派徽记、门派诗与条码后输出

「名片特写」在生成前先由用户从配置的提示词中选择一条，输出不叠加文字。

网络配置相互独立：JX3API 请求经 HTTP 代理，生图请求直连。
"""

from __future__ import annotations

import base64
import logging
from functools import lru_cache
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.access import DENIED_MESSAGE, is_allowed, load_whitelist
from .core.card import build_card, build_closeup, resolve_card
from .core.image_api import ImageAPIClient, ImageAPIError, normalize_api_base
from .core.jx3api import JX3APIClient, JX3APIError
from .core.menu import CHOICE_TIMEOUT, ask_choice
from .core.mingpian_data import parse_card_index
from .core.prompts import Prompt, load_prompts, menu_text, task_hint

logger = logging.getLogger("astrbot")

PLUGIN_NAME = "astrbot_plugin_jx3_mingpian"
PLUGIN_VERSION = "1.0.0"
COMMAND = "名片卡"
CLOSEUP_COMMAND = "名片特写"
DEFAULT_CONCURRENT = 3
TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "templates" / "standalone" / "mingpian.html"
)

RENDER_OPTIONS = {
    "quality": 100,
    "device_scale_factor_level": "normal",
    "full_page": True,
    "omit_background": False,
    "type": "png",
}


@lru_cache(maxsize=1)
def load_template() -> str:
    """模板为自包含单文件，读取一次后缓存。"""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def usage() -> str:
    return (
        f"用法：\n"
        f"  {COMMAND} 服务器 角色名       随机抽一张名片形象图\n"
        f"  {COMMAND} 服务器 角色名 序号   指定第几张（序号从 1 起）\n"
        f"例如：{COMMAND} 飞龙在天 小螺卜头 1"
    )


def closeup_usage() -> str:
    return (
        f"用法：\n"
        f"  {CLOSEUP_COMMAND} 服务器 角色名       随机抽一张名片形象图\n"
        f"  {CLOSEUP_COMMAND} 服务器 角色名 序号   指定第几张（序号从 1 起）\n"
        f"例如：{CLOSEUP_COMMAND} 飞龙在天 小螺卜头\n"
        f"执行后返回提示词清单，回复序号即可生成。"
    )


@register(
    PLUGIN_NAME,
    "muqing",
    "剑网3 名片卡：取角色名片形象图，生成底卡后叠加文字与门派元素。",
    PLUGIN_VERSION,
    "",
)
class JX3MingpianPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        # 当前正在生成的任务数
        self._active = 0
        logger.info("%s 初始化完成（同时生成上限 %d）", PLUGIN_NAME, self._limit())

    # ---------- 配置 ----------

    def _text(self, key: str, default: str = "") -> str:
        return str(self.conf.get(key) or default).strip()

    def _limit(self) -> int:
        """同时生成上限。取自 image_api_max_concurrent，默认 3，取值范围 1~20。"""
        raw = str(self.conf.get("image_api_max_concurrent") or "").strip()
        try:
            number = int(raw) if raw else DEFAULT_CONCURRENT
        except ValueError:
            number = DEFAULT_CONCURRENT
        return max(1, min(number, 20))

    def _jx3_client(self) -> JX3APIClient:
        return JX3APIClient(
            base_url=self._text("jx3api_base_url", "https://www.jx3api.com"),
            token=self._text("jx3api_token"),
            proxy=self._text("proxy"),
            ssl_verify=bool(self.conf.get("jx3api_ssl_verify", True)),
        )

    def _image_client(self) -> ImageAPIClient:
        try:
            base = normalize_api_base(self._text("image_api_base_url"))
        except ValueError:
            base = ""
        return ImageAPIClient(
            base_url=base,
            api_key=self._text("image_api_key"),
            model=self._text("image_api_model", "gpt-image-2.5"),
        )

    # ---------- 渲染 ----------

    async def _render(self, payload: dict) -> str:
        """将 payload 渲染为图片地址。"""
        from astrbot.core import html_renderer

        return await html_renderer.render_custom_template(
            load_template(), payload, return_url=True, options=RENDER_OPTIONS
        )

    # ---------- 白名单 ----------

    def _allowed(self, event: AstrMessageEvent) -> bool:
        """当前发送者是否在白名单内。白名单未启用时一律放行。"""
        return is_allowed(
            self.conf.get("whitelist_enabled"),
            load_whitelist(self.conf.get("whitelist")),
            event.get_sender_id(),
        )

    # ---------- 命令 ----------

    @filter.command(COMMAND)
    async def mingpian_card(
        self,
        event: AstrMessageEvent,
        server: str = "",
        name: str = "",
        index: str = "",
    ):
        """名片卡 服务器 角色名 [序号]"""
        if not self._allowed(event):
            yield event.plain_result(DENIED_MESSAGE)
            return

        server, name, index = server.strip(), name.strip(), index.strip()
        if not server or not name:
            yield event.plain_result(usage())
            return

        picked: int | None = None
        if index:
            try:
                picked = parse_card_index(index)
            except ValueError:
                yield event.plain_result(
                    "序号只能是正整数，例如：名片卡 飞龙在天 小螺卜头 1"
                )
                return

        limit = self._limit()
        if self._active >= limit:
            yield event.plain_result(
                f"现在已有 {self._active} 张名片卡在生成中（同时最多 {limit} 张），"
                "请过几分钟再试。"
            )
            return

        # 生图耗时较长，先回执再执行
        yield event.plain_result(task_hint(self.conf.get("mingpian_task_hint")))

        self._active += 1
        try:
            payload, note = await build_card(
                self._jx3_client(),
                self._image_client(),
                server=server,
                name=name,
                index=picked,
            )
        except (JX3APIError, ImageAPIError) as exc:
            yield event.plain_result(f"名片卡生成失败：{exc}")
            return
        except Exception as exc:
            logger.exception("名片卡生成异常")
            yield event.plain_result(f"名片卡生成失败：{exc}")
            return
        finally:
            self._active -= 1

        try:
            url = await self._render(payload)
        except Exception as exc:
            logger.exception("名片卡渲染失败")
            yield event.plain_result(f"渲染图片失败：{exc}")
            return

        yield event.plain_result(note)
        yield event.image_result(url)

    @filter.command(CLOSEUP_COMMAND)
    async def mingpian_closeup(
        self,
        event: AstrMessageEvent,
        server: str = "",
        name: str = "",
        index: str = "",
    ):
        """名片特写 服务器 角色名 [名片序号]"""
        await self._mingpian_closeup(event, server, name, index)

    async def _mingpian_closeup(
        self,
        event: AstrMessageEvent,
        server: str,
        name: str,
        index: str,
    ):
        """名片特写：先定名片，再选提示词，最后生成图片。"""
        if not self._allowed(event):
            await event.send(event.plain_result(DENIED_MESSAGE))
            return

        server, name, index = server.strip(), name.strip(), index.strip()
        if not server or not name:
            await event.send(event.plain_result(closeup_usage()))
            return

        picked: int | None = None
        if index:
            try:
                picked = parse_card_index(index)
            except ValueError:
                await event.send(
                    event.plain_result(
                        "序号只能是正整数，例如：名片特写 飞龙在天 小螺卜头 1"
                    )
                )
                return

        prompts = load_prompts(self.conf.get("mingpian_prompts"))
        if not prompts:
            await event.send(event.plain_result("未配置提示词"))
            return

        limit = self._limit()
        if self._active >= limit:
            await event.send(
                event.plain_result(
                    f"现在已有 {self._active} 个任务在生成中（同时最多 {limit} 个），"
                    "请过几分钟再试。"
                )
            )
            return

        async def run(choice: int, target: AstrMessageEvent) -> None:
            await self._run_closeup(target, server, name, picked, prompts[choice - 1])

        await ask_choice(
            event, menu_text(prompts), len(prompts), run, timeout=CHOICE_TIMEOUT
        )

    async def _run_closeup(
        self,
        event: AstrMessageEvent,
        server: str,
        name: str,
        index: int | None,
        prompt: Prompt,
    ) -> None:
        """按选定的提示词生成名片特写。"""
        # 菜单等待期间名额可能已被占满，此处再确认一次
        limit = self._limit()
        if self._active >= limit:
            await event.send(
                event.plain_result(
                    f"现在已有 {self._active} 个任务在生成中（同时最多 {limit} 个），"
                    "请过几分钟再试。"
                )
            )
            return

        await event.send(
            event.plain_result(task_hint(self.conf.get("mingpian_task_hint")))
        )

        self._active += 1
        try:
            source = await resolve_card(
                self._jx3_client(), server=server, name=name, index=index
            )
            image_bytes = await build_closeup(self._image_client(), source, prompt.text)
        except (JX3APIError, ImageAPIError) as exc:
            await event.send(event.plain_result(f"名片特写生成失败：{exc}"))
            return
        except Exception as exc:
            logger.exception("名片特写生成异常")
            await event.send(event.plain_result(f"名片特写生成失败：{exc}"))
            return
        finally:
            self._active -= 1

        await event.send(event.plain_result(f"{prompt.name} · {source.note}"))
        await event.send(
            event.chain_result(
                [Comp.Image.fromBase64(base64.b64encode(image_bytes).decode("ascii"))]
            )
        )

    async def terminate(self):
        logger.info("%s 已卸载", PLUGIN_NAME)
