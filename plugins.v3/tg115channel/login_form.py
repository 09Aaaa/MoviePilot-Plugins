"""Native MoviePilot form events using its authenticated API client."""

from __future__ import annotations

import json

BOT_UNLOCKED = (
    "model._tg_logged_in && model.telegram_api_id === model._tg_api_id "
    "&& model.telegram_api_hash === model._tg_api_hash && model.telegram_phone === model._tg_phone"
)


def login_handler(plugin_id: str, action: str) -> str:
    route = json.dumps(f"plugin/{plugin_id}/telegram/login")
    operation = json.dumps(action)
    return f"""async () => {{
        if (model._tg_busy) return;
        model._tg_busy = true;
        model._tg_message = {operation} === 'status' ? '正在检查 Telegram 登录状态…' : '正在处理登录请求…';
        if ({operation} === 'status') model._tg_logged_in = false;
        const apiId = model.telegram_api_id;
        const apiHash = model.telegram_api_hash;
        const phone = model.telegram_phone;
        const payload = {{action: {operation}, telegram_api_id: apiId,
            telegram_api_hash: apiHash, telegram_phone: phone,
            telegram_code: model.telegram_code, telegram_password: model.telegram_password}};
        try {{
            if (!window.MoviePilotAPI) throw new Error('API unavailable');
            const result = await window.MoviePilotAPI.post({route}, payload,
                {{timeout: 55000, feedback: 'silent'}});
            model._tg_state = result.state;
            model._tg_logged_in = result.logged_in === true;
            model._tg_message = result.message;
            model._tg_api_id = apiId;
            model._tg_api_hash = apiHash;
            model._tg_phone = phone;
        }} catch (_) {{
            model._tg_message = '登录请求未完成，请检查网络或重新登录 MoviePilot 后重试。';
            model._tg_state = 'unknown';
            model._tg_logged_in = false;
        }} finally {{
            model.telegram_code = '';
            model.telegram_password = '';
            model._tg_busy = false;
        }}
    }}"""


def login_buttons(plugin_id: str) -> list[dict]:
    controls = []
    for action, label in (
        ("send", "发送登录验证码"),
        ("verify", "登录"),
        ("status", "检查登录状态"),
        ("cancel", "取消本次登录"),
    ):
        props = {
            "type": "button",
            "color": "primary",
            "variant": "tonal",
            "disabled": "{{ model._tg_busy }}",
            "onClick": login_handler(plugin_id, action),
        }
        column_props = {"cols": 12, "sm": 6, "md": 3}
        if action in {"verify", "cancel"}:
            column_props["show"] = "{{ ['code_sent', 'password_needed'].includes(model._tg_state) }}"
        controls.append(
            {
                "component": "VCol",
                "props": column_props,
                "content": [{"component": "VBtn", "text": label, "props": props}],
            }
        )
    return controls


def directory_handler(plugin_id: str, *, selection: bool = False) -> str:
    route = json.dumps(f"plugin/{plugin_id}/115/directories")
    target = "String(event)" if selection else "String(model.destination_id || '0')"
    return f"""async (event) => {{
        if (model._dir_busy) return;
        model._dir_busy = true;
        const previous = model._dir_current;
        try {{
            const result = await window.MoviePilotAPI.post({route}, {{
                directory_id: {target}, cookie: model.p115_cookie
            }}, {{timeout: 55000, feedback: 'silent'}});
            if (!result.ok) {{ model._dir_message = result.message; model.destination_id = previous; return; }}
            const listing = result.directory;
            const items = [{{title: '当前目录：' + listing.path, value: listing.id}}];
            if (listing.id !== '0') items.push({{title:'返回上一级', value:listing.parent_id}});
            if (listing.id !== '0' && listing.parent_id !== '0') items.push({{title:'根目录 /', value:'0'}});
            items.push(...listing.children);
            model._dir_items = items;
            model.destination_id = listing.id;
            model._dir_current = listing.id;
            model.destination_path = listing.path;
            model._dir_message = '已选择：' + listing.path + '。可继续选择子目录，最后点击保存生效。';
        }} catch (_) {{
            model.destination_id = previous;
            model._dir_message = '读取目录失败，请检查网络后重试。';
        }} finally {{ model._dir_busy = false; }}
    }}"""
