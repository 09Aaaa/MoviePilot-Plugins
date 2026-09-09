from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_sliding_window_and_boundary(plugin_module):
    module = importlib.import_module(f"{plugin_module.__package__}.rate_limit")
    now = [0.0]
    limiter = module.BotRateLimiter(2, 60, clock=lambda: now[0])
    limiter.acquire()
    now[0] = 20
    limiter.acquire()
    now[0] = 59
    with pytest.raises(module.BotRateLimitError):
        limiter.acquire()
    now[0] = 60
    limiter.acquire()
    with pytest.raises(module.BotRateLimitError):
        limiter.acquire()


def test_concurrent_requests_share_one_budget(plugin_module):
    module = importlib.import_module(f"{plugin_module.__package__}.rate_limit")
    limiter = module.BotRateLimiter(5, 60)

    def attempt(_):
        try:
            limiter.acquire()
            return True
        except module.BotRateLimitError:
            return False

    with ThreadPoolExecutor(max_workers=10) as executor:
        assert sum(executor.map(attempt, range(50))) == 5


def test_saving_config_preserves_budget(plugin_module):
    plugin = plugin_module.Tg115Channel()
    config = {
        "enabled": True,
        "telegram_api_id": "123",
        "telegram_api_hash": "hash",
        "telegram_session": "session",
        "resource_bot": "bot",
        "bot_request_count": 1,
    }
    plugin.init_plugin(config)
    budget = plugin._telegram.rate_limiter
    budget.acquire()
    plugin.init_plugin(config)
    assert plugin._telegram.rate_limiter is budget
    module = importlib.import_module(f"{plugin_module.__package__}.rate_limit")
    with pytest.raises(module.BotRateLimitError):
        plugin._telegram.rate_limiter.acquire()


def test_gateway_keeps_all_messages_and_limits_callback_requests(plugin_module):
    import asyncio
    from types import SimpleNamespace

    gateway_module = importlib.import_module(f"{plugin_module.__package__}.telegram_gateway")
    gateway = gateway_module.TelegramGateway(
        gateway_module.TelegramGatewayConfig(
            api_id=123,
            api_hash="hash",
            string_session="session",
            bot_username="bot",
            bot_request_count=2,
            detail_button_pattern="详情",
        )
    )
    clicks = []

    async def click(*args):
        clicks.append(args)
        return None

    messages = [SimpleNamespace(id=i + 2, raw_text=f"资源 {i}", buttons=[], out=False) for i in range(150)]
    messages[0].buttons = [
        [SimpleNamespace(text="详情", data=b"1", url=None), SimpleNamespace(text="详情", data=b"2", url=None)]
    ]
    messages[0].click = click

    class Conversation:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def send_message(self, _text):
            return SimpleNamespace(id=1)

    class Client:
        def conversation(self, _bot, **kwargs):
            assert kwargs["max_messages"] == float("inf")
            return Conversation()

        async def get_messages(self, _bot, **kwargs):
            assert kwargs["limit"] is None
            return []

    queue = iter(messages)

    async def receive(*_args):
        return next(queue, None)

    gateway._client = Client()
    gateway._bot = "bot"
    gateway._receive = receive
    result = asyncio.run(gateway._request_async("query"))
    assert len(result.replies) == 150
    assert len(clicks) == 1  # Search plus one callback consume the shared quota.
    with pytest.raises(importlib.import_module(f"{plugin_module.__package__}.rate_limit").BotRateLimitError):
        asyncio.run(gateway._request_async("transfer"))
