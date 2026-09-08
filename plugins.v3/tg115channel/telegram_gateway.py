"""Telegram MTProto gateway with an isolated, restartable asyncio loop."""

from __future__ import annotations

import asyncio
import re
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .models import BotReply, BotResource, ReplyButton, RequestResult
from .protocol import extract_resources, render_template


class TelegramGatewayError(RuntimeError):
    """Raised when the Telegram user session cannot complete a bot request."""


@dataclass(frozen=True, slots=True)
class TelegramGatewayConfig:
    api_id: int
    api_hash: str
    string_session: str
    bot_username: str
    search_template: str = "{keyword}"
    request_timeout: float = 60.0
    quiet_seconds: float = 2.0
    max_messages: int = 12
    detail_button_pattern: str = ""
    max_detail_clicks: int = 3
    connect_timeout: float = 20.0

    def validate(self) -> None:
        if self.api_id <= 0:
            raise ValueError("Telegram api_id 无效")
        if not self.api_hash.strip():
            raise ValueError("Telegram api_hash 不能为空")
        if not self.string_session.strip():
            raise ValueError("Telegram StringSession 不能为空")
        if not self.bot_username.strip().lstrip("@"):
            raise ValueError("Telegram 资源机器人用户名不能为空")
        if self.request_timeout < 5:
            raise ValueError("Telegram 回复超时不能小于 5 秒")
        if not 0.3 <= self.quiet_seconds <= 15:
            raise ValueError("Telegram 收集静默时间必须在 0.3 到 15 秒之间")
        if not 1 <= self.max_messages <= 50:
            raise ValueError("Telegram 单次收集消息数必须在 1 到 50 之间")
        if not 0 <= self.max_detail_clicks <= 10:
            raise ValueError("Telegram 详情按钮点击数必须在 0 到 10 之间")
        render_template(self.search_template, {"keyword": "test"}, required=("keyword",))
        if self.detail_button_pattern:
            re.compile(self.detail_button_pattern, re.IGNORECASE)


class TelegramGateway:
    """Serialize conversations with one resource bot on a dedicated event loop."""

    def __init__(self, config: TelegramGatewayConfig) -> None:
        config.validate()
        self.config = config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._bot: Any = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._lifecycle_lock = threading.RLock()
        self._request_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._ready.is_set() and not self._startup_error)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.running:
                return
            if self._thread and self._thread.is_alive():
                raise TelegramGatewayError("Telegram 客户端仍在启动或停止，请稍后重试")
            self._ready.clear()
            self._startup_error = None
            self._thread = threading.Thread(
                target=self._thread_main,
                name="tg115-telegram",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=self.config.connect_timeout):
            self.stop()
            raise TelegramGatewayError("连接 Telegram 超时")
        if self._startup_error:
            error = self._startup_error
            self.stop()
            raise TelegramGatewayError(f"连接 Telegram 失败: {error}") from error

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._connect())
        except BaseException as exc:  # lifecycle boundary: preserve the original startup error
            self._startup_error = exc
            self._ready.set()
        else:
            self._ready.set()
            loop.run_forever()
        finally:
            try:
                if self._client and self._client.is_connected():
                    loop.run_until_complete(self._client.disconnect())
            except BaseException:
                pass
            self._client = None
            self._bot = None
            self._loop = None
            loop.close()

    async def _connect(self) -> None:
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError as exc:
            raise TelegramGatewayError("缺少 Telethon 依赖，请重新安装插件依赖") from exc

        self._client = TelegramClient(
            StringSession(self.config.string_session.strip()),
            self.config.api_id,
            self.config.api_hash.strip(),
            connection_retries=3,
            request_retries=2,
            auto_reconnect=True,
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise TelegramGatewayError("StringSession 未授权或已失效")
        self._bot = await self._client.get_entity(self.config.bot_username.strip().lstrip("@"))

    def stop(self) -> None:
        with self._lifecycle_lock:
            loop = self._loop
            client = self._client
            thread = self._thread
            if loop and loop.is_running():
                if client and client.is_connected():
                    try:
                        future = asyncio.run_coroutine_threadsafe(client.disconnect(), loop)
                        future.result(timeout=5)
                    except (FutureTimeoutError, RuntimeError, OSError):
                        pass
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(loop.stop)
            if thread and thread is not threading.current_thread():
                thread.join(timeout=8)
            if not thread or not thread.is_alive():
                self._thread = None
            self._ready.clear()

    @staticmethod
    def _project_message(message: Any) -> BotReply:
        buttons: list[ReplyButton] = []
        for row_index, row in enumerate(getattr(message, "buttons", None) or []):
            for column_index, button in enumerate(row or []):
                url = str(getattr(button, "url", "") or "")
                buttons.append(
                    ReplyButton(
                        text=str(getattr(button, "text", "") or ""),
                        url=url,
                        row=row_index,
                        column=column_index,
                        callback=bool(getattr(button, "data", None)) and not url,
                    )
                )
        message_id = getattr(message, "id", None)
        return BotReply(
            message_id=int(message_id) if message_id is not None else None,
            text=str(getattr(message, "raw_text", "") or getattr(message, "message", "") or ""),
            buttons=tuple(buttons),
        )

    @staticmethod
    def _callback_projection(answer: Any) -> BotReply | None:
        if answer is None:
            return None
        text = str(getattr(answer, "message", "") or "")
        url = str(getattr(answer, "url", "") or "")
        if not text and not url:
            return None
        buttons = (ReplyButton(text="详情链接", url=url),) if url else ()
        return BotReply(message_id=None, text=text, buttons=buttons)

    async def _receive(self, conversation: Any, timeout: float) -> Any | None:
        try:
            return await asyncio.wait_for(conversation.get_response(), timeout=timeout)
        except TimeoutError:
            return None

    async def _request_async(self, text: str) -> RequestResult:
        if not self._client or not self._bot:
            raise TelegramGatewayError("Telegram 客户端尚未连接")

        native_messages: list[Any] = []
        projections: list[BotReply] = []
        async with self._client.conversation(
            self._bot,
            timeout=self.config.request_timeout,
            exclusive=True,
        ) as conversation:
            sent = await conversation.send_message(text)
            first = await self._receive(conversation, self.config.request_timeout)
            if first is None:
                raise TelegramGatewayError("资源机器人在超时前没有回复")
            native_messages.append(first)

            while len(native_messages) < self.config.max_messages:
                reply = await self._receive(conversation, self.config.quiet_seconds)
                if reply is None:
                    break
                native_messages.append(reply)

            pattern = (
                re.compile(self.config.detail_button_pattern, re.IGNORECASE)
                if self.config.detail_button_pattern
                else None
            )
            clicked = 0
            if pattern:
                for message in tuple(native_messages):
                    for row_index, row in enumerate(getattr(message, "buttons", None) or []):
                        for column_index, button in enumerate(row or []):
                            if clicked >= self.config.max_detail_clicks:
                                break
                            if getattr(button, "url", None) or not getattr(button, "data", None):
                                continue
                            if not pattern.search(str(getattr(button, "text", "") or "")):
                                continue
                            try:
                                answer = await asyncio.wait_for(
                                    message.click(row_index, column_index),
                                    timeout=min(15.0, self.config.request_timeout),
                                )
                                projection = self._callback_projection(answer)
                                if projection:
                                    projections.append(projection)
                                clicked += 1
                                reply = await self._receive(conversation, self.config.quiet_seconds)
                                if reply is not None:
                                    native_messages.append(reply)
                            except Exception:
                                continue
                        if clicked >= self.config.max_detail_clicks:
                            break
                    if clicked >= self.config.max_detail_clicks:
                        break

            try:
                recent = await self._client.get_messages(
                    self._bot,
                    limit=self.config.max_messages,
                    min_id=int(getattr(sent, "id", 0) or 0),
                )
                native_messages.extend(reversed(list(recent or [])))
            except (OSError, RuntimeError, ValueError):
                pass

        by_id: dict[int, BotReply] = {}
        idless: list[BotReply] = list(projections)
        for message in native_messages:
            if getattr(message, "out", False):
                continue
            projection = self._project_message(message)
            if projection.message_id is None:
                idless.append(projection)
            else:
                by_id[projection.message_id] = projection
        ordered = [by_id[key] for key in sorted(by_id)]
        ordered.extend(idless)
        return RequestResult(replies=tuple(ordered))

    def request(self, text: str) -> RequestResult:
        text = str(text or "").strip()
        if not text:
            raise ValueError("发送给 Telegram 机器人的消息不能为空")
        with self._request_lock:
            self.start()
            loop = self._loop
            if not loop or not loop.is_running():
                raise TelegramGatewayError("Telegram 事件循环不可用")
            future = asyncio.run_coroutine_threadsafe(self._request_async(text), loop)
            budget = (
                self.config.request_timeout
                + self.config.quiet_seconds * (self.config.max_messages + self.config.max_detail_clicks + 1)
                + 20
            )
            try:
                return future.result(timeout=budget)
            except FutureTimeoutError as exc:
                future.cancel()
                raise TelegramGatewayError("等待 Telegram 机器人完整回复超时") from exc
            except TelegramGatewayError:
                raise
            except BaseException as exc:
                raise TelegramGatewayError(f"Telegram 机器人请求失败: {exc}") from exc

    async def async_request(self, text: str) -> RequestResult:
        return await asyncio.to_thread(self.request, text)

    def search(self, keyword: str) -> list[BotResource]:
        query = render_template(
            self.config.search_template,
            {"keyword": keyword},
            required=("keyword",),
        )
        result = self.request(query)
        return extract_resources(result.replies, keyword=keyword)
