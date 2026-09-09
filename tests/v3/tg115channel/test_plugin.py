from __future__ import annotations

import inspect


def test_form_uses_consistent_grid_and_preserves_models(plugin_module):
    plugin = plugin_module.Tg115Channel()
    form, defaults = plugin.get_form()
    row = form[0]["content"][0]
    assert row["component"] == "VRow"
    assert row["props"]["style"] == "margin: 0;"
    models = []
    for column in row["content"]:
        assert column["component"] == "VCol"
        assert column["props"]["style"]["padding"] == "12px"
        for control in column["content"]:
            options = control.get("props", {})
            if "model" in options:
                models.append(options["model"])
            if options.get("type") == "password":
                assert options["autocomplete"] == "new-password"
    assert len(models) == len(set(models))
    assert set(models) == {key for key in defaults if not key.startswith(("_tg_", "_dir_"))}


class FakeTelegram:
    def __init__(self, resource):
        self.resource = resource
        self.calls = []

    def search(self, keyword):
        self.calls.append(keyword)
        return [self.resource]

    def stop(self):
        return None


class FailingTelegram:
    def search(self, _keyword):
        raise RuntimeError("offline")

    def stop(self):
        return None


class FakeTransfer:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def transfer(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _config():
    return {
        "enabled": True,
        "telegram_api_id": "12345",
        "telegram_api_hash": "hash",
        "telegram_session": "session",
        "resource_bot": "resource_bot",
        "search_template": "{keyword}",
        "p115_cookie": "UID=1; CID=2; SEID=3",
        "destination_path": "/影视/电影",
    }


def test_native_search_returns_opaque_resource_without_priority(plugin_module):
    plugin = plugin_module.Tg115Channel()
    plugin.init_plugin(_config())
    resource = plugin_module.BotResource(
        title="三体 S01 2160P",
        url="https://115.com/s/swexample?password=a1b2",
        access_code="a1b2",
        quality="4K",
        size=10 * 1024**3,
    )
    gateway = FakeTelegram(resource)
    plugin._telegram = gateway

    results = plugin.search_torrents(site={}, keyword="三体 S01", mtype="电视剧", page=0)

    assert len(results) == 1
    assert results[0].site_name == "TG115"
    assert results[0].site_downloader == "Tg115Channel"
    assert "pri_order" not in results[0].__dict__
    assert results[0].category == "电视剧"
    assert "115.com" not in results[0].enclosure
    assert gateway.calls == ["三体 S01"]

    cached = plugin.search_torrents(site={}, keyword="三体 S01", mtype="电视剧", page=0)
    assert len(cached) == 1
    assert gateway.calls == ["三体 S01"]


def test_download_settles_transfer_as_moviepilot_task(plugin_module):
    plugin = plugin_module.Tg115Channel()
    plugin.init_plugin(_config())
    plugin._telegram = FakeTelegram(
        plugin_module.BotResource(
            title="沙丘2 2024 2160P",
            url="https://115.com/s/swexample?password=a1b2",
            access_code="a1b2",
        )
    )
    torrent = plugin.search_torrents(site={}, keyword="沙丘2 2024", mtype="电影", page=0)[0]
    transfer = FakeTransfer(plugin_module.TransferResult(True, "115 转存成功", "/影视/电影"))
    plugin._p115 = transfer

    result = plugin.download(torrent.enclosure, download_dir="/unused")

    assert result is not None
    downloader, task_hash, layout, message = result
    assert downloader == "Tg115Channel"
    assert len(task_hash) == 40
    assert layout == "NoSubfolder"
    assert message == ""
    assert transfer.calls[0]["destination"] == "/影视/电影"


def test_foreign_download_token_is_not_claimed(plugin_module):
    plugin = plugin_module.Tg115Channel()
    plugin.init_plugin(_config())

    assert plugin.download("magnet:?xt=urn:btih:foreign", download_dir="/unused") is None


def test_search_failure_returns_empty_for_normal_site_fallback(plugin_module):
    plugin = plugin_module.Tg115Channel()
    plugin.init_plugin(_config())
    plugin._telegram = FailingTelegram()

    assert plugin.search_torrents(site={}, keyword="Alien", mtype="电影", page=0) == []
    assert "offline" in plugin._last_error


def test_error_messages_hide_credentials_and_share_links(plugin_module):
    plugin = plugin_module.Tg115Channel()
    plugin.init_plugin(_config())

    message = plugin._safe_message("failed https://115.com/s/private?password=a1b2 UID=1; CID=2; SEID=3 hash session")

    assert "115.com" not in message
    assert "password" not in message
    assert "UID=1" not in message
    assert "hash" not in message
    assert "session" not in message


def test_malformed_persisted_resource_is_ignored(plugin_module):
    plugin = plugin_module.Tg115Channel()
    plugin._test_data[plugin_module.RESOURCE_DATA_KEY] = {
        "a" * 64: {"discovered_at": "not-a-number"},
    }

    plugin.init_plugin(_config())

    assert plugin._resource_records == {}


def test_moviepilot_module_contract_signatures(plugin_module):
    plugin = plugin_module.Tg115Channel()
    plugin.init_plugin(_config())
    modules = plugin.get_module()

    assert set(modules) == {"search_torrents", "async_search_torrents", "download"}
    search_parameters = inspect.signature(modules["search_torrents"]).parameters
    download_parameters = inspect.signature(modules["download"]).parameters
    assert {"site", "keyword", "mtype", "page"} <= set(search_parameters)
    assert {"content", "download_dir", "cookie", "episodes", "category", "label", "downloader"} <= set(
        download_parameters
    )


def test_all_resources_remain_downloadable_above_old_caps(plugin_module):
    plugin = plugin_module.Tg115Channel()
    plugin.init_plugin(_config())
    resources = [
        plugin_module.BotResource(
            title=f"资源 {i}", url=f"https://115.com/s/share{i}?password=abcd", access_code="abcd"
        )
        for i in range(601)
    ]
    stored = plugin._store_search_results(cache_key="all", keyword="资源", media_type="movie", resources=resources)
    assert len(stored) == 601
    assert len(plugin._cached_resources("all")) == 601
    assert all(identifier in plugin._resource_records for identifier, _ in stored)
    assert plugin._destination("movie") == plugin._destination("tv") == plugin._destination("unknown")


def test_directory_selection_and_failed_navigation(plugin_module, monkeypatch):
    plugin = plugin_module.Tg115Channel()
    calls = []

    def listing(_self, directory_id, **_kwargs):
        calls.append(directory_id)
        if directory_id == "99":
            raise RuntimeError("目录已删除")
        return {
            "id": directory_id,
            "path": "/" if directory_id == "0" else "/电影",
            "parent_id": "0",
            "children": [{"title": "电影", "value": "12"}],
        }

    monkeypatch.setattr(plugin_module.P115TransferService, "list_directory", listing)
    plugin.init_plugin({**_config(), "destination_id": "0", "refresh_directories": True})
    assert plugin._config["destination_path"] == "/"
    plugin.init_plugin({**plugin._saved_config, "destination_id": "12"})
    assert plugin._config["destination_path"] == "/电影"
    plugin.init_plugin({**plugin._saved_config, "destination_id": "99"})
    assert plugin._config["destination_id"] == "12"
    assert plugin._config["destination_path"] == "/电影"
    assert calls == ["0", "12", "99"]
    assert "目录已删除" in plugin.get_data("directory_status")
