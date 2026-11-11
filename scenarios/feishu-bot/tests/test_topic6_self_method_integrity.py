"""静态守卫：Topic6/Topicx 网关与 Runner 中的 `self.xxx(...)` 调用必须指向类内真实定义的方法或属性。

触发：merge 时删除 `_start_token_keepalive` 但漏删 gateway.py 的调用点，测试又给实例注入了同名方法，
导致 212 上 `/continue` 抛 AttributeError。此守卫不依赖运行时打桩，能在合并后立刻发现悬空调用。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted(
    [*(ROOT / "cases" / "topic6").glob("*.py"), *(ROOT / "cases" / "topicx").glob("*.py")]
)


def _dangling_self_calls(path: Path) -> list[tuple[str, str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems: list[tuple[str, str, int]] = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        if any(isinstance(b, ast.Name) and b.id == "Protocol" for b in cls.bases):
            continue
        defined = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        defined |= {t.id for n in cls.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
        if cls.bases and not all(isinstance(b, ast.Name) and b.id in {"object", "Exception", "RuntimeError"}
                                 for b in cls.bases):
            continue  # 继承自外部类的方法无法静态确认，跳过
        attrs = {t.attr for n in ast.walk(cls) if isinstance(n, (ast.Assign, ast.AnnAssign))
                 for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
                 if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"}
        for node in ast.walk(cls):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"):
                name = node.func.attr
                if name not in defined and name not in attrs:
                    problems.append((cls.name, name, node.lineno))
    return problems


@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_dangling_self_method_calls(path):
    assert _dangling_self_calls(path) == []
