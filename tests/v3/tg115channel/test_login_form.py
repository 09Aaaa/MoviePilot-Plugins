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
    assert any(control.get("text") == "发送登录验证码" for col in columns for control in col["content"])


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


def test_full_form_parses_with_moviepilot_visibility_contract(plugin_module):
    """MP FormRender writes style.display in prop order; CSS strings cannot receive it.

    Contract: MoviePilot-Frontend/src/components/render/FormRender.vue (v3).
    Exercise the complete schema, not only the login handler or individual expressions.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute the native form parser")
    form, defaults = plugin_module.Tg115Channel().get_form()
    script = r"""
'use strict';
const assert = require('node:assert/strict');
const {form, defaults} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const expression = (value, model) => new Function('model', 'with(model) { return ' + value + ' }')(model);
function props(raw, model) {
  const parsed = {};
  for (const [key, value] of Object.entries(raw)) {
    if (key === 'model') {
      parsed.modelValue = model[value];
    } else if (key === 'show' || key === 'v-show') {
      const visible = expression(value.slice(2, -2).trim(), model);
      if (!parsed.style) parsed.style = {};
      parsed.style.display = visible ? '' : 'none';
    } else if (key.startsWith('on')) {
      parsed[key] = new Function('model', 'event', 'with(model) { return (' + value + ')(event); }');
    } else if (typeof value === 'string' && value.startsWith('{{') && value.endsWith('}}')) {
      parsed[key] = expression(value.slice(2, -2).trim(), model);
    } else {
      parsed[key] = typeof value === 'string' && value in model ? model[value] : value;
    }
  }
  return parsed;
}
for (const state of ['logged_out', 'code_sent', 'password_needed', 'logged_in', 'unknown']) {
  const model = {...defaults, _tg_state: state, _tg_logged_in: state === 'logged_in'};
  const visible = new Set();
  function visit(item, hidden = false) {
    const parsed = props(item.props || {}, model);
    hidden = hidden || parsed.style?.display === 'none';
    if (item.props?.model && !hidden) visible.add(item.props.model);
    for (const child of item.content || []) visit(child, hidden);
  }
  for (const item of form) visit(item);
  assert(visible.has('telegram_api_id'));
  assert(visible.has('telegram_phone'));
  assert.equal(visible.has('resource_bot'), state === 'logged_in');
  assert.equal(visible.has('telegram_code'), state === 'code_sent');
  assert.equal(visible.has('telegram_password'), state === 'password_needed');
}
"""
    result = subprocess.run(
        [node, "-e", script],
        input=json.dumps({"form": form, "defaults": defaults}),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_buttons_have_native_slot_content(plugin_module):
    form, _ = plugin_module.Tg115Channel().get_form()
    buttons = []

    def visit(item):
        if item["component"] == "VBtn":
            # FormRender always supplies a default slot, so props.text is insufficient.
            assert item.get("text")
            buttons.append(item["text"])
        for child in item.get("content", []):
            visit(child)

    for item in form:
        visit(item)
    assert "发送登录验证码" in buttons
    assert "读取 / 刷新 115 目录" in buttons


def test_directory_event_navigates_without_save_and_rolls_back_errors(plugin_module):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    module = importlib.import_module(f"{plugin_module.__package__}.login_form")
    script = r"""
const assert = require('node:assert/strict');
const model = {_dir_busy:false, _dir_current:'0', destination_id:'0', p115_cookie:'test'};
const calls = [];
global.window = {MoviePilotAPI: {post: async (url, payload) => {
 calls.push(payload.directory_id);
 assert.equal(url, 'plugin/Tg115Channel/115/directories');
 if (payload.directory_id === 'missing') return {ok:false, message:'目录不存在'};
 return {ok:true, directory:{id:payload.directory_id, path:'/影视/电影', parent_id:'123', children:[]}};
}}};
const run = new Function('model', 'event', 'with(model) { return (' + HANDLER + ')(event); }');
(async () => {
 const child = '3438672442749353668';
 await run(model, child);
 assert.equal(model.destination_id, child);
 assert.equal(model._dir_items[1].value, '123');
 await run(model, '123');
 assert.equal(model.destination_id, '123');
 await run(model, 'missing');
 assert.equal(model.destination_id, '123');
 assert.equal(model._dir_busy, false);
 assert.deepEqual(calls, [child,'123','missing']);
})().catch(e => {console.error(e); process.exit(1);});
""".replace("HANDLER", json.dumps(module.directory_handler("Tg115Channel", selection=True)))
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True, timeout=10)


def test_directory_api_does_not_apply_target(plugin_module, monkeypatch):
    plugin = plugin_module.Tg115Channel()
    plugin.init_plugin({})
    before = dict(plugin._saved_config)
    monkeypatch.setattr(
        plugin_module.P115TransferService,
        "list_directory",
        lambda self, cid: {"id": cid, "path": "/电影", "parent_id": "0", "children": []},
    )
    response = plugin.directory_api({"directory_id": "123", "cookie": "test-cookie"})
    assert response["ok"]
    assert response["directory"]["id"] == "123"
    assert plugin._saved_config == before
    assert "test-cookie" not in str(response)
