#!/usr/bin/env python3
"""Check MoviePilot package metadata and plugin source version consistency."""

from __future__ import annotations

import argparse
import ast
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "Tg115Channel"
PLUGIN_FILE = ROOT / "plugins.v3" / "tg115channel" / "__init__.py"
INDEX_FILE = ROOT / "package.v3.json"
PROJECT_FILE = ROOT / "pyproject.toml"
PLUGIN_PROJECT_FILE = ROOT / "plugins.v3" / "tg115channel" / "pyproject.toml"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def source_version() -> str:
    tree = ast.parse(PLUGIN_FILE.read_text(encoding="utf-8"), filename=str(PLUGIN_FILE))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == PLUGIN_ID:
            for item in node.body:
                if not isinstance(item, ast.Assign):
                    continue
                if not any(isinstance(target, ast.Name) and target.id == "plugin_version" for target in item.targets):
                    continue
                if isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                    return item.value.value
    raise RuntimeError(f"{PLUGIN_ID}.plugin_version not found")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="", help="Optional release tag to validate")
    args = parser.parse_args()

    index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    metadata = index.get(PLUGIN_ID)
    if not isinstance(metadata, dict):
        raise RuntimeError(f"{PLUGIN_ID} missing from {INDEX_FILE.name}")
    index_version = str(metadata.get("version") or "")
    code_version = source_version()
    project = tomllib.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    project_version = str(project.get("project", {}).get("version") or "")
    plugin_project = tomllib.loads(PLUGIN_PROJECT_FILE.read_text(encoding="utf-8"))
    plugin_project_version = str(plugin_project.get("project", {}).get("version") or "")
    if code_version != index_version:
        raise RuntimeError(f"version mismatch: source={code_version}, index={index_version}")
    if project_version != index_version:
        raise RuntimeError(f"version mismatch: project={project_version}, index={index_version}")
    if plugin_project_version != index_version:
        raise RuntimeError(f"version mismatch: plugin project={plugin_project_version}, index={index_version}")
    if not SEMVER.fullmatch(index_version):
        raise RuntimeError(f"invalid semantic version: {index_version}")
    history = metadata.get("history") or {}
    if next(iter(history), None) != f"v{index_version}":
        raise RuntimeError("latest history entry must be first and match the current version")
    if args.tag and args.tag != f"{PLUGIN_ID}_v{index_version}":
        raise RuntimeError(f"tag {args.tag!r} does not match {PLUGIN_ID}_v{index_version}")
    print(f"{PLUGIN_ID} version {index_version} is consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
