"""给测试装一套假的 astrbot，让插件模块能在本机导入。"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock


def _install_fake_astrbot() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    core = types.ModuleType("astrbot.core")
    components = types.ModuleType("astrbot.api.message_components")
    core_utils = types.ModuleType("astrbot.core.utils")
    session_waiter = types.ModuleType("astrbot.core.utils.session_waiter")

    # 插件统一从 astrbot.api 取 logger；测试里吞掉日志调用即可
    api.logger = MagicMock()
    api.AstrBotConfig = dict
    api.message_components = components
    core.html_renderer = types.SimpleNamespace()

    class _Image:
        def __init__(self, file=""):
            self.file = file

        @staticmethod
        def fromBase64(data):
            return _Image(f"base64://{data}")

    class _Plain:
        def __init__(self, text=""):
            self.text = text

    components.Image = _Image
    components.Plain = _Plain
    components.Reply = object

    class _MessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

    event.MessageChain = _MessageChain

    # session_waiter 装饰器在测试里原样返回函数，不启动真实等待
    session_waiter.SessionController = object

    def _session_waiter(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    session_waiter.session_waiter = _session_waiter

    def _command(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    event.AstrMessageEvent = object
    event.filter = types.SimpleNamespace(command=_command, regex=_command)

    class Star:
        def __init__(self, context=None):
            self.context = context

    def register(*args, **kwargs):
        def decorator(cls):
            return cls

        return decorator

    # 绑定数据写到临时目录，不污染插件目录
    import tempfile

    _data_dir = Path(tempfile.mkdtemp(prefix="mingpian-test-"))

    star.Context = object
    star.Star = Star
    star.register = register
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: _data_dir)

    core.utils = core_utils
    core_utils.session_waiter = session_waiter

    astrbot.api = api
    astrbot.api.event = event
    astrbot.api.star = star
    astrbot.api.message_components = components
    astrbot.core = core

    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)
    sys.modules.setdefault("astrbot.api.event", event)
    sys.modules.setdefault("astrbot.api.star", star)
    sys.modules.setdefault("astrbot.api.message_components", components)
    sys.modules.setdefault("astrbot.core", core)
    sys.modules.setdefault("astrbot.core.utils", core_utils)
    sys.modules.setdefault("astrbot.core.utils.session_waiter", session_waiter)


_install_fake_astrbot()


# 插件用相对导入（from .core.card import ...），所以必须按「包」来加载，
# 直接 import main 会报 "attempted relative import with no known parent package"。
PLUGIN_PKG = "astrbot_plugin_jx3_mingpian"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def load_plugin_main():
    import importlib.util

    if f"{PLUGIN_PKG}.main" in sys.modules:
        return sys.modules[f"{PLUGIN_PKG}.main"]

    package = types.ModuleType(PLUGIN_PKG)
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PLUGIN_PKG] = package

    spec = importlib.util.spec_from_file_location(
        f"{PLUGIN_PKG}.main", PLUGIN_ROOT / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PLUGIN_PKG}.main"] = module
    spec.loader.exec_module(module)
    return module
