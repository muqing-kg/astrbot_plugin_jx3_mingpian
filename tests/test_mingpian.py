"""名片卡独立插件：单元测试（不发网络请求）。"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest
from aiohttp import FormData
from astrbot.api.message_components import Image as _Image
from astrbot.api.message_components import Plain as _Plain

from core.access import DENIED_MESSAGE, is_allowed, load_whitelist
from core.binding import BindingStore, parse_card_args
from core.card import build_payload
from core.image_api import (
    DEFAULT_MODEL,
    DEFAULT_POSTER_SIZE,
    ImageAPIClient,
    ImageAPIError,
    normalize_api_base,
)
from core.jx3api import JX3APIClient, JX3APIError
from core.keypool import KeyPool
from core.mingpian_data import (
    BASE_PROMPT,
    SECT_POEMS,
    SECT_SCENE,
    parse_card_index,
    sect_accent,
    sect_card_fields,
    sect_emblem,
    sect_poem,
)
from core.prompts import (
    DEFAULT_TASK_HINT,
    Prompt,
    load_prompts,
    menu_text,
    render_prompt,
    task_hint,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "standalone" / "mingpian.html"
# 生图接口需要一张真实存在的参考图，内容无所谓
REF_IMAGE = ROOT / "templates" / "img" / "剑网3标识.png"

# 插件用相对导入（from .core.card import ...），必须按「包」加载；
# 直接 import main 会报 "attempted relative import with no known parent package"。
_PKG = "astrbot_plugin_jx3_mingpian"


def _load_plugin_main():
    if f"{_PKG}.main" in sys.modules:
        return sys.modules[f"{_PKG}.main"]
    package = types.ModuleType(_PKG)
    package.__path__ = [str(ROOT)]
    sys.modules[_PKG] = package
    spec = importlib.util.spec_from_file_location(f"{_PKG}.main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG}.main"] = module
    spec.loader.exec_module(module)
    return module


plugin_main = _load_plugin_main()


class TestNormalizeApiBase:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("https://hub.example.com/v1", "https://hub.example.com/v1"),
            ("https://hub.example.com", "https://hub.example.com/v1"),
            ("https://hub.example.com/", "https://hub.example.com/v1"),
            ("  https://hub.example.com/v1  ", "https://hub.example.com/v1"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_自动补_v1(self, raw, expected):
        assert normalize_api_base(raw) == expected

    @pytest.mark.parametrize("raw", ["hub.example.com", "ftp://x/v1"])
    def test_非法地址报错(self, raw):
        with pytest.raises(ValueError):
            normalize_api_base(raw)


class TestImageClient:
    def test_接口自动拼接(self):
        client = ImageAPIClient("https://hub.example.com", "k", "gpt-image-2.5")
        assert client.endpoint("edits") == "https://hub.example.com/v1/images/edits"

    def test_没配全就不算可用(self):
        assert not ImageAPIClient("", "k", "m").configured
        assert not ImageAPIClient("https://h/v1", "", "m").configured
        assert ImageAPIClient("https://h/v1", "k", "m").configured

    def test_模型名缺省(self):
        assert ImageAPIClient("https://h/v1", "k", "").model == DEFAULT_MODEL

    def _client(self):
        return ImageAPIClient("https://h/v1", "k", "m")

    async def test_取_b64_图片(self):
        blob = b"\x89PNG-fake"
        payload = {"data": [{"b64_json": base64.b64encode(blob).decode()}]}
        assert await self._client()._extract_image(payload) == blob

    async def test_取_data_uri_图片(self):
        blob = b"\x89PNG-fake"
        encoded = base64.b64encode(blob).decode()
        payload = {"data": [{"b64_json": f"data:image/png;base64,{encoded}"}]}
        assert await self._client()._extract_image(payload) == blob

    async def test_下载_url_图片(self, monkeypatch):
        blob = b"\x89PNG-from-url"
        client = self._client()

        async def fake_fetch(self, url):
            return blob

        monkeypatch.setattr(ImageAPIClient, "_fetch_image", fake_fetch)
        payload = {"data": [{"url": "https://x/a.png"}]}
        assert await client._extract_image(payload) == blob

    @pytest.mark.parametrize("payload", [{}, {"data": []}, {"data": [{"nothing": 1}]}])
    async def test_没有可用图片就报错(self, payload):
        with pytest.raises(ImageAPIError):
            await self._client()._extract_image(payload)

    @pytest.mark.parametrize(
        "status, keyword",
        [
            (401, "密钥被拒绝"),
            (404, "地址或模型名"),
            (429, "限流"),
            (500, "生图接口报错"),
        ],
    )
    def test_错误提示可读(self, status, keyword):
        assert keyword in ImageAPIClient._explain_http(
            status, '{"error":{"message":"boom"}}'
        )

    @pytest.mark.parametrize(
        "message, stable",
        [
            ("生图密钥被拒绝（HTTP 401）：bad", True),
            ("生图接口地址或模型名不对（HTTP 404）：nf", True),
            ("生图超时（300 秒），请稍后再试", False),
            ("生图接口限流或额度不足（HTTP 429）：slow", False),
        ],
    )
    def test_配置类错误不重试(self, message, stable):
        assert ImageAPIClient._is_stable_failure(message) is stable


class TestApiContract:
    """README 中接口与令牌消耗的说明必须与代码一致。"""

    def test_接口路径(self):
        from core.jx3api import PATH_CARD_RECORDS, PATH_ROLE_DETAIL

        assert PATH_CARD_RECORDS == "/card/records"
        assert PATH_ROLE_DETAIL == "/role/detail"

    def test_两条接口请求都带令牌(self):
        source = (ROOT / "core" / "jx3api.py").read_text(encoding="utf-8")
        assert 'query = {**params, "token": token}' in source

    def test_图片下载不带令牌(self):
        source = (ROOT / "core" / "jx3api.py").read_text(encoding="utf-8")
        body = source.split("async def get_bytes", 1)[1]
        assert "self._get(" not in body
        assert '"token"' not in body

    def test_README_与代码一致(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "/card/records" in readme
        assert "/role/detail" in readme
        assert "2 次" in readme
        assert "docs/sample.jpg" in readme

    def test_示例图片存在(self):
        assert (ROOT / "docs" / "sample.jpg").is_file()


class TestProxySeparation:
    def test_jx3_带代理(self):
        assert (
            JX3APIClient(
                "https://www.jx3api.com", "t", proxy="http://127.0.0.1:7890"
            ).proxy
            == "http://127.0.0.1:7890"
        )

    def test_生图客户端没有代理参数(self):
        import inspect

        params = inspect.signature(ImageAPIClient.__init__).parameters
        assert "proxy" not in params, "生图客户端不该有代理参数"

    def test_生图请求不带_proxy(self):
        source = (ROOT / "core" / "image_api.py").read_text(encoding="utf-8")
        assert "proxy=" not in source, "生图请求里不该出现 proxy="


class TestCardIndex:
    @pytest.mark.parametrize("raw, expected", [("1", 1), ("3", 3), (" 2 ", 2), (5, 5)])
    def test_合法序号(self, raw, expected):
        assert parse_card_index(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "0", "-1", "abc", "1.5"])
    def test_非法序号报错(self, raw):
        with pytest.raises(ValueError):
            parse_card_index(raw)


class TestSectData:
    def test_有诗的门派都是四句(self):
        for school, lines in SECT_POEMS.items():
            assert len(lines) == 4, school

    def test_每个有诗的门派都有场景描述(self):
        for school in SECT_POEMS:
            assert SECT_SCENE.get(school), f"{school} 缺场景描述"

    def test_门派徽记都在(self):
        for school in SECT_POEMS:
            assert sect_emblem(school) is not None, f"{school} 缺徽记图标"

    def test_别名归一(self):
        assert sect_poem("凌雪阁") == sect_poem("凌雪")
        assert sect_poem("衍天宗") == sect_poem("衍天")
        assert sect_poem("北天药宗") == sect_poem("药宗")

    def test_取色合法且有区分度(self):
        accents = {s: sect_accent(s)[0] for s in SECT_POEMS}
        assert all(a.startswith("#") and len(a) == 7 for a in accents.values())
        assert len(set(accents.values())) >= len(accents) - 2

    def test_未收录门派不炸(self):
        fields = sect_card_fields("不存在的门派")
        assert fields["poem"] == []
        assert fields["accent"].startswith("#")

    def test_提示词占位符齐全(self):
        rendered = BASE_PROMPT.format(
            school="唐门", scene=SECT_SCENE["唐门"], accent="#123456"
        )
        assert "唐门" in rendered and "#123456" in rendered


class TestPayload:
    def _payload(self, school="唐门"):
        return build_payload(
            base_bytes=b"base",
            avatar_bytes=b"avatar",
            school=school,
            nickname="小螺卜头",
            server="飞龙在天",
            body="萝莉",
            camp="恶人谷",
            tong="英雄长乐坊",
        )

    def test_图片走_data_uri(self):
        payload = self._payload()
        assert payload["bgImage"].startswith("data:image/png;base64,")
        assert payload["avatar"].startswith("data:image/png;base64,")
        assert payload["overlayMode"] is True

    def test_门派字段齐全(self):
        payload = self._payload()
        assert payload["school"] == "唐门"
        assert payload["accent"].startswith("#")
        assert payload["schoolIcon"].startswith("data:image/png;base64,")
        assert len(payload["poem"]) == 4

    def test_模板用到的变量都在_payload_里(self):
        """模板引用的变量必须由 payload 提供，缺失时渲染为空白。"""
        template = TEMPLATE.read_text(encoding="utf-8")
        # {% set x = ... %} 和 {% for x in ... %} 定义的是模板内部变量，不算 payload 的
        internal = set(re.findall(r"\{%-?\s*set\s+([a-zA-Z_][a-zA-Z0-9_]*)", template))
        internal |= set(
            re.findall(r"\{%-?\s*for\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+in", template)
        )

        needed = set()
        for block in re.findall(r"\{\{(.*?)\}\}", template, re.DOTALL):
            if "| default" in block or "|default" in block:
                continue
            match = re.match(r"\s*([a-zA-Z_][a-zA-Z0-9_]*)", block)
            if match:
                needed.add(match.group(1))
        missing = sorted(needed - set(self._payload()) - internal)
        assert not missing, f"模板要这些变量，payload 没给：{missing}"

    def test_字体与标识都嵌进去了(self):
        payload = self._payload()
        assert payload["cardFont"].startswith("data:font/woff2;base64,")
        assert payload["brushFont"].startswith("data:font/ttf;base64,")
        assert payload["logo"].startswith("data:image/png;base64,")


class _FakeEvent:
    """假事件。send 收下的都是 MessageChain，按组件类型取出文本与图片。"""

    def __init__(self, sender: str = "u1", msg: str = "", umo: str = ""):
        self.sent: list[list] = []
        self._sender = sender
        self._msg = msg
        self.unified_msg_origin = umo
        self.stopped = False

    def plain_result(self, text):
        return types.SimpleNamespace(chain=[_Plain(text)])

    def image_result(self, url):
        return types.SimpleNamespace(chain=[_Image(url)])

    async def send(self, result):
        self.sent.append(list(getattr(result, "chain", None) or []))

    def get_sender_id(self):
        return self._sender

    def get_message_str(self):
        return self._msg

    @property
    def message_str(self):
        return self._msg

    def stop_event(self):
        self.stopped = True

    @property
    def texts(self) -> list[str]:
        return [
            comp.text
            for chain in self.sent
            for comp in chain
            if getattr(comp, "text", None) is not None
        ]

    @property
    def images(self) -> list[str]:
        return [
            comp.file
            for chain in self.sent
            for comp in chain
            if getattr(comp, "file", None)
        ]


def _stub_waiter(reply: str | None, sender: str = "u1"):
    """把 session_waiter 换成一个立即返回的版本：reply 为 None 表示超时。"""

    def factory(*, timeout):
        def decorator(func):
            async def wrapper(event):
                if reply is None:
                    raise TimeoutError
                controller = types.SimpleNamespace(stop=lambda: None)
                await func(controller, _FakeEvent(sender=sender, msg=reply))

            return wrapper

        return decorator

    return factory


async def _collect(agen):
    return [item async for item in agen]


class TestPrompts:
    def test_按顺序连续编号(self):
        prompts = load_prompts(
            [
                {"name": "甲", "prompt": "内容甲"},
                {"name": "乙", "prompt": "内容乙"},
                {"name": "丙", "prompt": "内容丙"},
            ]
        )
        assert [p.index for p in prompts] == [1, 2, 3]
        assert [p.name for p in prompts] == ["甲", "乙", "丙"]

    def test_跳过空内容并重新编号(self):
        prompts = load_prompts(
            [
                {"name": "甲", "prompt": "内容甲"},
                {"name": "空", "prompt": "   "},
                {"name": "丙", "prompt": "内容丙"},
                {"name": "缺字段"},
            ]
        )
        assert [p.index for p in prompts] == [1, 2]
        assert [p.name for p in prompts] == ["甲", "丙"]

    def test_名称留空时用默认名(self):
        prompts = load_prompts([{"name": "", "prompt": "内容"}])
        assert prompts[0].name == "提示词1"

    @pytest.mark.parametrize("raw", [None, "", {}, 123, [1, 2], ["字符串"]])
    def test_配置异常返回空清单(self, raw):
        assert load_prompts(raw) == []

    def test_清单文本(self):
        prompts = load_prompts(
            [{"name": "甲", "prompt": "x"}, {"name": "乙", "prompt": "y"}]
        )
        text = menu_text(prompts, timeout=15)
        assert "名片特写" in text
        assert "1. 甲" in text
        assert "2. 乙" in text
        assert "发送序号即可，15 秒后自动选 1" in text

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("", DEFAULT_TASK_HINT),
            (None, DEFAULT_TASK_HINT),
            ("  ", DEFAULT_TASK_HINT),
            ("自定义", "自定义"),
        ],
    )
    def test_等待提示回退默认(self, raw, expected):
        assert task_hint(raw) == expected

    def test_替换占位符(self):
        out = render_prompt(
            "门派{school} 场景{scene} 色{accent}",
            school="唐门",
            scene="蜀中",
            accent="#123456",
        )
        assert out == "门派唐门 场景蜀中 色#123456"

    def test_无占位符按原文(self):
        text = "采用21:9超宽横向角色海报：超大双眼极近特写铺满背景"
        assert (
            render_prompt(text, school="唐门", scene="蜀中", accent="#123456") == text
        )

    def test_含未知花括号按原文(self):
        text = "带 {未知} 的提示词"
        assert (
            render_prompt(text, school="唐门", scene="蜀中", accent="#123456") == text
        )


class TestReferenceImages:
    """名片特写只传角色图；名片卡额外传卡面样式参考图。"""

    def _source(self):
        from core.card import CardSource

        return CardSource(
            server="飞龙在天",
            nickname="小螺卜头",
            school="唐门",
            body="萝莉",
            camp="恶人谷",
            tong="英雄长乐坊",
            art=b"\x89PNG-art",
            card_no=1,
            card_total=4,
            accent="#123456",
            scene="蜀中",
        )

    async def _run(self, image, **kwargs):
        from core.card import generate_base

        return await generate_base(image, self._source(), "提示词", **kwargs)

    async def test_默认带卡面参考图(self):
        seen = {}

        class FakeImage:
            async def edit(self, prompt, images, **kwargs):
                seen["images"] = [Path(p).name for p in images]
                return b"ok"

        await self._run(FakeImage())
        assert len(seen["images"]) == 2
        assert seen["images"][0] == "卡面参考.png"
        assert seen["images"][1] == "art.png"

    async def test_关闭时只传角色图(self):
        seen = {}

        class FakeImage:
            async def edit(self, prompt, images, **kwargs):
                seen["images"] = [Path(p).name for p in images]
                return b"ok"

        await self._run(FakeImage(), with_card_ref=False)
        assert seen["images"] == ["art.png"]

    async def test_特写走关闭分支(self, monkeypatch):
        from core import card as card_mod

        seen = {}

        async def fake_generate_base(image, source, prompt, **kwargs):
            seen.update(kwargs)
            return b"ok"

        monkeypatch.setattr(card_mod, "generate_base", fake_generate_base)

        class FakeImage:
            configured = True

        prompt = Prompt(index=1, name="甲", text="提示词")
        await card_mod.build_closeup(FakeImage(), self._source(), prompt)
        assert seen.get("with_card_ref") is False


class TestAskChoice:
    async def test_超时执行第一项(self, monkeypatch):
        from core import menu

        seen: list[int] = []

        async def run(choice, event):
            seen.append(choice)

        monkeypatch.setattr(menu, "session_waiter", _stub_waiter(None))
        event = _FakeEvent()
        await menu.ask_choice(event, "名片特写\n1. 甲\n2. 乙", 2, run)

        assert seen == [1]
        assert "1. 甲" in event.texts[0]
        assert "发送序号即可" in event.texts[0]

    async def test_回复序号执行对应项(self, monkeypatch):
        from core import menu

        seen: list[int] = []

        async def run(choice, event):
            seen.append(choice)

        monkeypatch.setattr(menu, "session_waiter", _stub_waiter("2"))
        await menu.ask_choice(_FakeEvent(), "清单", 3, run)
        assert seen == [2]

    async def test_带斜杠的序号也能识别(self, monkeypatch):
        from core import menu

        seen: list[int] = []

        async def run(choice, event):
            seen.append(choice)

        monkeypatch.setattr(menu, "session_waiter", _stub_waiter("/3"))
        await menu.ask_choice(_FakeEvent(), "清单", 3, run)
        assert seen == [3]

    async def test_非数字结束会话(self, monkeypatch):
        from core import menu

        seen: list[int] = []
        replies: list[str] = []

        async def run(choice, event):
            seen.append(choice)

        def factory(*, timeout):
            def decorator(func):
                async def wrapper(event):
                    reply = _FakeEvent(msg="abc")
                    await func(types.SimpleNamespace(stop=lambda: None), reply)
                    replies.extend(reply.texts)

                return wrapper

            return decorator

        monkeypatch.setattr(menu, "session_waiter", factory)
        await menu.ask_choice(_FakeEvent(), "清单", 3, run)
        assert seen == []
        assert replies and "输入异常" in replies[0]

    async def test_超范围结束会话(self, monkeypatch):
        from core import menu

        seen: list[int] = []
        replies: list[str] = []

        async def run(choice, event):
            seen.append(choice)

        def factory(*, timeout):
            def decorator(func):
                async def wrapper(event):
                    reply = _FakeEvent(msg="9")
                    await func(types.SimpleNamespace(stop=lambda: None), reply)
                    replies.extend(reply.texts)

                return wrapper

            return decorator

        monkeypatch.setattr(menu, "session_waiter", factory)
        await menu.ask_choice(_FakeEvent(), "清单", 3, run)
        assert seen == []
        assert replies and "无效序号" in replies[0]


async def _noop():
    return None


class TestConfigSchema:
    def _schema(self) -> dict:
        import json

        return json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    def test_等待提示配置(self):
        item = self._schema()["mingpian_task_hint"]
        assert item["type"] == "text"
        assert item["default"] == DEFAULT_TASK_HINT

    def test_提示词为模板列表(self):
        item = self._schema()["mingpian_prompts"]
        assert item["type"] == "template_list"
        assert item["templates"]["prompt_item"]["display_item"] == "name"
        assert set(item["templates"]["prompt_item"]["items"]) == {
            "name",
            "prompt",
            "size",
        }

    def test_默认提示词可被解析(self):
        defaults = self._schema()["mingpian_prompts"]["default"]
        prompts = load_prompts(defaults)
        assert len(prompts) == 3
        assert [p.name for p in prompts] == [
            "双眼特写",
            "双眼特写（电影黑条）",
            "多视角海报",
        ]
        assert all(len(p.text) > 500 for p in prompts)


class TestCloseupCommand:
    def _plugin(self, **conf):
        base = {
            "jx3api_base_url": "https://www.jx3api.com",
            "jx3api_token": "t",
            "image_api_base_url": "https://hub.example.com",
            "image_api_key": "k",
            "image_api_model": "gpt-image-2.5",
            "image_api_max_concurrent": 3,
            "mingpian_prompts": [
                {"name": "甲", "prompt": "提示词甲"},
                {"name": "乙", "prompt": "提示词乙"},
            ],
        }
        base.update(conf)
        return plugin_main.JX3MingpianPlugin(context=None, config=base)

    async def test_缺参数给用法(self):
        event = _FakeEvent(msg="名片特写")
        await self._plugin().mingpian_closeup(event)
        assert event.texts and "用法" in event.texts[0]

    async def test_非法名片序号被拦下(self):
        event = _FakeEvent(msg="名片特写 飞龙在天 小螺卜头 abc")
        await self._plugin().mingpian_closeup(event)
        assert len(event.texts) == 1 and "序号" in event.texts[0]

    async def test_未配置提示词(self):
        event = _FakeEvent(msg="名片特写 飞龙在天 小螺卜头")
        await self._plugin(mingpian_prompts=[]).mingpian_closeup(event)
        assert event.texts == ["未配置提示词"]

    async def test_超过并发上限给提示(self):
        plugin = self._plugin(image_api_max_concurrent=2)
        plugin._active = 2
        event = _FakeEvent(msg="名片特写 飞龙在天 小螺卜头")
        await plugin.mingpian_closeup(event)
        assert len(event.texts) == 1 and "同时最多 2 个" in event.texts[0]

    async def test_回复序号后出图(self, monkeypatch):
        plugin = self._plugin()
        seen = {}

        async def fake_ask(event, text, count, run, *, timeout=0):
            seen["menu"] = text
            seen["count"] = count
            seen["timeout"] = timeout
            await run(2, event)

        async def fake_resolve(jx3, *, server, name, index=None):
            seen["index"] = index
            return types.SimpleNamespace(
                school="唐门",
                scene="蜀中",
                accent="#123456",
                note="飞龙在天 · 小螺卜头 · 第 1/4 张",
            )

        async def fake_closeup(image, source, prompt):
            seen["prompt"] = prompt
            return b"\x89PNG-fake"

        monkeypatch.setattr(plugin_main, "ask_choice", fake_ask)
        monkeypatch.setattr(plugin_main, "resolve_card", fake_resolve)
        monkeypatch.setattr(plugin_main, "build_closeup", fake_closeup)

        event = _FakeEvent(msg="名片特写 飞龙在天 小螺卜头 1")
        await plugin.mingpian_closeup(event)

        assert seen["count"] == 2 and seen["timeout"] == plugin_main.CHOICE_TIMEOUT
        assert "1. 甲" in seen["menu"] and "2. 乙" in seen["menu"]
        assert seen["index"] == 1
        assert seen["prompt"].name == "乙"
        assert event.texts == [DEFAULT_TASK_HINT]
        assert event.images and event.images[0].startswith("base64://")
        assert plugin._active == 0

    async def test_生图失败给失败提示(self, monkeypatch):
        plugin = self._plugin()

        async def fake_ask(event, text, count, run, *, timeout=0):
            await run(1, event)

        async def boom(*args, **kwargs):
            raise ImageAPIError("生图密钥被拒绝（HTTP 401）：bad key")

        monkeypatch.setattr(plugin_main, "ask_choice", fake_ask)
        monkeypatch.setattr(plugin_main, "resolve_card", boom)

        event = _FakeEvent(msg="名片特写 飞龙在天 小螺卜头")
        await plugin.mingpian_closeup(event)
        assert event.texts[0] == DEFAULT_TASK_HINT
        assert "名片特写生成失败" in event.texts[1]
        assert not event.images
        assert plugin._active == 0

    async def test_等待提示可配置(self, monkeypatch):
        plugin = self._plugin(mingpian_task_hint="排队中，请稍候")

        async def fake_ask(event, text, count, run, *, timeout=0):
            await run(1, event)

        async def fake_resolve(jx3, *, server, name, index=None):
            return types.SimpleNamespace(
                school="", scene="", accent="#000000", note="n"
            )

        async def fake_closeup(image, source, prompt):
            return b"x"

        monkeypatch.setattr(plugin_main, "ask_choice", fake_ask)
        monkeypatch.setattr(plugin_main, "resolve_card", fake_resolve)
        monkeypatch.setattr(plugin_main, "build_closeup", fake_closeup)

        event = _FakeEvent(msg="名片特写 飞龙在天 小螺卜头")
        await plugin.mingpian_closeup(event)
        assert event.texts[0] == "排队中，请稍候"

    async def test_不再单独发结果说明(self, monkeypatch):
        plugin = self._plugin()

        async def fake_ask(event, text, count, run, *, timeout=0):
            await run(1, event)

        async def fake_resolve(jx3, *, server, name, index=None):
            return types.SimpleNamespace(
                school="唐门",
                scene="蜀中",
                accent="#123456",
                note="飞龙在天 · 小螺卜头 · 第 1/4 张",
            )

        async def fake_closeup(image, source, prompt):
            return b"png"

        monkeypatch.setattr(plugin_main, "ask_choice", fake_ask)
        monkeypatch.setattr(plugin_main, "resolve_card", fake_resolve)
        monkeypatch.setattr(plugin_main, "build_closeup", fake_closeup)

        event = _FakeEvent(msg="名片特写 飞龙在天 小螺卜头")
        await plugin.mingpian_closeup(event)
        assert all("第 1/4 张" not in text for text in event.texts)


class TestKeyPool:
    def test_单条字符串按一条处理(self):
        assert KeyPool("k1").keys == ["k1"]
        assert len(KeyPool("k1")) == 1

    @pytest.mark.parametrize("raw", [None, "", [], ["", "  "], [None]])
    def test_空来源视为无密钥(self, raw):
        pool = KeyPool(raw)
        assert not pool
        assert pool.order() == []

    def test_忽略空项与前后空白(self):
        assert KeyPool([" k1 ", "", "k2", None]).keys == ["k1", "k2"]

    def test_从副本构造保留游标(self):
        pool = KeyPool(["k1", "k2", "k3"])
        pool.mark_used("k2")
        copy = KeyPool(pool)
        assert copy.order() == ["k3", "k1", "k2"]

    def test_轮询顺序随游标推进(self):
        pool = KeyPool(["k1", "k2", "k3"])
        assert pool.order() == ["k1", "k2", "k3"]
        pool.mark_used("k1")
        assert pool.order() == ["k2", "k3", "k1"]
        pool.mark_used("k3")
        assert pool.order() == ["k1", "k2", "k3"]

    def test_标记未知密钥不改变游标(self):
        pool = KeyPool(["k1", "k2"])
        pool.mark_used("unknown")
        assert pool.order() == ["k1", "k2"]

    def test_sync_保留游标位置(self):
        pool = KeyPool(["k1", "k2", "k3"])
        pool.mark_used("k2")
        pool.sync(["k1", "k2", "k3"])
        assert pool.order() == ["k3", "k1", "k2"]

    def test_sync_缩短后游标取模(self):
        pool = KeyPool(["k1", "k2", "k3"])
        pool.mark_used("k3")
        pool.sync(["k1", "k2"])
        assert pool.order() in (["k1", "k2"], ["k2", "k1"])

    def test_sync_清空后无密钥(self):
        pool = KeyPool(["k1", "k2"])
        pool.sync([])
        assert not pool and pool.order() == []

    @pytest.mark.parametrize(
        "message, expected",
        [
            ("生图密钥被拒绝（HTTP 401）：bad", True),
            ("JX3API 令牌无效：expired", True),
            ("生图接口限流或额度不足（HTTP 429）：slow", True),
            ("生图超时（300 秒），请稍后再试", False),
            ("生图接口地址或模型名不对（HTTP 404）：nf", False),
        ],
    )
    def test_判定是否值得换密钥(self, message, expected):
        assert KeyPool.is_credential_failure(message) is expected


class TestMultiKeyRotation:
    """多条密钥时按轮询顺序使用，凭据类失败自动换下一条。"""

    def _client(self, tokens):
        return JX3APIClient("https://www.jx3api.com", tokens)

    async def test_第一条失败换下一条(self, monkeypatch):
        client = self._client(["bad", "good"])
        tried = []

        async def fake_request(self, path, params, token):
            tried.append(token)
            if token == "bad":
                raise JX3APIError("JX3API 令牌无效：expired")
            return {"ok": token}

        monkeypatch.setattr(JX3APIClient, "_request", fake_request)
        assert await client._get("/role/detail", {}) == {"ok": "good"}
        assert tried == ["bad", "good"]

    async def test_成功后游标前进(self, monkeypatch):
        client = self._client(["k1", "k2"])

        async def fake_request(self, path, params, token):
            return {"ok": token}

        monkeypatch.setattr(JX3APIClient, "_request", fake_request)
        await client._get("/role/detail", {})
        assert client.tokens.order() == ["k2", "k1"]

    async def test_非凭据类错误直接抛出(self, monkeypatch):
        client = self._client(["k1", "k2"])
        tried = []

        async def fake_request(self, path, params, token):
            tried.append(token)
            raise JX3APIError("JX3API 请求超时（30 秒）")

        monkeypatch.setattr(JX3APIClient, "_request", fake_request)
        with pytest.raises(JX3APIError):
            await client._get("/role/detail", {})
        assert tried == ["k1"]

    async def test_全部失败时报最后一条错误(self, monkeypatch):
        client = self._client(["k1", "k2"])

        async def fake_request(self, path, params, token):
            raise JX3APIError(f"JX3API 令牌无效：{token}")

        monkeypatch.setattr(JX3APIClient, "_request", fake_request)
        with pytest.raises(JX3APIError, match="k2"):
            await client._get("/role/detail", {})

    def test_单条密钥仍可用(self):
        assert self._client("only").configured is True

    def test_无密钥视为未配置(self):
        assert self._client([]).configured is False

    async def test_生图第一条失败换下一条(self, monkeypatch):
        client = ImageAPIClient("https://hub.example.com", ["bad", "good"])
        tried = []

        async def fake_post(self, url, key, *, form):
            tried.append(key)
            if key == "bad":
                raise ImageAPIError("生图密钥被拒绝（HTTP 401）：bad key")
            return {"data": [{"b64_json": base64.b64encode(b"png").decode()}]}

        monkeypatch.setattr(ImageAPIClient, "_post", fake_post)
        assert await client.edit("提示词", [REF_IMAGE]) == b"png"
        assert tried == ["bad", "good"]

    async def test_生图成功后游标前进(self, monkeypatch):
        client = ImageAPIClient("https://hub.example.com", ["k1", "k2"])

        async def fake_post(self, url, key, *, form):
            return {"data": [{"b64_json": base64.b64encode(b"png").decode()}]}

        monkeypatch.setattr(ImageAPIClient, "_post", fake_post)
        await client.edit("提示词", [REF_IMAGE])
        assert client.keys.order() == ["k2", "k1"]

    async def test_生图非凭据错误不换密钥(self, monkeypatch):
        client = ImageAPIClient("https://hub.example.com", ["k1", "k2"])
        tried = []

        async def fake_post(self, url, key, *, form):
            tried.append(key)
            raise ImageAPIError("生图接口地址或模型名不对（HTTP 404）：nf")

        monkeypatch.setattr(ImageAPIClient, "_post", fake_post)
        with pytest.raises(ImageAPIError):
            await client.edit("提示词", [REF_IMAGE])
        assert tried == ["k1"]


class TestWhitelist:
    def test_未启用时放行(self):
        assert is_allowed(False, {"u1"}, "u2") is True
        assert is_allowed(None, set(), "u2") is True

    def test_启用后仅名单内放行(self):
        assert is_allowed(True, {"u1", "u2"}, "u1") is True
        assert is_allowed(True, {"u1"}, "u9") is False

    def test_启用但名单为空时全部拒绝(self):
        assert is_allowed(True, set(), "u1") is False

    def test_编号前后空白被忽略(self):
        assert is_allowed(True, load_whitelist([" u1 ", ""]), "u1") is True

    # --- UMO ---

    def test_UMO_命中整个会话(self):
        umo = "aiocqhttp:GroupMessage:987654321"
        assert is_allowed(True, {umo}, "u9", umo) is True

    def test_UMO_不匹配其他会话(self):
        allowed = {"aiocqhttp:GroupMessage:987654321"}
        assert (
            is_allowed(True, allowed, "u9", "aiocqhttp:GroupMessage:111111111") is False
        )

    def test_UMO_区分消息类型(self):
        allowed = {"aiocqhttp:GroupMessage:123"}
        assert is_allowed(True, allowed, "u9", "aiocqhttp:FriendMessage:123") is False

    def test_UMO_区分平台(self):
        allowed = {"aiocqhttp:GroupMessage:123"}
        assert is_allowed(True, allowed, "u9", "telegram:GroupMessage:123") is False

    def test_用户ID与UMO可混用(self):
        allowed = {"u1", "aiocqhttp:GroupMessage:987654321"}
        assert is_allowed(True, allowed, "u1", "aiocqhttp:FriendMessage:1") is True
        assert (
            is_allowed(True, allowed, "u9", "aiocqhttp:GroupMessage:987654321") is True
        )
        assert is_allowed(True, allowed, "u9", "aiocqhttp:GroupMessage:222") is False

    def test_UMO_条目不会被当成用户ID(self):
        umo = "aiocqhttp:GroupMessage:987654321"
        assert is_allowed(True, {umo}, umo, "") is False

    def test_事件无UMO属性时不报错(self):
        class Bare:
            pass

        assert (
            is_allowed(True, {"u1"}, "u1", getattr(Bare(), "unified_msg_origin", ""))
            is True
        )

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (["a", "b"], {"a", "b"}),
            (["a", "a"], {"a"}),
            (["", "  ", None], set()),
            ("a", set()),
            (None, set()),
            ([123, "456"], {"123", "456"}),
        ],
    )
    def test_规整名单(self, raw, expected):
        assert load_whitelist(raw) == expected


class TestWhitelistCommand:
    def _plugin(self, **conf):
        base = {
            "jx3api_base_url": "https://www.jx3api.com",
            "jx3api_token": "t",
            "image_api_base_url": "https://hub.example.com",
            "image_api_key": "k",
            "image_api_model": "gpt-image-2.5",
            "image_api_max_concurrent": 3,
            "mingpian_prompts": [{"name": "甲", "prompt": "提示词甲"}],
        }
        base.update(conf)
        return plugin_main.JX3MingpianPlugin(context=None, config=base)

    async def test_名片卡_名单外被拒(self):
        plugin = self._plugin(whitelist_enabled=True, whitelist=["u1"])
        event = _FakeEvent(sender="u9", msg="名片卡 飞龙在天 小螺卜头")
        await plugin.mingpian_card(event)
        assert event.texts == [DENIED_MESSAGE]

    async def test_名片卡_名单内放行(self, monkeypatch):
        plugin = self._plugin(whitelist_enabled=True, whitelist=["u1"])

        async def fake_build(jx3, image, *, server, name, index=None):
            return {"bgImage": "data:image/png;base64,AA=="}, "说明"

        monkeypatch.setattr(plugin_main, "build_card", fake_build)
        monkeypatch.setattr(
            plugin_main.JX3MingpianPlugin,
            "_render",
            lambda self, payload: _async_value("http://img/card.png"),
        )
        event = _FakeEvent(sender="u1", msg="名片卡 飞龙在天 小螺卜头")
        await plugin.mingpian_card(event)
        assert event.texts == [DEFAULT_TASK_HINT]

    async def test_名片特写_名单外被拒(self):
        plugin = self._plugin(whitelist_enabled=True, whitelist=["u1"])
        event = _FakeEvent(sender="u9", msg="名片特写 飞龙在天 小螺卜头")
        await plugin.mingpian_closeup(event)
        assert event.texts == [DENIED_MESSAGE]

    async def test_名片特写_名单内放行(self, monkeypatch):
        plugin = self._plugin(whitelist_enabled=True, whitelist=["u1"])

        async def fake_ask(event, text, count, run, *, timeout=0):
            await run(1, event)

        async def fake_resolve(jx3, *, server, name, index=None):
            return types.SimpleNamespace(
                school="", scene="", accent="#000000", note="n"
            )

        async def fake_closeup(image, source, prompt):
            return b"x"

        monkeypatch.setattr(plugin_main, "ask_choice", fake_ask)
        monkeypatch.setattr(plugin_main, "resolve_card", fake_resolve)
        monkeypatch.setattr(plugin_main, "build_closeup", fake_closeup)

        event = _FakeEvent(sender="u1", msg="名片特写 飞龙在天 小螺卜头")
        await plugin.mingpian_closeup(event)
        assert event.texts[0] == DEFAULT_TASK_HINT


class TestCardCommand:
    def _plugin(self, **conf):
        base = {
            "jx3api_base_url": "https://www.jx3api.com",
            "jx3api_token": "t",
            "image_api_base_url": "https://hub.example.com",
            "image_api_key": "k",
            "image_api_model": "gpt-image-2.5",
            "image_api_max_concurrent": 3,
        }
        base.update(conf)
        return plugin_main.JX3MingpianPlugin(context=None, config=base)

    async def test_缺参数给用法(self):
        plugin = self._plugin()
        event = _FakeEvent(msg="名片卡")
        await plugin.mingpian_card(event)
        assert event.texts and "用法" in event.texts[0]

    async def test_非法序号被拦下(self):
        plugin = self._plugin()
        event = _FakeEvent(msg="名片卡 飞龙在天 小螺卜头 abc")
        await plugin.mingpian_card(event)
        assert len(event.texts) == 1 and "序号" in event.texts[0]

    async def test_超过并发上限给提示(self):
        plugin = self._plugin(image_api_max_concurrent=2)
        plugin._active = 2
        event = _FakeEvent(msg="名片卡 飞龙在天 小螺卜头")
        await plugin.mingpian_card(event)
        assert len(event.texts) == 1
        assert "同时最多 2 张" in event.texts[0]

    async def test_先回执再出图(self, monkeypatch):
        plugin = self._plugin()
        calls = {}

        async def fake_build(jx3, image, *, server, name, index=None):
            calls.update(server=server, name=name, index=index)
            return {
                "bgImage": "data:image/png;base64,AA=="
            }, "飞龙在天 · 小螺卜头 · 第 1/4 张"

        monkeypatch.setattr(plugin_main, "build_card", fake_build)
        monkeypatch.setattr(
            plugin_main.JX3MingpianPlugin,
            "_render",
            lambda self, payload: _async_value("http://img/card.png"),
        )

        event = _FakeEvent(msg="名片卡 飞龙在天 小螺卜头 1")
        await plugin.mingpian_card(event)
        assert event.texts[0] == DEFAULT_TASK_HINT
        assert calls == {"server": "飞龙在天", "name": "小螺卜头", "index": 1}
        assert event.images == ["http://img/card.png"]
        assert plugin._active == 0, "跑完要把并发计数放回去"

    async def test_生图失败给失败提示(self, monkeypatch):
        plugin = self._plugin()

        async def boom(*args, **kwargs):
            raise ImageAPIError("生图密钥被拒绝（HTTP 401）：bad key")

        monkeypatch.setattr(plugin_main, "build_card", boom)
        event = _FakeEvent(msg="名片卡 飞龙在天 小螺卜头")
        await plugin.mingpian_card(event)
        assert event.texts[0] == DEFAULT_TASK_HINT
        assert "名片卡生成失败" in event.texts[1]
        assert plugin._active == 0

    async def test_没配生图接口直接报错(self):
        plugin = self._plugin(image_api_base_url="", image_api_key="")
        event = _FakeEvent(msg="名片卡 飞龙在天 小螺卜头")
        await plugin.mingpian_card(event)
        assert "还没配置生图接口" in event.texts[1]


class TestCardArgs:
    @pytest.mark.parametrize(
        "args, bound, expected",
        [
            (["飞龙在天", "小螺卜头"], "", ("飞龙在天", "小螺卜头", "")),
            (["飞龙在天", "小螺卜头", "2"], "", ("飞龙在天", "小螺卜头", "2")),
            (["小螺卜头"], "飞龙在天", ("飞龙在天", "小螺卜头", "")),
            (["小螺卜头", "3"], "飞龙在天", ("飞龙在天", "小螺卜头", "3")),
            (["小螺卜头", "3"], "", ("小螺卜头", "3", "")),
        ],
    )
    def test_解析(self, args, bound, expected):
        assert parse_card_args(args, bound) == expected

    @pytest.mark.parametrize("args", [[], ["a", "b", "c", "d"]])
    def test_报错(self, args):
        assert isinstance(parse_card_args(args, ""), str)

    def test_未绑定且只给角色名时提示绑定(self):
        assert "绑定" in parse_card_args(["小螺卜头"], "")


class TestBindingStore:
    def test_存取与落盘(self, tmp_path):
        path = tmp_path / "b.json"
        store = BindingStore(path)
        assert store.get("umo1") == ""
        store.set("umo1", "飞龙在天")
        assert store.get("umo1") == "飞龙在天"
        assert BindingStore(path).get("umo1") == "飞龙在天"

    def test_忽略空值(self, tmp_path):
        store = BindingStore(tmp_path / "b.json")
        store.set("", "飞龙在天")
        store.set("umo1", "   ")
        assert store.get("umo1") == ""

    def test_文件损坏不炸(self, tmp_path):
        path = tmp_path / "b.json"
        path.write_text("{不是 json", encoding="utf-8")
        assert BindingStore(path).get("umo1") == ""


class TestCommandPattern:
    @pytest.mark.parametrize(
        "text",
        ["名片卡", "/名片卡", "名片卡 飞龙在天", "/名片卡 飞龙在天"],
    )
    def test_斜杠可选(self, text):
        assert re.match(plugin_main._command_pattern("名片卡"), text)

    @pytest.mark.parametrize("text", ["名片卡牌", "名片", "x名片卡"])
    def test_不误匹配(self, text):
        assert not re.match(plugin_main._command_pattern("名片卡"), text)


class TestSizePassthrough:
    async def test_尺寸与响应格式都传给接口(self, monkeypatch):
        client = ImageAPIClient("https://hub.example.com", "k")
        seen = {}
        original = FormData.add_field

        def spy(self, name, value, **kwargs):
            seen[name] = value
            return original(self, name, value, **kwargs)

        monkeypatch.setattr(FormData, "add_field", spy)

        async def fake_post(self, url, key, *, form):
            return {"data": [{"b64_json": base64.b64encode(b"png").decode()}]}

        monkeypatch.setattr(ImageAPIClient, "_post", fake_post)
        await client.edit("提示词", [REF_IMAGE], size="2048x880")
        assert seen["size"] == "2048x880"
        assert seen["response_format"] == "b64_json"


class TestDirectSend:
    """回复直发消息链，不经结果装饰，因此不带引用与 @。"""

    def test_命令用直发(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        assert "MessageChain(chain=" in source
        assert "event.plain_result(" not in source

    def test_菜单也用直发(self):
        source = (ROOT / "core" / "menu.py").read_text(encoding="utf-8")
        assert "MessageChain(chain=" in source
        assert "plain_result" not in source


class TestPromptSize:
    def test_条目带尺寸(self):
        prompts = load_prompts([{"name": "甲", "prompt": "x", "size": "1024x1024"}])
        assert prompts[0].size == "1024x1024"

    def test_缺尺寸用默认(self):
        prompts = load_prompts([{"name": "甲", "prompt": "x"}])
        assert prompts[0].size == DEFAULT_POSTER_SIZE

    def test_默认提示词都带尺寸(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for entry in schema["mingpian_prompts"]["default"]:
            assert entry.get("size"), entry["name"]


async def _async_value(value):
    return value
