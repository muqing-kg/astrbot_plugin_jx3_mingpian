"""区服绑定：按会话记住默认区服，并据此解析命令参数。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("astrbot")

FILENAME = "bindings.json"
UNBOUND_HINT = (
    "未绑定区服。请先发送「名片绑定 区服名」，或按「名片卡 服务器 角色名」填写。"
)


class BindingStore:
    """按统一消息来源保存默认区服，落盘为一个 JSON 文件。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("区服绑定读取失败，按空名单继续：%s", self.path)
            return
        if isinstance(raw, dict):
            self._data = {
                str(key): str(value).strip()
                for key, value in raw.items()
                if str(value or "").strip()
            }

    def get(self, umo: str) -> str:
        return self._data.get(str(umo or "").strip(), "")

    def set(self, umo: str, server: str) -> None:
        key = str(umo or "").strip()
        server = str(server or "").strip()
        if not key or not server:
            return
        self._data[key] = server
        self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.exception("区服绑定写入失败")


def parse_card_args(args: list[str], bound: str) -> tuple[str, str, str] | str:
    """解析「[服务器] 角色名 [序号]」，已绑定区服时服务器可省略。

    返回 (服务器, 角色名, 序号) 或错误文案。
    """
    args = [str(item).strip() for item in args if str(item).strip()]
    bound = str(bound or "").strip()
    if not args:
        return "缺少角色名。"
    if len(args) > 3:
        return "参数过多，最多为「服务器 角色名 序号」。"
    if len(args) == 3:
        return args[0], args[1], args[2]
    if len(args) == 2:
        # 已绑定区服且第二段是纯数字时，视为「角色名 序号」
        if bound and args[1].isdigit():
            return bound, args[0], args[1]
        return args[0], args[1], ""
    if not bound:
        return UNBOUND_HINT
    return bound, args[0], ""
