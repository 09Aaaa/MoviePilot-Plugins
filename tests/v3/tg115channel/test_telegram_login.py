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


def test_button_login_keeps_session_on_server_and_survives_form_save(plugin_module, monkeypatch):
    plugin = plugin_module.Tg115Channel()
    plugin.init_plugin({"telegram_api_id": "123", "telegram_api_hash": "hash"})

    async def step(**kwargs):
        assert "telegram_code" not in plugin._saved_config
        assert "telegram_password" not in plugin._saved_config
        assert "telegram_login_action" not in plugin._saved_config
        assert kwargs["password"] == "password"
        return {"message": "成功", "pending": {}, "session": "new-session"}

    monkeypatch.setattr(plugin_module, "login_step", step)
    result = plugin.telegram_login_api({"action": "verify", "telegram_code": "12345", "telegram_password": "password"})
    assert result["logged_in"] is True
    assert "new-session" not in str(result)
    assert "telegram_session" not in plugin._saved_config
    assert plugin.get_data("telegram_auth")["session"] == "new-session"
    plugin.init_plugin(plugin._saved_config)
    assert plugin._config["telegram_session"] == "new-session"
    assert plugin._login_view()["logged_in"] is True


def test_cancel_preserves_existing_login(plugin_module):
    plugin = plugin_module.Tg115Channel()
    plugin.init_plugin({"telegram_session": "existing"})
    plugin.save_data("telegram_login_pending", {"session": "pending"})
    plugin.telegram_login_api({"action": "cancel"})
    assert plugin.get_data("telegram_auth")["session"] == "existing"
    assert plugin.get_data("telegram_login_pending") == {}
    assert "telegram_session" not in plugin._saved_config


def test_failed_login_preserves_session_and_action_does_not_replay(plugin_module, monkeypatch):
    plugin = plugin_module.Tg115Channel()
    calls = []

    async def step(**kwargs):
        calls.append(kwargs)
        return {"message": "验证码错误", "pending": {"session": "pending"}}

    monkeypatch.setattr(plugin_module, "login_step", step)
    plugin.init_plugin({"telegram_api_id": "123", "telegram_session": "existing"})
    plugin.telegram_login_api({"action": "verify", "telegram_code": "12345"})
    assert plugin.get_data("telegram_auth")["session"] == "existing"
    plugin.init_plugin(plugin._saved_config)
    assert len(calls) == 1
    assert plugin._config["telegram_session"] == "existing"


def test_blank_code_never_triggers_implicit_send(login):
    module, client, kwargs = login
    pending = asyncio.run(module.login_step(**kwargs))["pending"]
    result = asyncio.run(module.login_step(**{**kwargs, "action": "verify", "pending": pending}))
    assert "请填写" in result["message"]
    assert len(client.calls) == 1


def test_actual_status_unlocks_bot_and_revocation_locks_it(plugin_module, monkeypatch):
    plugin = plugin_module.Tg115Channel()
    plugin.init_plugin({"telegram_api_id": "123", "telegram_api_hash": "hash", "telegram_session": "stored"})
    assert not plugin._login_view()["logged_in"]
    state = ["logged_in"]

    async def check(**kwargs):
        assert kwargs["session"] == "stored"
        return {"state": state[0], "message": state[0]}

    monkeypatch.setattr(plugin_module, "check_session", check)
    assert plugin.telegram_login_api({"action": "status"})["logged_in"]
    state[0] = "logged_out"
    assert not plugin.telegram_login_api({"action": "status"})["logged_in"]
    assert plugin.get_data("telegram_auth")["session"] == "stored"


def test_unsaved_app_credentials_cannot_unlock_bot(plugin_module, monkeypatch):
    plugin = plugin_module.Tg115Channel()
    plugin.init_plugin({"telegram_api_id": "123", "telegram_api_hash": "hash"})
    result = plugin.telegram_login_api({"action": "status", "telegram_api_id": "456", "telegram_api_hash": "new"})
    assert result["logged_in"] is False
    assert plugin._config["telegram_api_id"] == "123"


def test_view_flags_and_legacy_action_are_not_trusted_or_persisted(plugin_module, monkeypatch):
    plugin = plugin_module.Tg115Channel()

    async def forbidden(**_kwargs):
        raise AssertionError("Saving settings must never send a code")

    monkeypatch.setattr(plugin_module, "login_step", forbidden)
    plugin.init_plugin(
        {
            "_tg_logged_in": True,
            "telegram_login_action": "send",
            "telegram_code": "12345",
            "telegram_password": "secret",
        }
    )
    assert not plugin._login_view()["logged_in"]
    assert not any(key.startswith("_tg_") for key in plugin._saved_config)
    assert "telegram_code" not in plugin._saved_config
    assert "telegram_password" not in plugin._saved_config


def test_check_session_calls_authorization_and_disconnects(login):
    module, client, _kwargs = login
    result = asyncio.run(module.check_session(api_id=123, api_hash="hash", session="session"))
    assert result["state"] == "logged_in"
    assert client.disconnected
    client.authorized = False
    result = asyncio.run(module.check_session(api_id=123, api_hash="hash", session="session"))
    assert result["state"] == "logged_out"
