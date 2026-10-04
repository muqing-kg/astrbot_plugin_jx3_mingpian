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
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.request import url2pathname

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core.access import (
    ADMIN_ONLY_MESSAGE,
    DENIED_MESSAGE,
    is_allowed,
    load_whitelist,
)
from .core.binding import FILENAME as BINDING_FILE
from .core.binding import BindingStore, parse_card_args
from .core.card import (
    brush_font_uri,
    build_card,
    build_closeup,
    card_font_uri,
    logo_uri,
    resolve_card,
)
from .core.image_api import ImageAPIClient, ImageAPIError, normalize_api_base
from .core.jx3api import JX3APIClient, JX3APIError
from .core.keypool import KeyPool
from .core.menu import CHOICE_TIMEOUT, ask_choice
from .core.mingpian_data import parse_card_index
from .core.prompts import Prompt, load_prompts, menu_text, task_hint
from .core.servers import canonical_server, server_list_text
from .core.sizes import card_size

PLUGIN_NAME = "astrbot_plugin_jx3_mingpian"
PLUGIN_VERSION = "1.2.0"
COMMAND = "名片卡"
CLOSEUP_COMMAND = "名片特写"
BIND_COMMAND = "名片绑定"
VIEW_BIND_COMMAND = "查看名片区服"
HELP_COMMAND = "名片帮助"
DEFAULT_CONCURRENT = 3
TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "templates" / "standalone" / "mingpian.html"
)
HELP_TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "templates" / "standalone" / "help.html"
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


@lru_cache(maxsize=1)
def load_help_template() -> str:
    """帮助图模板，同样读一次后缓存。"""
    return HELP_TEMPLATE_PATH.read_text(encoding="utf-8")


def help_payload() -> dict[str, Any]:
    """帮助图 payload。命令名一律取自命令常量，避免与实现脱节。"""
    return {
        "cardFont": card_font_uri(),
        "brushFont": brush_font_uri(),
        "logo": logo_uri(),
        "helpCommand": HELP_COMMAND,
        "generators": [
            {
                "cmd": COMMAND,
                "args": "服务器 角色名 [序号]",
                "desc": "取名片形象图，生成完整名片卡",
            },
            {
                "cmd": CLOSEUP_COMMAND,
                "args": "服务器 角色名 [序号]",
                "desc": "先选提示词，再生成对应的海报",
            },
        ],
        "admins": [
            {"cmd": BIND_COMMAND, "args": "区服名", "desc": "为当前会话绑定默认区服"},
            {"cmd": VIEW_BIND_COMMAND, "args": "", "desc": "查看当前会话绑定的区服"},
            {"cmd": HELP_COMMAND, "args": "", "desc": "返回这张命令说明图"},
        ],
        "examples": [
            f"{COMMAND} 飞龙在天 小螺卜头 1",
            f"{CLOSEUP_COMMAND} 飞龙在天 小螺卜头",
            f"{BIND_COMMAND} 飞龙在天",
        ],
        "notes": [
            "序号指名片序号：一个角色可以有多张名片，填 1 就取第 1 张；省略序号时随机抽一张",
            "已绑定区服的会话可以省略服务器参数",
            "名片卡每次消耗 2 次 JX3API 令牌；名片特写 1 次",
            "管理命令仅 AstrBot 管理员可用；白名单开启时另受白名单限制",
            "同时生成上限、名片卡比例与提示词清单均在插件配置里维护",
        ],
    }


def usage() -> str:
    return (
        f"用法：\n"
        f"  {COMMAND} 服务器 角色名       随机抽一张名片形象图\n"
        f"  {COMMAND} 服务器 角色名 序号   指定第几张（序号从 1 起）\n"
        f"  {COMMAND} 角色名              已绑定区服时可省略服务器\n"
        f"例如：{COMMAND} 飞龙在天 小螺卜头 1"
    )


def closeup_usage() -> str:
    return (
        f"用法：\n"
        f"  {CLOSEUP_COMMAND} 服务器 角色名       随机抽一张名片形象图\n"
        f"  {CLOSEUP_COMMAND} 服务器 角色名 序号   指定第几张（序号从 1 起）\n"
        f"  {CLOSEUP_COMMAND} 角色名              已绑定区服时可省略服务器\n"
        f"例如：{CLOSEUP_COMMAND} 飞龙在天 小螺卜头\n"
        f"执行后返回提示词清单，回复序号即可生成。"
    )


def _data_dir() -> Path:
    """插件数据目录。取不到 AstrBot 数据目录时退回插件目录下的 data。"""
    try:
        return Path(StarTools.get_data_dir(PLUGIN_NAME))
    except (AttributeError, ImportError, OSError, TypeError):
        path = Path(__file__).resolve().parent / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path


def _command_pattern(name: str) -> str:
    """命令匹配式。斜杠可选，命令后需为空白或结尾。"""
    return rf"^/?{re.escape(name)}(?:\s|$)"


def _split_args(event: AstrMessageEvent) -> list[str]:
    """取命令名之后的参数。"""
    text = (event.message_str or "").strip().removeprefix("/")
    return text.split()[1:]


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
        # 密钥池在请求之间共享，轮询位置才不会被重置
        self._token_pool = KeyPool(self._list("jx3api_token"))
        self._image_key_pool = KeyPool(self._list("image_api_key"))
        self.bindings = BindingStore(_data_dir() / BINDING_FILE)
        logger.info("%s 初始化完成（同时生成上限 %d）", PLUGIN_NAME, self._limit())

    # ---------- 配置 ----------

    def _text(self, key: str, default: str = "") -> str:
        return str(self.conf.get(key) or default).strip()

    def _list(self, key: str) -> list[str]:
        """读取列表型配置项，忽略空项。"""
        raw = self.conf.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [text for item in raw if (text := str(item or "").strip())]

    def _limit(self) -> int:
        """同时生成上限。取自 image_api_max_concurrent，默认 3，取值范围 1~20。"""
        raw = str(self.conf.get("image_api_max_concurrent") or "").strip()
        try:
            number = int(raw) if raw else DEFAULT_CONCURRENT
        except ValueError:
            number = DEFAULT_CONCURRENT
        return max(1, min(number, 20))

    def _jx3_client(self) -> JX3APIClient:
        # 按当前配置刷新密钥内容，游标保留
        self._token_pool.sync(self._list("jx3api_token"))
        return JX3APIClient(
            base_url=self._text("jx3api_base_url", "https://www.jx3api.com"),
            token=self._token_pool,
            proxy=self._text("proxy"),
            ssl_verify=bool(self.conf.get("jx3api_ssl_verify", True)),
        )

    def _image_client(self) -> ImageAPIClient:
        self._image_key_pool.sync(self._list("image_api_key"))
        try:
            base = normalize_api_base(self._text("image_api_base_url"))
        except ValueError:
            base = ""
        return ImageAPIClient(
            base_url=base,
            api_key=self._image_key_pool,
            model=self._text("image_api_model", "gpt-image-2.5"),
        )

    # ---------- 渲染 ----------

    async def _render(self, payload: dict, template: str | None = None) -> str:
        """将 payload 渲染为图片地址。template 省略时用名片卡模板。"""
        from astrbot.core import html_renderer

        return await html_renderer.render_custom_template(
            template or load_template(),
            payload,
            return_url=True,
            options=RENDER_OPTIONS,
        )

    # ---------- 白名单 ----------

    def _allowed(self, event: AstrMessageEvent) -> bool:
        """当前发送者或其所在会话是否在白名单内。白名单未启用时一律放行。"""
        return is_allowed(
            self.conf.get("whitelist_enabled"),
            load_whitelist(self.conf.get("whitelist")),
            event.get_sender_id(),
            getattr(event, "unified_msg_origin", ""),
        )

    # ---------- 发送 ----------

    async def _send(self, event: AstrMessageEvent, *comps) -> None:
        """直发消息链。不经 AstrBot 结果装饰，避免附加引用与 @。"""
        try:
            await event.send(MessageChain(chain=list(comps)))
        except Exception:  # noqa: BLE001
            logger.exception("消息发送失败")
        try:
            event.stop_event()
        except AttributeError:
            logger.debug("事件不支持 stop_event，跳过")

    async def _send_text(self, event: AstrMessageEvent, text: str) -> None:
        await self._send(event, Comp.Plain(text))

    async def _send_result(self, event: AstrMessageEvent, result) -> None:
        """发送由 event.image_result 构造的结果，发送后清理本地临时文件。"""
        await self._send(event, *(getattr(result, "chain", None) or []))

    @staticmethod
    def _cleanup(path: object) -> None:
        """删除渲染产生的本地临时文件。远程地址与 base64 直接跳过。"""
        text = str(path or "").strip()
        if not text or text.startswith(("http://", "https://", "base64://")):
            return
        local = Path(url2pathname(text.removeprefix("file://")))
        try:
            if local.is_file():
                local.unlink()
        except OSError:
            logger.warning("临时文件清理失败：%s", local)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """是否为 AstrBot 管理员。"""
        check = getattr(event, "is_admin", None)
        try:
            return bool(callable(check) and check())
        except (AttributeError, TypeError, ValueError):
            logger.debug("管理员判定失败，按非管理员处理")
            return False

    def _bound_server(self, event: AstrMessageEvent) -> str:
        return self.bindings.get(getattr(event, "unified_msg_origin", ""))

    # ---------- 命令 ----------

    @filter.regex(_command_pattern(BIND_COMMAND))
    async def bind_server(self, event: AstrMessageEvent):
        """名片绑定 区服名"""
        if not self._allowed(event):
            await self._send_text(event, DENIED_MESSAGE)
            return
        if not self._is_admin(event):
            await self._send_text(event, ADMIN_ONLY_MESSAGE)
            return

        args = _split_args(event)
        if not args:
            await self._send_text(
                event, f"用法：{BIND_COMMAND} 区服名\n{server_list_text()}"
            )
            return

        server = canonical_server(" ".join(args))
        if not server:
            await self._send_text(
                event,
                f"未识别的区服：{' '.join(args)}\n"
                f"请填写完整区服名。\n{server_list_text()}",
            )
            return

        umo = getattr(event, "unified_msg_origin", "")
        previous = self._bound_server(event)
        self.bindings.set(umo, server)
        if previous and previous != server:
            await self._send_text(event, f"已将区服由 {previous} 更换为 {server}")
        else:
            await self._send_text(event, f"已为当前会话绑定区服：{server}")

    @filter.regex(_command_pattern(VIEW_BIND_COMMAND))
    async def view_bind_server(self, event: AstrMessageEvent):
        """查看名片区服"""
        if not self._allowed(event):
            await self._send_text(event, DENIED_MESSAGE)
            return
        if not self._is_admin(event):
            await self._send_text(event, ADMIN_ONLY_MESSAGE)
            return

        current = self._bound_server(event)
        if current:
            await self._send_text(event, f"当前会话绑定的区服：{current}")
        else:
            await self._send_text(
                event, f"当前会话未绑定区服。发送「{BIND_COMMAND} 区服名」即可绑定。"
            )

    @filter.regex(_command_pattern(HELP_COMMAND))
    async def help_image(self, event: AstrMessageEvent):
        """名片帮助：返回一张命令说明图。"""
        if not self._allowed(event):
            await self._send_text(event, DENIED_MESSAGE)
            return

        try:
            url = await self._render(help_payload(), load_help_template())
        except Exception as exc:  # noqa: BLE001
            logger.exception("帮助图渲染失败")
            await self._send_text(event, f"渲染帮助图失败：{exc}")
            return

        await self._send_result(event, event.image_result(url))
        self._cleanup(url)

    @filter.regex(_command_pattern(COMMAND))
    async def mingpian_card(self, event: AstrMessageEvent):
        """名片卡 [服务器] 角色名 [序号]"""
        if not self._allowed(event):
            await self._send_text(event, DENIED_MESSAGE)
            return

        parsed = parse_card_args(_split_args(event), self._bound_server(event))
        if isinstance(parsed, str):
            await self._send_text(event, f"{parsed}\n\n{usage()}")
            return
        server, name, index = parsed

        picked: int | None = None
        if index:
            try:
                picked = parse_card_index(index)
            except ValueError:
                await self._send_text(
                    event, "序号只能是正整数，例如：名片卡 飞龙在天 小螺卜头 1"
                )
                return

        limit = self._limit()
        if self._active >= limit:
            await self._send_text(
                event,
                f"现在已有 {self._active} 张名片卡在生成中（同时最多 {limit} 张），"
                "请过几分钟再试。",
            )
            return

        # 生图耗时较长，先回执再执行
        await self._send_text(event, task_hint(self.conf.get("mingpian_task_hint")))

        self._active += 1
        try:
            payload, _note = await build_card(
                self._jx3_client(),
                self._image_client(),
                server=server,
                name=name,
                index=picked,
                size=card_size(self.conf.get("mingpian_card_ratio")),
            )
        except (JX3APIError, ImageAPIError) as exc:
            await self._send_text(event, f"名片卡生成失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("名片卡生成异常")
            await self._send_text(event, f"名片卡生成失败：{exc}")
            return
        finally:
            self._active -= 1

        try:
            url = await self._render(payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("名片卡渲染失败")
            await self._send_text(event, f"渲染图片失败：{exc}")
            return

        await self._send_result(event, event.image_result(url))
        self._cleanup(url)

    @filter.regex(_command_pattern(CLOSEUP_COMMAND))
    async def mingpian_closeup(self, event: AstrMessageEvent):
        """名片特写 [服务器] 角色名 [名片序号]"""
        await self._mingpian_closeup(event)

    async def _mingpian_closeup(self, event: AstrMessageEvent):
        """名片特写：先定名片，再选提示词，最后生成图片。"""
        if not self._allowed(event):
            await self._send_text(event, DENIED_MESSAGE)
            return

        parsed = parse_card_args(_split_args(event), self._bound_server(event))
        if isinstance(parsed, str):
            await self._send_text(event, f"{parsed}\n\n{closeup_usage()}")
            return
        server, name, index = parsed

        picked: int | None = None
        if index:
            try:
                picked = parse_card_index(index)
            except ValueError:
                await self._send_text(
                    event, "序号只能是正整数，例如：名片特写 飞龙在天 小螺卜头 1"
                )
                return

        prompts = load_prompts(self.conf.get("mingpian_prompts"))
        if not prompts:
            await self._send_text(event, "未配置提示词")
            return

        limit = self._limit()
        if self._active >= limit:
            await self._send_text(
                event,
                f"现在已有 {self._active} 个任务在生成中（同时最多 {limit} 个），"
                "请过几分钟再试。",
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
            await self._send_text(
                event,
                f"现在已有 {self._active} 个任务在生成中（同时最多 {limit} 个），"
                "请过几分钟再试。",
            )
            return

        await self._send_text(event, task_hint(self.conf.get("mingpian_task_hint")))

        self._active += 1
        try:
            source = await resolve_card(
                self._jx3_client(),
                server=server,
                name=name,
                index=index,
                with_detail=False,
            )
            image_bytes = await build_closeup(self._image_client(), source, prompt)
        except (JX3APIError, ImageAPIError) as exc:
            await self._send_text(event, f"名片特写生成失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("名片特写生成异常")
            await self._send_text(event, f"名片特写生成失败：{exc}")
            return
        finally:
            self._active -= 1

        await self._send(
            event,
            Comp.Image.fromBase64(base64.b64encode(image_bytes).decode("ascii")),
        )

    async def terminate(self):
        logger.info("%s 已卸载", PLUGIN_NAME)
