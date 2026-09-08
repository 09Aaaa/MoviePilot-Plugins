from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins.v3" / "tg115channel"


def _load_module(name: str, path: Path, *, package_path: Path | None = None):
    kwargs = {"submodule_search_locations": [str(package_path)]} if package_path else {}
    spec = importlib.util.spec_from_file_location(name, path, **kwargs)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def pure_modules():
    package_name = "_tg115_pure"
    package = types.ModuleType(package_name)
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules[package_name] = package
    models = _load_module(f"{package_name}.models", PLUGIN_DIR / "models.py")
    protocol = _load_module(f"{package_name}.protocol", PLUGIN_DIR / "protocol.py")
    p115 = _load_module(f"{package_name}.p115_transfer", PLUGIN_DIR / "p115_transfer.py")
    return types.SimpleNamespace(models=models, protocol=protocol, p115=p115)


@pytest.fixture(scope="session")
def plugin_module():
    app_module = types.ModuleType("app")
    plugins_module = types.ModuleType("app.plugins")
    plugins_module.__path__ = [str(ROOT / "plugins.v3")]
    sdk_module = types.ModuleType("app.sdk")
    logging_module = types.ModuleType("app.sdk.logging")
    media_module = types.ModuleType("app.sdk.media")

    class FakePluginBase:
        def __init__(self):
            self._test_data = {}

        def get_data(self, key=None, plugin_id=None):
            del plugin_id
            return self._test_data.get(key)

        def save_data(self, key, value, plugin_id=None):
            del plugin_id
            self._test_data[key] = value

    class FakeLogger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    class FakeTorrentInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    plugins_module._PluginBase = FakePluginBase
    logging_module.logger = FakeLogger()
    media_module.TorrentInfo = FakeTorrentInfo
    app_module.plugins = plugins_module
    app_module.sdk = sdk_module
    sdk_module.logging = logging_module
    sdk_module.media = media_module
    sys.modules.update(
        {
            "app": app_module,
            "app.plugins": plugins_module,
            "app.sdk": sdk_module,
            "app.sdk.logging": logging_module,
            "app.sdk.media": media_module,
        }
    )

    return _load_module(
        "app.plugins.tg115channel",
        PLUGIN_DIR / "__init__.py",
        package_path=PLUGIN_DIR,
    )
