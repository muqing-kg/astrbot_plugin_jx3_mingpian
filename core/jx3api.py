"""JX3API 客户端。仅提供名片卡所需的两个接口。

本模块的请求经配置中的 HTTP 代理发出。生图请求不走该代理，见 core/image_api.py。

令牌去掉首尾空白后使用。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any

import aiohttp
from aiohttp import ClientTimeout
from astrbot.api import logger

from .keypool import KeyPool

DEFAULT_BASE_URL = "https://www.jx3api.com"
DEFAULT_TIMEOUT = 30

# 使用的 JX3API 接口路径
PATH_CARD_RECORDS = "/card/records"
PATH_ROLE_DETAIL = "/role/detail"


class JX3APIError(RuntimeError):
    """JX3API 调用失败。message 为可直接返回给用户的文案。"""


class JX3APIClient:
    """JX3API 客户端。token 可传单条或密钥池，池内按轮询顺序使用。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str | KeyPool | Iterable[str] = "",
        *,
        proxy: str = "",
        ssl_verify: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.base_url = (
            str(base_url or DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL
        )
        self.tokens = token if isinstance(token, KeyPool) else KeyPool(token)
        self.proxy = str(proxy or "").strip()
        self.ssl_verify = bool(ssl_verify)
        self.timeout = int(timeout)

    @property
    def configured(self) -> bool:
        return bool(self.tokens)

    async def _get(self, path: str, params: dict) -> Any:
        if not self.configured:
            raise JX3APIError("还没配置 JX3API 接口令牌")

        last_error = ""
        for index, token in enumerate(self.tokens.take_order()):
            try:
                data = await self._request(path, params, token)
            except JX3APIError as exc:
                last_error = str(exc)
                # 换一条密钥可能成功；其余错误直接抛出
                if not KeyPool.is_credential_failure(last_error):
                    raise
                if index + 1 < len(self.tokens):
                    logger.warning("接口令牌不可用，改用下一条：%s", last_error)
                    continue
                raise
            return data
        raise JX3APIError(last_error or "JX3API 请求失败")

    async def _request(self, path: str, params: dict, token: str) -> Any:
        query = {**params, "token": token}
        url = f"{self.base_url}{path}"
        timeout = ClientTimeout(total=self.timeout, sock_read=self.timeout)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(
                    url,
                    params=query,
                    headers={
                        "User-Agent": "astrbot-jx3-mingpian",
                        "Accept": "application/json",
                    },
                    ssl=self.ssl_verify,
                    proxy=self.proxy or None,
                ) as response,
            ):
                raw = await response.text()
        except asyncio.TimeoutError:
            raise JX3APIError(f"JX3API 请求超时（{self.timeout} 秒）")
        except aiohttp.ClientError as exc:
            raise JX3APIError(f"JX3API 网络请求失败：{exc}")

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise JX3APIError("JX3API 返回的不是 JSON")
        if not isinstance(payload, dict):
            raise JX3APIError("JX3API 返回结构异常")

        code = payload.get("code")
        if code != 200:
            message = str(
                payload.get("msg") or payload.get("message") or f"code={code}"
            )
            if code in (401, 403):
                raise JX3APIError(f"JX3API 令牌无效：{message}")
            if code == 429:
                # 文案需含「限流」，密钥池据此判断是否换下一条
                raise JX3APIError(f"JX3API 限流：{message}")
            raise JX3APIError(f"JX3API 报错：{message}")
        return payload.get("data")

    async def role_detail(self, server: str, name: str) -> dict:
        """角色详情。返回门派、体型、阵营、帮会等字段。"""
        data = await self._get(
            PATH_ROLE_DETAIL, {"server": server, "name": name, "history": 0}
        )
        return data if isinstance(data, dict) else {}

    async def card_records(self, server: str, name: str) -> list[dict]:
        """名片记录。每条包含 showAvatar 图片地址。"""
        data = await self._get(PATH_CARD_RECORDS, {"server": server, "name": name})
        if not isinstance(data, list):
            return []
        return [
            item for item in data if isinstance(item, dict) and item.get("showAvatar")
        ]

    async def get_bytes(self, url: str) -> bytes | None:
        """下载名片形象图。该地址属 JX3API 服务，请求经同一代理发出。"""
        if not url:
            return None
        timeout = ClientTimeout(total=max(self.timeout, 60))
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(
                    url,
                    headers={"User-Agent": "astrbot-jx3-mingpian"},
                    ssl=self.ssl_verify,
                    proxy=self.proxy or None,
                ) as response,
            ):
                if response.status != 200:
                    logger.error("名片形象图下载失败 %s: %s", response.status, url)
                    return None
                return await response.read()
        except asyncio.TimeoutError:
            logger.error("名片形象图下载超时 %s", url)
            return None
        except aiohttp.ClientError as exc:
            logger.error("名片形象图下载出错 %s: %s", url, exc)
            return None
