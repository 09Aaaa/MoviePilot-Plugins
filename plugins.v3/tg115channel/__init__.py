"""MoviePilot V3 plugin that exposes a Telegram 115 bot as a resource source."""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from app.plugins import _PluginBase
from app.sdk.logging import logger
from app.sdk.media import TorrentInfo

from .models import BotResource
from .p115_transfer import P115TransferService, TransferResult
from .protocol import (
    RESOURCE_ID_PATTERN,
    decode_resource_token,
    encode_resource_token,
    normalize_keyword,
    render_template,
    resource_id,
)
from .telegram_gateway import TelegramGateway, TelegramGatewayConfig
from .telegram_login import login_step

RESOURCE_DATA_KEY = "resource_registry_v1"
HISTORY_DATA_KEY = "history_v1"
MAX_RESOURCE_RECORDS = 500
MAX_HISTORY_RECORDS = 100
SENSITIVE_URL_PATTERN = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*(?:115\.com|115cdn\.com)(?:[^\s\"'<>]*)?",
    re.IGNORECASE,
)
COOKIE_VALUE_PATTERN = re.compile(r"\b(UID|CID|SEID)\s*=\s*[^;\s]+", re.IGNORECASE)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


class Tg115Channel(_PluginBase):
    """Search a Telegram resource bot first and transfer selected shares to 115."""

    plugin_name = "TG 115资源通道"
    plugin_desc = "优先通过 Telegram 资源机器人搜索，并将选中的 115 资源转存到分类目录。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/download.png"
    plugin_version = "0.1.2"
    _login_lock = threading.Lock()
    plugin_author = "09a"
    author_url = ""
    plugin_config_prefix = "tg115channel_"
    plugin_order = 5
    auth_level = 1

    def init_plugin(self, config: dict | None = None) -> None:
        """Apply configuration without starting network work during module import."""

        self.stop_service()
        config = dict(config or {})
        self._enabled = _as_bool(config.get("enabled"), False)
        self._config = {
            "enabled": self._enabled,
            "telegram_api_id": str(config.get("telegram_api_id") or "").strip(),
            "telegram_api_hash": str(config.get("telegram_api_hash") or "").strip(),
            "telegram_session": str(config.get("telegram_session") or "").strip(),
            "telegram_phone": str(config.get("telegram_phone") or "").strip(),
            "telegram_login_action": "none",
            "telegram_code": "",
            "telegram_password": "",
            "resource_bot": str(config.get("resource_bot") or "").strip().lstrip("@"),
            "search_template": str(config.get("search_template") or "{keyword}").strip(),
            "request_timeout": _as_float(config.get("request_timeout"), 60.0, 5.0, 180.0),
            "quiet_seconds": _as_float(config.get("quiet_seconds"), 2.0, 0.3, 15.0),
            "max_messages": _as_int(config.get("max_messages"), 12, 1, 50),
            "detail_button_pattern": str(config.get("detail_button_pattern") or "").strip(),
            "max_detail_clicks": _as_int(config.get("max_detail_clicks"), 3, 0, 10),
            "result_limit": _as_int(config.get("result_limit"), 10, 1, 30),
            "search_cache_minutes": _as_int(config.get("search_cache_minutes"), 15, 0, 1440),
            "resource_ttl_hours": _as_int(config.get("resource_ttl_hours"), 168, 1, 2160),
            "resource_priority": _as_int(config.get("resource_priority"), 100, 1, 100),
            "transfer_backend": str(config.get("transfer_backend") or "p115").strip().lower(),
            "p115_cookie": str(config.get("p115_cookie") or "").strip(),
            "telegram_transfer_template": str(config.get("telegram_transfer_template") or "").strip(),
            "telegram_success_pattern": str(
                config.get("telegram_success_pattern") or r"(?:成功|已转存|已保存|任务已提交)"
            ).strip(),
            "telegram_failure_pattern": str(
                config.get("telegram_failure_pattern") or r"(?:失败|错误|失效|不存在|无权限)"
            ).strip(),
            "movie_path": self._safe_path(config.get("movie_path"), "/影视/电影"),
            "tv_path": self._safe_path(config.get("tv_path"), "/影视/电视剧"),
            "default_path": self._safe_path(config.get("default_path"), "/影视/待整理"),
        }
        if self._config["transfer_backend"] not in {"p115", "telegram"}:
            self._config["transfer_backend"] = "p115"

        self._state_lock = threading.RLock()
        self._transfer_lock = threading.Lock()
        self._search_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
        self._resource_records = self._load_resource_records()
        self._history = self._load_history()
        self._last_error = ""
        self._telegram: TelegramGateway | None = None
        self._p115 = P115TransferService(self._config["p115_cookie"])

        self._process_login(config)
        if self._enabled:
            self._configure_telegram()

    def _process_login(self, submitted: dict[str, Any]) -> None:
        action = submitted.get("telegram_login_action", "none")
        if action not in {"send", "verify", "cancel"}:
            if submitted.get("telegram_code") or submitted.get("telegram_password"):
                self.update_config(self._config)
            return
        # Clear one-shot actions and secrets before doing any network work.
        self.update_config(self._config)
        with self._login_lock:
            if action == "cancel":
                self.save_data("telegram_login_pending", {})
                self.save_data("telegram_login_status", "本次登录已取消，已有登录凭证保留。")
                return
            try:
                api_id = int(self._config["telegram_api_id"])
                kwargs = dict(
                    action=action,
                    api_id=api_id,
                    api_hash=self._config["telegram_api_hash"],
                    phone=self._config["telegram_phone"],
                    code=str(submitted.get("telegram_code") or "").strip(),
                    password=str(submitted.get("telegram_password") or ""),
                    pending=self.get_data("telegram_login_pending") or {},
                )
                # init_plugin may run inside the host's event loop.
                with ThreadPoolExecutor(max_workers=1) as executor:
                    result = executor.submit(lambda: asyncio.run(login_step(**kwargs))).result()
            except (ValueError, TypeError):
                result = {"message": "请填写有效的数字应用 ID 后重试。", "pending": {}}
            except ImportError:
                result = {"message": "缺少 Telethon 依赖，请重新安装插件依赖。", "pending": {}}
            except Exception:
                result = {"message": "登录操作未完成，请检查配置和服务器网络后重试。", "pending": {}}
            self.save_data("telegram_login_pending", result["pending"])
            self.save_data("telegram_login_status", result["message"])
            if result.get("session"):
                self._config["telegram_session"] = result["session"]
                self.update_config(self._config)

    @staticmethod
    def _safe_path(value: Any, fallback: str) -> str:
        try:
            return P115TransferService.normalize_pan_path(str(value or fallback))
        except ValueError:
            logger.warning("TG115 配置中的 115 目录无效，已使用默认目录")
            return fallback

    def _configure_telegram(self) -> None:
        required = (
            self._config["telegram_api_id"],
            self._config["telegram_api_hash"],
            self._config["telegram_session"],
            self._config["resource_bot"],
        )
        if not all(required):
            self._last_error = "Telegram 用户会话配置不完整"
            logger.warning("TG115 已启用，但 Telegram api_id/api_hash/StringSession/机器人用户名配置不完整")
            return
        try:
            gateway_config = TelegramGatewayConfig(
                api_id=int(self._config["telegram_api_id"]),
                api_hash=self._config["telegram_api_hash"],
                string_session=self._config["telegram_session"],
                bot_username=self._config["resource_bot"],
                search_template=self._config["search_template"],
                request_timeout=self._config["request_timeout"],
                quiet_seconds=self._config["quiet_seconds"],
                max_messages=self._config["max_messages"],
                detail_button_pattern=self._config["detail_button_pattern"],
                max_detail_clicks=self._config["max_detail_clicks"],
            )
            self._telegram = TelegramGateway(gateway_config)
            self._validate_transfer_patterns()
        except (TypeError, ValueError, re.error) as exc:
            self._telegram = None
            self._last_error = self._safe_message(exc)
            logger.error("TG115 配置无效：%s", self._last_error)

    def _safe_message(self, value: Any) -> str:
        """Keep operational errors useful without exposing credentials or share URLs."""

        message = str(value or "").replace("\r", " ").replace("\n", " ").strip()
        for key in ("telegram_api_hash", "telegram_session", "p115_cookie"):
            secret = str(self._config.get(key) or "")
            if secret:
                message = message.replace(secret, "[敏感信息已隐藏]")
        message = COOKIE_VALUE_PATTERN.sub(lambda match: f"{match.group(1)}=[已隐藏]", message)
        message = SENSITIVE_URL_PATTERN.sub("[115分享链接已隐藏]", message)
        return message[:500]

    def _validate_transfer_patterns(self) -> None:
        for key in ("telegram_success_pattern", "telegram_failure_pattern"):
            pattern = self._config[key]
            if pattern:
                re.compile(pattern, re.IGNORECASE)
        template = self._config["telegram_transfer_template"]
        if template:
            render_template(
                template,
                {"url": "url", "code": "code", "path": "/path", "title": "title"},
                required=("url", "path"),
            )

    def _load_resource_records(self) -> dict[str, dict[str, Any]]:
        try:
            value = self.get_data(RESOURCE_DATA_KEY) or {}
        except Exception as exc:
            logger.warning("TG115 读取资源索引失败：%s", exc)
            return {}
        if not isinstance(value, dict):
            return {}
        now = time.time()
        ttl = self._config["resource_ttl_hours"] * 3600
        records: dict[str, dict[str, Any]] = {}
        for identifier, record in value.items():
            identifier = str(identifier)
            if not RESOURCE_ID_PATTERN.fullmatch(identifier) or not isinstance(record, dict):
                continue
            try:
                discovered_at = float(record.get("discovered_at") or 0)
            except (TypeError, ValueError):
                continue
            if discovered_at <= 0 or now - discovered_at > ttl:
                continue
            records[identifier] = dict(record)
        return records

    def _load_history(self) -> list[dict[str, Any]]:
        try:
            value = self.get_data(HISTORY_DATA_KEY) or []
        except Exception as exc:
            logger.warning("TG115 读取历史记录失败：%s", exc)
            return []
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value[-MAX_HISTORY_RECORDS:] if isinstance(item, dict)]

    def _save_resources(self) -> None:
        with self._state_lock:
            records = dict(self._resource_records)
        try:
            self.save_data(RESOURCE_DATA_KEY, records)
        except Exception as exc:
            logger.error("TG115 保存资源索引失败：%s", exc)

    def _record_history(self, **entry: Any) -> None:
        record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
        with self._state_lock:
            self._history.append(record)
            self._history = self._history[-MAX_HISTORY_RECORDS:]
            history = list(self._history)
        try:
            self.save_data(HISTORY_DATA_KEY, history)
        except Exception as exc:
            logger.error("TG115 保存历史记录失败：%s", exc)

    def _prune_resources(self) -> None:
        now = time.time()
        ttl = self._config["resource_ttl_hours"] * 3600
        ordered = sorted(
            self._resource_records.items(),
            key=lambda item: float(item[1].get("discovered_at") or 0),
            reverse=True,
        )
        self._resource_records = {
            identifier: record for identifier, record in ordered if now - float(record.get("discovered_at") or 0) <= ttl
        }
        if len(self._resource_records) > MAX_RESOURCE_RECORDS:
            self._resource_records = dict(list(self._resource_records.items())[:MAX_RESOURCE_RECORDS])

    @staticmethod
    def _media_type_key(media_type: Any) -> str:
        value = str(getattr(media_type, "value", media_type) or "").strip().lower()
        if value in {"电视剧", "tv", "series", "show", "tvshow"}:
            return "tv"
        if value in {"电影", "movie", "film"}:
            return "movie"
        return "unknown"

    @staticmethod
    def _category(media_type: str) -> str:
        if media_type == "tv":
            return "电视剧"
        if media_type == "movie":
            return "电影"
        return ""

    def _destination(self, media_type: str) -> str:
        if media_type == "tv":
            return self._config["tv_path"]
        if media_type == "movie":
            return self._config["movie_path"]
        return self._config["default_path"]

    def _cache_key(self, keyword: str, media_type: str) -> str:
        return f"{media_type}:{normalize_keyword(keyword).casefold()}"

    def _cached_resources(self, key: str) -> list[tuple[str, dict[str, Any]]] | None:
        ttl = self._config["search_cache_minutes"] * 60
        if ttl <= 0:
            return None
        with self._state_lock:
            cached = self._search_cache.get(key)
            if not cached or time.monotonic() - cached[0] > ttl:
                self._search_cache.pop(key, None)
                return None
            records = [
                (identifier, self._resource_records[identifier])
                for identifier in cached[1]
                if identifier in self._resource_records
            ]
        return records

    def _store_search_results(
        self,
        *,
        cache_key: str,
        keyword: str,
        media_type: str,
        resources: list[BotResource],
    ) -> list[tuple[str, dict[str, Any]]]:
        now = time.time()
        stored: list[tuple[str, dict[str, Any]]] = []
        with self._state_lock:
            for resource in resources[: self._config["result_limit"]]:
                identifier = resource_id(resource, context=f"{media_type}:{keyword}")
                record = resource.to_record(media_type=media_type, keyword=keyword, discovered_at=now)
                self._resource_records[identifier] = record
                stored.append((identifier, record))
            self._prune_resources()
            self._search_cache[cache_key] = (time.monotonic(), tuple(identifier for identifier, _ in stored))
        self._save_resources()
        return stored

    def _torrent(self, identifier: str, record: dict[str, Any]) -> TorrentInfo:
        resource = BotResource.from_record(record)
        keyword = normalize_keyword(str(record.get("keyword") or resource.title))
        media_type = str(record.get("media_type") or "unknown")
        quality_suffix = (
            f" {resource.quality}" if resource.quality and resource.quality.casefold() not in keyword.casefold() else ""
        )
        bot_title = resource.title if resource.title and resource.title.casefold() != keyword.casefold() else ""
        details = ["TG 115资源"]
        if bot_title:
            details.append(bot_title)
        if resource.quality:
            details.append(resource.quality)
        details.append(f"转存至 {self._destination(media_type)}")
        labels = ["TG115", "115网盘"]
        if resource.quality:
            labels.append(resource.quality)
        return TorrentInfo(
            site_name="TG115",
            site_order=0,
            site_downloader=self.__class__.__name__,
            title=f"{keyword}{quality_suffix}".strip(),
            description=" · ".join(details),
            enclosure=encode_resource_token(identifier),
            page_url=f"https://t.me/{self._config['resource_bot']}",
            size=resource.size,
            seeders=1,
            peers=0,
            grabs=0,
            uploadvolumefactor=1.0,
            downloadvolumefactor=0.0,
            labels=labels,
            pri_order=self._config["resource_priority"],
            category=self._category(media_type),
        )

    def search_torrents(
        self,
        site: dict[str, Any],
        keyword: str,
        mtype: Any = None,
        page: int | None = 0,
        **_: Any,
    ) -> list[TorrentInfo]:
        """Participate once in each native search; failures leave normal sites untouched."""

        del site
        keyword = normalize_keyword(keyword)
        if not self._enabled or _as_int(page, 0, 0, 1_000_000) > 0 or not keyword or not self._telegram:
            return []
        media_type = self._media_type_key(mtype)
        key = self._cache_key(keyword, media_type)
        cached = self._cached_resources(key)
        if cached is not None:
            return [self._torrent(identifier, record) for identifier, record in cached]
        try:
            resources = self._telegram.search(keyword)
            stored = self._store_search_results(
                cache_key=key,
                keyword=keyword,
                media_type=media_type,
                resources=resources,
            )
            self._last_error = ""
            self._record_history(action="搜索", keyword=keyword, result=f"找到 {len(stored)} 条资源")
            logger.info("TG115 资源搜索完成：%s，返回 %s 条", keyword, len(stored))
            return [self._torrent(identifier, record) for identifier, record in stored]
        except Exception as exc:
            error = self._safe_message(exc)
            self._last_error = error
            self._record_history(action="搜索", keyword=keyword, result="TG 通道失败，已回退普通站点")
            logger.warning("TG115 资源搜索失败，MoviePilot 将继续普通站点搜索：%s", error)
            return []

    async def async_search_torrents(self, **kwargs: Any) -> list[TorrentInfo]:
        """Keep Telegram waits away from MoviePilot's main asyncio loop."""

        return await asyncio.to_thread(self.search_torrents, **kwargs)

    def _telegram_transfer(self, resource: BotResource, destination: str) -> TransferResult:
        if not self._telegram:
            return TransferResult(False, "Telegram 用户会话未配置", destination)
        template = self._config["telegram_transfer_template"]
        if not template:
            return TransferResult(False, "未配置 TG 转存命令模板", destination)
        try:
            command = render_template(
                template,
                {
                    "url": resource.url,
                    "code": resource.access_code,
                    "path": destination,
                    "title": resource.title,
                },
                required=("url", "path"),
            )
            reply = self._telegram.request(command)
            text = reply.text
            failure_pattern = self._config["telegram_failure_pattern"]
            success_pattern = self._config["telegram_success_pattern"]
            if failure_pattern and re.search(failure_pattern, text, re.IGNORECASE):
                return TransferResult(False, "TG 机器人报告转存失败", destination)
            if not success_pattern or not re.search(success_pattern, text, re.IGNORECASE):
                return TransferResult(False, "TG 机器人未返回可识别的成功状态", destination)
            return TransferResult(True, "TG 机器人已确认转存", destination)
        except Exception as exc:
            return TransferResult(False, f"TG 机器人转存请求失败: {exc}", destination)

    def download(
        self,
        content: Path | str | bytes,
        download_dir: Path,
        cookie: str = "",
        episodes: set[int] | None = None,
        category: str | None = None,
        label: str | None = None,
        downloader: str | None = None,
        **_: Any,
    ) -> tuple[str | None, str | None, str | None, str] | None:
        """Handle only this plugin's opaque token and synchronously settle the transfer."""

        del download_dir, cookie, episodes, category, label, downloader
        identifier = decode_resource_token(content)
        if identifier is None:
            return None
        with self._state_lock:
            record = dict(self._resource_records.get(identifier) or {})
        if not record:
            return self.__class__.__name__, None, None, "TG115 资源已过期，请重新搜索"
        try:
            resource = BotResource.from_record(record)
        except (TypeError, ValueError) as exc:
            return self.__class__.__name__, None, None, f"TG115 资源记录无效: {exc}"
        media_type = str(record.get("media_type") or "unknown")
        destination = self._destination(media_type)

        with self._transfer_lock:
            if self._config["transfer_backend"] == "telegram":
                result = self._telegram_transfer(resource, destination)
            else:
                result = self._p115.transfer(
                    url=resource.url,
                    access_code=resource.access_code,
                    destination=destination,
                )

        fingerprint = identifier[:10]
        if not result.ok:
            message = self._safe_message(result.message)
            self._last_error = message
            self._record_history(
                action="转存",
                keyword=str(record.get("keyword") or resource.title),
                resource=fingerprint,
                destination=destination,
                result="失败",
            )
            logger.error("TG115 资源转存失败 [%s]：%s", fingerprint, message)
            return self.__class__.__name__, None, None, message

        self._last_error = ""
        transfer_hash = hashlib.sha1(
            f"tg115:{identifier}:{destination}".encode(),
            usedforsecurity=False,
        ).hexdigest()
        self._record_history(
            action="转存",
            keyword=str(record.get("keyword") or resource.title),
            resource=fingerprint,
            destination=destination,
            result="成功",
        )
        logger.info("TG115 资源转存成功 [%s] -> %s", fingerprint, destination)
        return self.__class__.__name__, transfer_hash, "NoSubfolder", ""

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        return []

    def get_module(self) -> dict[str, Any]:
        if not self._enabled:
            return {}
        return {
            "search_torrents": self.search_torrents,
            "async_search_torrents": self.async_search_torrents,
            "download": self.download,
        }

    def get_api(self) -> list[dict[str, Any]]:
        return []

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        form, defaults = self._get_form_schema()
        status = self.get_data("telegram_login_status") or "尚未进行手机号登录；已有登录凭证可继续使用。"
        form[0]["content"].insert(
            0,
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": status,
                },
            },
        )
        # Keep every control in the same grid; adjacent bare inputs otherwise
        # collide with Vuetify rows' negative margins and floating labels.
        fields = []
        for item in form[0]["content"]:
            if item["component"] == "VRow":
                fields.extend(item["content"])
            else:
                fields.append({"component": "VCol", "props": {"cols": 12}, "content": [item]})
        for column in fields:
            props = column["props"]
            props["style"] = "min-width: 0; padding: 12px;"
            if props.get("md") == 3:
                props.update({"sm": 6, "md": 6, "lg": 3})
            for control in column["content"]:
                if control["component"] not in {"VTextField", "VSelect", "VSwitch"}:
                    continue
                options = control["props"]
                options.update({"density": "comfortable", "hide-details": "auto"})
                if options.get("hint"):
                    options["persistent-hint"] = True
                if control["component"] == "VTextField":
                    options["autocomplete"] = "new-password" if options.get("type") == "password" else "off"
                    options["spellcheck"] = False
                if options.get("model") == "telegram_api_id":
                    options.update({"inputmode": "numeric", "placeholder": "例如 12345678"})
        form[0]["content"] = [{"component": "VRow", "props": {"style": "margin: 0;"}, "content": fields}]
        return form, defaults

    def _get_form_schema(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": (
                                "必须使用 Telegram MTProto 用户会话；Bot Token 不能操控另一个资源机器人。"
                                "敏感配置请勿提交到 Git。"
                            ),
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "transfer_backend",
                                            "label": "转存方式",
                                            "hint": (
                                                "将分享资源保存到你的 115 网盘。"
                                                "115 直转由插件完成；TG 命令转存需机器人支持。"
                                            ),
                                            "persistent-hint": True,
                                            "items": [
                                                {"title": "115 直转", "value": "p115"},
                                                {"title": "TG 命令转存", "value": "telegram"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "resource_priority",
                                            "label": "资源优先级",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "result_limit", "label": "最多返回资源数", "type": "number"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "telegram_api_id",
                                            "label": "Telegram 应用 ID（api_id）",
                                            "hint": (
                                                "在 my.telegram.org 的 API development tools 页面申请，填写数字 ID。"
                                            ),
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "telegram_api_hash",
                                            "label": "Telegram 应用密钥（api_hash）",
                                            "hint": "与应用 ID 在同一页面获取，复制对应的 api_hash。",
                                            "persistent-hint": True,
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "resource_bot",
                                            "label": "资源机器人用户名",
                                            "placeholder": "example_bot",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "telegram_phone",
                            "label": "Telegram 手机号（含国家区号）",
                            "hint": "例如 +8613800138000。填写应用 ID、密钥和手机号后，选择“发送验证码”并保存。",
                            "persistent-hint": True,
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "telegram_code",
                            "label": "登录验证码",
                            "hint": "查看 Telegram 客户端或短信中的验证码，填写后选择“完成登录”并保存。",
                            "persistent-hint": True,
                            "type": "password",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "telegram_password",
                            "label": "两步验证密码（按提示填写）",
                            "hint": "仅在账号开启两步验证时需要；每次保存后清空。",
                            "persistent-hint": True,
                            "type": "password",
                        },
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "telegram_login_action",
                            "label": "Telegram 登录操作（选择后保存）",
                            "items": [
                                {"title": "不执行登录操作", "value": "none"},
                                {"title": "发送验证码", "value": "send"},
                                {"title": "完成登录", "value": "verify"},
                                {"title": "取消本次登录 / 准备重发", "value": "cancel"},
                            ],
                            "hint": (
                                "保存后重新打开配置查看结果。新设备验证按 Telegram 提示完成；"
                                "登录成功后自动保存凭证。"
                            ),
                            "persistent-hint": True,
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "telegram_session",
                            "label": "高级：已有登录凭证（StringSession，可留空）",
                            "hint": "手机号登录成功后自动填写，无需手动生成。仅在迁移已有凭证时粘贴，请勿分享。",
                            "persistent-hint": True,
                            "type": "password",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "search_template",
                                            "label": "搜索消息模板",
                                            "hint": "必须包含 {keyword}",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "detail_button_pattern",
                                            "label": "详情按钮正则（可选）",
                                            "hint": "只点击明确匹配的回调按钮；留空最安全",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "request_timeout",
                                            "label": "首条回复超时（秒）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "quiet_seconds",
                                            "label": "消息静默收集（秒）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "max_messages", "label": "最多收集消息数", "type": "number"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_detail_clicks",
                                            "label": "最多点击详情按钮",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "p115_cookie",
                            "label": "115 客户端 Cookie（115 直转时填写）",
                            "type": "password",
                            "hint": "需要包含 UID、CID、SEID",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "telegram_transfer_template",
                            "label": "TG 转存命令模板（TG 命令转存时填写）",
                            "hint": "可用 {url} {code} {path} {title}；必须包含 {url} 和 {path}",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "telegram_success_pattern", "label": "TG 转存成功正则"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "telegram_failure_pattern", "label": "TG 转存失败正则"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "movie_path", "label": "115 电影目录"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "tv_path", "label": "115 电视剧目录"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "default_path", "label": "115 兜底目录"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "search_cache_minutes",
                                            "label": "搜索缓存（分钟）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "resource_ttl_hours",
                                            "label": "资源令牌有效期（小时）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "telegram_api_id": "",
            "telegram_api_hash": "",
            "telegram_session": "",
            "telegram_phone": "",
            "telegram_code": "",
            "telegram_password": "",
            "telegram_login_action": "none",
            "resource_bot": "",
            "search_template": "{keyword}",
            "request_timeout": 60,
            "quiet_seconds": 2,
            "max_messages": 12,
            "detail_button_pattern": "",
            "max_detail_clicks": 3,
            "result_limit": 10,
            "search_cache_minutes": 15,
            "resource_ttl_hours": 168,
            "resource_priority": 100,
            "transfer_backend": "p115",
            "p115_cookie": "",
            "telegram_transfer_template": "",
            "telegram_success_pattern": r"(?:成功|已转存|已保存|任务已提交)",
            "telegram_failure_pattern": r"(?:失败|错误|失效|不存在|无权限)",
            "movie_path": "/影视/电影",
            "tv_path": "/影视/电视剧",
            "default_path": "/影视/待整理",
        }

    def get_page(self) -> list[dict[str, Any]]:
        telegram_ready = bool(self._telegram)
        transfer_ready = (
            not P115TransferService.cookie_error(self._config.get("p115_cookie", ""))
            if self._config.get("transfer_backend") == "p115"
            else bool(self._config.get("telegram_transfer_template"))
        )
        status = (
            f"启用：{'是' if self._enabled else '否'}；Telegram 配置：{'完整' if telegram_ready else '不完整'}；"
            f"转存配置：{'完整' if transfer_ready else '不完整'}；缓存资源：{len(self._resource_records)} 条"
        )
        content: list[dict[str, Any]] = [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if self._enabled and telegram_ready and transfer_ready else "warning",
                    "variant": "tonal",
                    "text": status,
                },
            }
        ]
        login_status = self.get_data("telegram_login_status")
        if login_status:
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": login_status,
                    },
                }
            )
        if self._last_error:
            content.append(
                {
                    "component": "VAlert",
                    "props": {"type": "error", "variant": "tonal", "text": f"最近错误：{self._last_error}"},
                }
            )
        if self._history:
            content.append(
                {
                    "component": "VDataTable",
                    "props": {
                        "headers": [
                            {"title": "时间", "key": "time"},
                            {"title": "动作", "key": "action"},
                            {"title": "关键词", "key": "keyword"},
                            {"title": "目录", "key": "destination"},
                            {"title": "结果", "key": "result"},
                        ],
                        "items": list(reversed(self._history[-20:])),
                        "items-per-page": 10,
                    },
                }
            )
        return content

    def stop_service(self) -> None:
        gateway = getattr(self, "_telegram", None)
        if gateway:
            try:
                gateway.stop()
            except Exception as exc:
                logger.warning("TG115 停止 Telegram 客户端失败：%s", exc)
        self._telegram = None
        self._enabled = False
