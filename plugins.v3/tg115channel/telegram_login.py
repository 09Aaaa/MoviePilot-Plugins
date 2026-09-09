"""Bounded, non-interactive Telegram login steps for the configuration form."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from contextlib import suppress
from typing import Any

PENDING_TTL = 600


def identity(api_id: int, api_hash: str, phone: str) -> str:
    return hashlib.sha256(f"{api_id}:{api_hash}:{phone}".encode()).hexdigest()


async def login_step(
    *, action: str, api_id: int, api_hash: str, phone: str, code: str, password: str, pending: dict[str, Any]
) -> dict[str, Any]:
    """Return only explicit UI messages; never expose raw RPC errors or input secrets."""
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession

    phone = re.sub(r"[\s()-]", "", phone)
    if api_id <= 0 or not api_hash:
        return {"message": "请先填写有效的 Telegram 应用 ID 和应用密钥。", "pending": {}}
    if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
        return {"message": "请输入含国家区号的手机号，例如 +8613800138000。", "pending": {}}
    key = identity(api_id, api_hash, phone)
    if pending.get("identity") != key or time.time() - pending.get("created", 0) > PENDING_TTL:
        pending = {}
    if action == "verify" and not pending:
        return {"message": "登录请求已过期或账号配置已变化，请重新发送验证码。", "pending": {}}
    if action == "send" and pending:
        return {"message": "验证码已发送，请完成登录；如需重发，先取消本次登录。", "pending": pending}
    client = TelegramClient(
        StringSession(pending.get("session", "")),
        api_id,
        api_hash,
        connection_retries=1,
        request_retries=0,
        flood_sleep_threshold=0,
    )
    try:
        async with asyncio.timeout(40):
            await client.connect()
            if action == "send":
                sent = await client.send_code_request(phone)
                pending = {
                    "identity": key,
                    "created": time.time(),
                    "session": client.session.save(),
                    "phone_code_hash": sent.phone_code_hash,
                    "password_needed": False,
                }
                return {
                    "message": "验证码已发送，请查看 Telegram 客户端或短信，填写后选择“完成登录”并保存。",
                    "pending": pending,
                }
            if pending.get("password_needed"):
                if not password:
                    return {"message": "此账号已开启两步验证，请填写两步验证密码后再次完成登录。", "pending": pending}
                await client.sign_in(password=password)
            else:
                if not code:
                    return {"message": "请填写收到的登录验证码。", "pending": pending}
                try:
                    await client.sign_in(phone=phone, code=code, phone_code_hash=pending["phone_code_hash"])
                except errors.SessionPasswordNeededError:
                    pending = {**pending, "password_needed": True, "session": client.session.save()}
                    if not password:
                        return {
                            "message": "此账号已开启两步验证，请填写两步验证密码后再次完成登录。",
                            "pending": pending,
                        }
                    await client.sign_in(password=password)
            if not await client.is_user_authorized():
                return {"message": "Telegram 尚未确认登录，请重新发送验证码。", "pending": {}}
            return {
                "message": "Telegram 登录成功，登录凭证已自动保存。",
                "pending": {},
                "session": client.session.save(),
            }
    except errors.PhoneCodeInvalidError:
        return {"message": "验证码不正确，请重新输入后完成登录。", "pending": pending}
    except errors.PhoneCodeExpiredError:
        return {"message": "验证码已过期，请重新发送。", "pending": {}}
    except errors.PasswordHashInvalidError:
        return {"message": "两步验证密码不正确，请重新输入。", "pending": pending}
    except errors.FloodWaitError as exc:
        return {"message": f"Telegram 请求过于频繁，请等待 {exc.seconds} 秒后重试。", "pending": pending}
    except (errors.PhoneNumberInvalidError, errors.PhoneNumberBannedError):
        return {"message": "手机号无效或已被 Telegram 限制，请在官方客户端检查账号。", "pending": {}}
    except errors.ApiIdInvalidError:
        return {"message": "应用 ID 或应用密钥无效，请检查后重试。", "pending": {}}
    except (TimeoutError, OSError):
        return {"message": "连接 Telegram 超时或网络不可用，请检查 MoviePilot 服务器网络后重试。", "pending": pending}
    except Exception:
        return {
            "message": "Telegram 未能完成登录，请检查官方客户端的安全提示；必要时取消本次登录后重试。",
            "pending": pending,
        }
    finally:
        with suppress(Exception):
            await asyncio.wait_for(client.disconnect(), timeout=5)
