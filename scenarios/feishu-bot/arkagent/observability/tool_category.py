"""把工具调用归类成可读标签。

方舟事件里外部调用几乎都是 ``bash``,真正的目标藏在 command 里;不归类的话看板上
33 次调用都叫 bash,没有信息量。这里是启发式,归类失败时退回 ``bash``。
"""
from __future__ import annotations

import os
import re
import shlex
from urllib.parse import urlparse

_SPLIT = re.compile(r"&&|\|\||;|\|")
_SKIP_HEAD = (
    "cd ", "export ", "set ", "source ", ". ", "mkdir ", "echo ",
    "for ", "while ", "if ", "then", "done", "fi",
)
_WRAPPERS = {"nohup", "time", "sudo", "env", "exec"}
_TWO_LEVEL = {"lark-cli", "gh", "git", "npm", "pip", "pip3"}
_PYTHON = {"python", "python3", "python3.11"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def categorize_tool(name: str, tool_input: object) -> str:
    if name != "bash":
        return name
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return "bash"
    return _categorize_command(command) or "bash"


def _categorize_command(command: str) -> str:
    for segment in _SPLIT.split(command):
        segment = segment.strip().removeprefix("do ").strip()  # for ...; do cmd
        if not segment or segment.startswith(_SKIP_HEAD):
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        tokens = _strip_prefix(tokens)
        if not tokens:
            continue
        return _label(tokens)
    return ""


def _strip_prefix(tokens: list[str]) -> list[str]:
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if _ENV_ASSIGN.match(token) or token in _WRAPPERS:
            i += 1
        elif token == "timeout" and i + 1 < len(tokens):
            i += 2
        else:
            break
    return tokens[i:]


def _label(tokens: list[str]) -> str:
    exe = os.path.basename(tokens[0])
    rest = [t for t in tokens[1:] if not t.startswith("-")]
    if exe in _PYTHON:
        if "-c" in tokens[1:]:
            return "python:-c"
        if not rest or rest[0].startswith(("<", "$")):
            return "python:inline"  # heredoc / 变量展开,拿不到脚本名
        return f"python:{os.path.basename(rest[0])}"
    if exe in _TWO_LEVEL:
        return f"{exe} {' '.join(rest[:2])}".strip()
    if exe in ("curl", "wget"):
        for token in tokens[1:]:
            host = urlparse(token).netloc
            if host:
                return f"{exe}:{host}"
    return exe
