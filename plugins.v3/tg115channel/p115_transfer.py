"""Narrow 115 share-transfer adapter built on the pinned p115client API."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True, slots=True)
class TransferResult:
    ok: bool
    message: str
    destination: str
    share_code: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class P115TransferService:
    """Transfer a complete 115 share into one validated account directory."""

    REQUIRED_COOKIE_KEYS = frozenset({"UID", "CID", "SEID"})
    ALREADY_SAVED_MARKERS = (
        "已经转存",
        "已转存",
        "已经保存",
        "已保存",
        "already",
        "exist",
    )

    def __init__(
        self,
        cookie: str,
        *,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.cookie = str(cookie or "").strip()
        self._client_factory = client_factory
        self._client: Any = None
        self._lock = threading.RLock()

    @classmethod
    def parse_cookie(cls, cookie: str) -> dict[str, str]:
        pairs: dict[str, str] = {}
        for part in str(cookie or "").strip().strip(";").split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key.strip() and value.strip():
                pairs[key.strip()] = value.strip()
        return pairs

    @classmethod
    def cookie_error(cls, cookie: str) -> str:
        pairs = cls.parse_cookie(cookie)
        missing = sorted(cls.REQUIRED_COOKIE_KEYS - set(pairs))
        return f"115 客户端 Cookie 缺少 {'/'.join(missing)}" if missing else ""

    @staticmethod
    def normalize_pan_path(path: str) -> str:
        raw = str(path or "").strip().replace("\\", "/")
        if not raw:
            raise ValueError("115 目标目录不能为空")
        if any(ord(character) < 32 for character in raw):
            raise ValueError("115 目标目录包含控制字符")
        parts: list[str] = []
        for part in raw.split("/"):
            part = part.strip()
            if not part or part == ".":
                continue
            if part == "..":
                raise ValueError("115 目标目录不允许包含 ..")
            if len(part) > 255:
                raise ValueError("115 目标目录的单级名称过长")
            parts.append(part)
        normalized = "/" + "/".join(parts)
        if len(normalized) > 1024:
            raise ValueError("115 目标目录过长")
        return normalized or "/"

    @staticmethod
    def is_share_url(url: str) -> bool:
        hostname = (urlparse(str(url or "").strip()).hostname or "").lower()
        return (
            hostname == "115.com"
            or hostname.endswith(".115.com")
            or hostname == "115cdn.com"
            or hostname.endswith(".115cdn.com")
        )

    @staticmethod
    def _extract_share_payload(url: str, access_code: str = "") -> tuple[str, str]:
        try:
            from p115client.util import share_extract_payload

            payload = share_extract_payload(url) or {}
            share_code = str(payload.get("share_code") or "").strip()
            receive_code = str(payload.get("receive_code") or "").strip()
            if share_code:
                return share_code, receive_code or str(access_code or "").strip()
        except Exception:
            pass

        parsed = urlparse(str(url or "").strip())
        match = re.search(r"/s/([^/?#]+)", parsed.path or "")
        share_code = match.group(1).strip() if match else ""
        query = parse_qs(parsed.query)
        receive_code = str(access_code or "").strip()
        if not receive_code:
            for key in ("password", "receive_code", "pwd", "code"):
                values = query.get(key)
                if values and str(values[0]).strip():
                    receive_code = str(values[0]).strip()
                    break
        return share_code, receive_code

    @staticmethod
    def _response_ok(response: Any) -> bool:
        if not isinstance(response, dict):
            return False
        if response.get("state") is True:
            return True
        if response.get("code") in (0, "0") and response.get("state") not in (False, 0):
            return True
        return response.get("errno") in (0, "0") and response.get("state") not in (False, 0)

    @staticmethod
    def _response_message(response: Any) -> str:
        if not isinstance(response, dict):
            return str(response or "")
        for key in ("error", "message", "msg", "errno"):
            value = response.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    @classmethod
    def _already_saved(cls, message: str) -> bool:
        lowered = str(message or "").casefold()
        return any(marker.casefold() in lowered for marker in cls.ALREADY_SAVED_MARKERS)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        error = self.cookie_error(self.cookie)
        if error:
            raise RuntimeError(error)
        if self._client_factory:
            self._client = self._client_factory(self.cookie)
            return self._client
        try:
            from p115client import P115Client
        except ImportError as exc:
            raise RuntimeError("缺少 p115client 依赖，请重新安装插件依赖") from exc
        self._client = P115Client(self.cookie, console_qrcode=False)
        return self._client

    def _directory_id(self, client: Any, path: str) -> int:
        if path == "/":
            return 0
        try:
            response = client.fs_dir_getid(path)
            directory_id = int(response.get("id", -1)) if isinstance(response, dict) else -1
            if directory_id > 0:
                return directory_id
        except (AttributeError, TypeError, ValueError):
            pass

        response = client.fs_makedirs_app(path, pid=0)
        if isinstance(response, dict):
            candidates = [response.get("cid")]
            data = response.get("data")
            if isinstance(data, dict):
                candidates.append(data.get("cid"))
            for candidate in candidates:
                try:
                    directory_id = int(candidate)
                except (TypeError, ValueError):
                    continue
                if directory_id > 0:
                    return directory_id
        message = self._response_message(response) or "返回中没有目录 ID"
        raise RuntimeError(f"无法创建 115 目录: {message}")

    def transfer(self, *, url: str, access_code: str = "", destination: str) -> TransferResult:
        destination = self.normalize_pan_path(destination)
        url = str(url or "").strip()
        if not self.is_share_url(url):
            return TransferResult(False, "资源不是有效的 115 分享链接", destination)
        share_code, receive_code = self._extract_share_payload(url, access_code)
        if not share_code:
            return TransferResult(False, "无法解析 115 分享码", destination)
        if not receive_code:
            return TransferResult(False, "115 分享链接缺少提取码", destination, share_code)

        with self._lock:
            try:
                client = self._get_client()
                directory_id = self._directory_id(client, destination)
                response = client.share_receive(
                    {
                        "share_code": share_code,
                        "receive_code": receive_code,
                        "file_id": 0,
                        "cid": directory_id,
                        "is_check": 0,
                    }
                )
            except Exception as exc:
                return TransferResult(False, f"115 转存调用失败: {exc}", destination, share_code)

        if self._response_ok(response):
            return TransferResult(
                True,
                "115 转存成功",
                destination,
                share_code,
                {"directory_id": directory_id},
            )
        message = self._response_message(response) or "115 返回未知错误"
        if self._already_saved(message):
            return TransferResult(
                True,
                "资源已存在于 115",
                destination,
                share_code,
                {"directory_id": directory_id, "already_saved": True},
            )
        return TransferResult(False, f"115 转存失败: {message}", destination, share_code)
