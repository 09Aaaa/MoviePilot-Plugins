from __future__ import annotations

import asyncio
import importlib
import time
from types import SimpleNamespace

import pytest
from telethon import errors


@pytest.fixture
def login(plugin_module, monkeypatch):
    module = importlib.import_module(f"{plugin_module.__package__}.telegram_login")

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.session = SimpleNamespace(save=lambda: "saved-session")
            self.calls = []
            self.failure = None
            self.authorized = True
            self.disconnected = False

        async def connect(self):
            pass

        async def disconnect(self):
            self.disconnected = True

        async def send_code_request(self, phone):
            self.calls.append(("send", phone))
            if self.failure:
                raise self.failure
            return SimpleNamespace(phone_code_hash="code-hash")

        async def sign_in(self, **kwargs):
            self.calls.append(("sign_in", kwargs))
            if self.failure:
                raise self.failure

        async def is_user_authorized(self):
            return self.authorized

    client = Client()
    monkeypatch.setattr("telethon.TelegramClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("telethon.sessions.StringSession", lambda value: value)
    kwargs = dict(
        action="send", api_id=123, api_hash="secret", phone="+8613800138000", code="", password="", pending={}
    )
    return module, client, kwargs


def test_send_verify_and_disconnect(login):
    module, client, kwargs = login
    sent = asyncio.run(module.login_step(**kwargs))
    assert sent["pending"]["phone_code_hash"] == "code-hash"
    assert client.disconnected
    result = asyncio.run(
        module.login_step(**{**kwargs, "action": "verify", "pending": sent["pending"], "code": "12345"})
    )
    assert result["session"] == "saved-session"
    assert result["pending"] == {}
    assert client.calls[-1][1]["phone_code_hash"] == "code-hash"


def test_two_factor_can_continue_without_code(login):
    module, client, kwargs = login
    pending = asyncio.run(module.login_step(**kwargs))["pending"]
    client.failure = errors.SessionPasswordNeededError(None)
    result = asyncio.run(module.login_step(**{**kwargs, "action": "verify", "pending": pending, "code": "12345"}))
    assert result["pending"]["password_needed"]
    client.failure = None
    result = asyncio.run(
        module.login_step(**{**kwargs, "action": "verify", "pending": result["pending"], "password": "2fa"})
    )
    assert result["session"]
    assert client.calls[-1] == ("sign_in", {"password": "2fa"})


@pytest.mark.parametrize(
    "failure,clear",
    [
        (errors.PhoneCodeInvalidError(None), False),
        (errors.PhoneCodeExpiredError(None), True),
        (errors.PasswordHashInvalidError(None), False),
        (errors.FloodWaitError(None, capture=12), False),
        (OSError("sensitive-network-details"), False),
    ],
)
def test_failures_are_safe_and_recoverable(login, failure, clear):
    module, client, kwargs = login
    pending = asyncio.run(module.login_step(**kwargs))["pending"]
    client.failure = failure
    result = asyncio.run(module.login_step(**{**kwargs, "action": "verify", "pending": pending, "code": "12345"}))
    assert bool(result["pending"]) is not clear
    assert "session" not in result
    assert "sensitive-network-details" not in result["message"]
    assert client.disconnected


@pytest.mark.parametrize("change", ["expired", "phone", "api_hash"])
def test_changed_or_expired_request_needs_new_code(login, change):
    module, client, kwargs = login
    pending = asyncio.run(module.login_step(**kwargs))["pending"]
    if change == "expired":
        pending["created"] = time.time() - 601
    else:
        kwargs[change] = "+8613900139000" if change == "phone" else "other"
    count = len(client.calls)
    result = asyncio.run(module.login_step(**{**kwargs, "action": "verify", "pending": pending, "code": "12345"}))
    assert not result["pending"]
    assert len(client.calls) == count


def test_duplicate_send_does_not_request_code(login):
    module, client, kwargs = login
    pending = asyncio.run(module.login_step(**kwargs))["pending"]
    asyncio.run(module.login_step(**{**kwargs, "pending": pending}))
    assert len(client.calls) == 1


def test_plugin_clears_secrets_and_saves_session(plugin_module, monkeypatch):
    plugin = plugin_module.Tg115Channel()

    async def step(**kwargs):
        assert plugin._saved_config["telegram_code"] == ""
        assert plugin._saved_config["telegram_password"] == ""
        assert plugin._saved_config["telegram_login_action"] == "none"
        assert kwargs["password"] == "password"
        return {"message": "成功", "pending": {}, "session": "new-session"}

    monkeypatch.setattr(plugin_module, "login_step", step)
    plugin.init_plugin(
        {
            "telegram_api_id": "123",
            "telegram_login_action": "verify",
            "telegram_code": "12345",
            "telegram_password": "password",
        }
    )
    assert plugin._saved_config["telegram_session"] == "new-session"
    assert plugin.get_data("telegram_login_status") == "成功"
    plugin.init_plugin(plugin._saved_config)
    assert plugin._config["telegram_session"] == "new-session"


def test_cancel_preserves_existing_login(plugin_module):
    plugin = plugin_module.Tg115Channel()
    plugin.save_data("telegram_login_pending", {"session": "pending"})
    plugin.init_plugin({"telegram_login_action": "cancel", "telegram_session": "existing"})
    assert plugin._saved_config["telegram_session"] == "existing"
    assert plugin.get_data("telegram_login_pending") == {}


def test_failed_login_preserves_session_and_action_does_not_replay(plugin_module, monkeypatch):
    plugin = plugin_module.Tg115Channel()
    calls = []

    async def step(**kwargs):
        calls.append(kwargs)
        return {"message": "验证码错误", "pending": {"session": "pending"}}

    monkeypatch.setattr(plugin_module, "login_step", step)
    plugin.init_plugin(
        {
            "telegram_api_id": "123",
            "telegram_session": "existing",
            "telegram_login_action": "verify",
            "telegram_code": "12345",
        }
    )
    assert plugin._saved_config["telegram_session"] == "existing"
    plugin.init_plugin(plugin._saved_config)
    assert len(calls) == 1
    assert plugin._config["telegram_session"] == "existing"


def test_blank_code_never_triggers_implicit_send(login):
    module, client, kwargs = login
    pending = asyncio.run(module.login_step(**kwargs))["pending"]
    result = asyncio.run(module.login_step(**{**kwargs, "action": "verify", "pending": pending}))
    assert "请填写" in result["message"]
    assert len(client.calls) == 1
