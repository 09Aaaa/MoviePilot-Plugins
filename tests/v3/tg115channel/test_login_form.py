from __future__ import annotations

import importlib
import json
import shutil
import subprocess

import pytest


def test_form_groups_and_removes_manual_session(plugin_module):
    plugin = plugin_module.Tg115Channel()
    form, defaults = plugin.get_form()
    columns = form[0]["content"][0]["content"]
    titles = [
        control.get("text") for col in columns for control in col["content"] if control["component"] == "VCardTitle"
    ]
    assert titles == ["Telegram 账号登录", "资源 Bot 设置", "115 转存设置"]
    controls = {
        control.get("props", {}).get("model"): (col, control)
        for col in columns
        for control in col["content"]
        if "model" in control.get("props", {})
    }
    assert "telegram_session" not in controls
    assert "telegram_login_action" not in controls
    assert "_tg_logged_in" in controls["resource_bot"][0]["props"]["show"]
    assert "password_needed" in controls["telegram_password"][0]["props"]["show"]
    assert defaults["_tg_logged_in"] is False
    assert any(
        control.get("props", {}).get("text") == "发送登录验证码" for col in columns for control in col["content"]
    )


def test_button_event_runs_with_host_model_contract(plugin_module):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute the native form event")
    module = importlib.import_module(f"{plugin_module.__package__}.login_form")
    script = """
const assert = require('node:assert/strict');
const handlerSource = HANDLER;
const model = {_tg_busy:false, telegram_api_id:'123', telegram_api_hash:'hash',
  telegram_phone:'+8613800138000', telegram_code:'12345', telegram_password:'secret'};
let calls = 0;
global.window = {MoviePilotAPI: {post: async (url, payload) => {
  calls++;
  assert.equal(url, 'plugin/Tg115Channel/telegram/login');
  assert.equal(payload.action, 'verify');
  return {state:'logged_in', logged_in:true, message:'已登录'};
}}};
const run = new Function('model', 'event', 'with(model) { return (' + handlerSource + ')(event); }');
(async () => {
 await run(model, {});
 assert.equal(calls, 1);
 assert.equal(model._tg_logged_in, true);
 assert.equal(model.telegram_password, '');
 assert.equal(model.telegram_code, '');
 assert.equal(model._tg_busy, false);
 const unlocked = new Function('model', 'return ' + EXPRESSION);
 assert.equal(unlocked(model), true);
 model.telegram_api_hash = 'changed';
 assert.equal(unlocked(model), false);
 model._tg_busy = true;
 await run(model, {});
 assert.equal(calls, 1);
 model._tg_busy = false;
 window.MoviePilotAPI.post = async () => { throw new Error('network'); };
 await run(model, {});
 assert.equal(model._tg_logged_in, false);
 assert.equal(model._tg_busy, false);
})().catch(e => { console.error(e); process.exit(1); });
""".replace("HANDLER", json.dumps(module.login_handler("Tg115Channel", "verify"))).replace(
        "EXPRESSION", json.dumps(module.BOT_UNLOCKED)
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True, timeout=10)
