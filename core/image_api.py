"""生图接口客户端。OpenAI Images 格式，gpt-image-2 与 gpt-image-2.5 通用。

配置项：
    image_api_base_url   形如 https://host/v1，末尾缺 /v1 时自动补
    image_api_key        密钥
    image_api_model      模型名

请求接口：{base}/images/edits（multipart）。
名片卡均带参考图，不使用纯文生图接口。

生图请求不经代理，与 JX3API 的代理配置相互独立。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import Iterable
from pathlib import Path

import aiohttp
from aiohttp import ClientTimeout, FormData

logger = logging.getLogger("astrbot")

DEFAULT_MODEL = "gpt-image-2.5"

DEFAULT_TIMEOUT = 300

VALID_SIZES = {
    "auto",
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2048x2048",
    "2048x1152",
    "3840x2160",
    "2160x3840",
}


class ImageAPIError(RuntimeError):
    """生图失败。message 为可直接返回给用户的文案。"""


def normalize_api_base(value: object) -> str:
    """规范生图接口地址：去尾斜杠，缺 /v1 时补上。空值返回空串。"""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        raise ValueError("生图接口地址必须以 http:// 或 https:// 开头。")
    if not text.endswith("/v1") and "/v1/" not in text:
        text += "/v1"
    return text


class ImageAPIClient:
    """每次调用创建独立会话，调用结束即关闭。"""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        *,
        ssl_verify: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.base_url = normalize_api_base(base_url)
        self.api_key = str(api_key or "").strip()
        self.model = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.ssl_verify = bool(ssl_verify)
        self.timeout = int(timeout)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def endpoint(self, kind: str) -> str:
        """kind: generations / edits"""
        return f"{self.base_url}/images/{kind}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "astrbot-jx3-mingpian",
            "Accept": "application/json",
        }

    async def _post(self, url: str, *, form: FormData) -> dict:
        timeout = ClientTimeout(total=self.timeout, sock_read=self.timeout)
        try:
            # 不传 proxy：生图与 JX3API 的代理配置相互独立
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(
                    url,
                    headers=self._headers(),
                    data=form,
                    ssl=self.ssl_verify,
                ) as response,
            ):
                raw = await response.text()
                if response.status != 200:
                    raise ImageAPIError(self._explain_http(response.status, raw))
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    raise ImageAPIError("生图接口返回的不是 JSON")
        except ImageAPIError:
            raise
        except asyncio.TimeoutError:
            raise ImageAPIError(f"生图超时（{self.timeout} 秒），请稍后再试")
        except aiohttp.ClientError as exc:
            raise ImageAPIError(f"生图网络请求失败：{exc}")

    @staticmethod
    def _explain_http(status: int, body: str) -> str:
        detail = (body or "").strip()[:200]
        # 响应体可能不是 JSON，解析失败时沿用原始文本
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                detail = str(error["message"])[:200]
            elif payload.get("message"):
                detail = str(payload["message"])[:200]
        if status in (401, 403):
            return f"生图密钥被拒绝（HTTP {status}）：{detail}"
        if status == 404:
            return f"生图接口地址或模型名不对（HTTP 404）：{detail}"
        if status == 429:
            return f"生图接口限流或额度不足（HTTP 429）：{detail}"
        return f"生图接口报错（HTTP {status}）：{detail}"

    @staticmethod
    def _pick_image(payload: dict) -> bytes:
        items = payload.get("data")
        if not isinstance(items, list) or not items:
            raise ImageAPIError("生图接口没有返回图片")
        for item in items:
            if not isinstance(item, dict):
                continue
            b64 = item.get("b64_json")
            if not isinstance(b64, str) or not b64:
                continue
            try:
                return base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError):
                logger.warning("生图返回的 b64_json 无法解码，尝试下一条")
                continue
        raise ImageAPIError("生图接口没有返回可用的图片数据（缺 b64_json）")

    @staticmethod
    def _is_stable_failure(message: str) -> bool:
        """判断是否为配置类错误。此类错误重试无效，直接放弃。"""
        for marker in ("密钥被拒绝", "地址或模型名不对", "不是 JSON"):
            if marker in message:
                return True
        return False

    async def edit(
        self,
        prompt: str,
        images: Iterable[Path],
        *,
        size: str = "2048x1152",
        quality: str = "high",
        retries: int = 1,
    ) -> bytes:
        """带参考图生图。失败时按 retries 重试。"""
        if not self.configured:
            raise ImageAPIError(
                "还没配置生图接口，请填写「生图接口地址 / 密钥 / 模型名」"
            )
        paths = [Path(p) for p in images]
        if not paths:
            raise ImageAPIError("edit 至少需要一张参考图")

        last_error = ""
        for attempt in range(retries + 1):
            form = FormData()
            form.add_field("model", self.model)
            form.add_field("prompt", prompt)
            form.add_field("size", size if size in VALID_SIZES else "auto")
            form.add_field("quality", quality)
            form.add_field("n", "1")
            for path in paths:
                form.add_field(
                    "image[]",
                    path.read_bytes(),
                    filename=path.name,
                    content_type="image/png"
                    if path.suffix.lower() == ".png"
                    else "image/jpeg",
                )
            logger.info(
                "生图请求 %s model=%s size=%s 参考图 %d 张（第 %d 次）",
                self.endpoint("edits"),
                self.model,
                size,
                len(paths),
                attempt + 1,
            )
            try:
                payload = await self._post(self.endpoint("edits"), form=form)
                return self._pick_image(payload)
            except ImageAPIError as exc:
                last_error = str(exc)
                if attempt >= retries or self._is_stable_failure(last_error):
                    break
                logger.warning("生图失败，重试一次：%s", last_error)
                await asyncio.sleep(1.5)
        raise ImageAPIError(last_error or "生图失败")
