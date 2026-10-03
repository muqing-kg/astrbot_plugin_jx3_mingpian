"""密钥池：多条密钥按轮询顺序使用。

实例持有游标，多次请求之间共享，因此轮询是跨请求生效的。
调用方每次取 ``order()`` 得到从当前游标开始的完整顺序，
成功后用 ``mark_used()`` 把游标推进到下一条。
"""

from __future__ import annotations

from collections.abc import Iterable


def _normalize(keys: KeyPool | str | Iterable[str] | None) -> list[str]:
    """规整密钥来源：单条字符串按一条处理，其余按可迭代处理，忽略空项。"""
    if keys is None:
        return []
    if isinstance(keys, str):
        keys = [keys]
    return [text for item in keys if (text := str(item or "").strip())]


class KeyPool:
    """一组密钥，按顺序轮询使用。"""

    def __init__(self, keys: KeyPool | str | Iterable[str] | None = None):
        if isinstance(keys, KeyPool):
            self._keys = list(keys._keys)
            self._cursor = keys._cursor
            return
        self._keys = _normalize(keys)
        self._cursor = 0

    def sync(self, keys: KeyPool | str | Iterable[str] | None) -> None:
        """按当前配置更新密钥内容。游标按新长度取模保留，避免重置轮询位置。"""
        self._keys = _normalize(keys)
        self._cursor = self._cursor % len(self._keys) if self._keys else 0

    def __bool__(self) -> bool:
        return bool(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def order(self) -> list[str]:
        """从当前游标开始的轮询顺序。"""
        if not self._keys:
            return []
        offset = self._cursor % len(self._keys)
        return self._keys[offset:] + self._keys[:offset]

    def mark_used(self, key: str) -> None:
        """记录某条密钥刚被成功使用，游标移到它的下一条。"""
        if key in self._keys:
            self._cursor = (self._keys.index(key) + 1) % len(self._keys)

    @staticmethod
    def is_credential_failure(message: str) -> bool:
        """该错误是否值得换一条密钥重试。"""
        for marker in (
            "密钥被拒绝",
            "令牌无效",
            "限流",
            "额度不足",
            "HTTP 401",
            "HTTP 403",
            "HTTP 429",
        ):
            if marker in message:
                return True
        return False
