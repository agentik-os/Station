from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "overlay/hermes/plugins/platforms/discord/adapter.py"


def _slash_command_names() -> list[str]:
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
    register = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_register_slash_commands"
    )
    names: list[str] = []
    for node in ast.walk(register):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "tree"
                and func.attr == "command"
            ):
                continue
            name = next(
                (
                    keyword.value.value
                    for keyword in decorator.keywords
                    if keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ),
                node.name,
            )
            names.append(name)
    return names


def test_each_native_slash_command_is_registered_once() -> None:
    names = _slash_command_names()
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == [], f"duplicate Discord slash registrations: {duplicates}"
