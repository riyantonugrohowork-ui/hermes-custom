"""Antarmuka tools yang bisa dipanggil otak."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    description: str
    args: dict[str, str]  # nama -> deskripsi
    func: Callable[..., Any]

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "args": self.args,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self, name: str, description: str, args: dict[str, str] | None = None
    ) -> Callable:
        args = args or {}

        def deco(func: Callable) -> Callable:
            self._tools[name] = Tool(name, description, args, func)
            return func

        return deco

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def call(self, name: str, kwargs: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Tool '{name}' tidak dikenal")
        return tool.func(**kwargs)

    def describe(self) -> str:
        lines = []
        for t in self._tools.values():
            argstr = ", ".join(f"{k}: {v}" for k, v in t.args.items())
            lines.append(f"- {t.name}({argstr}): {t.description}")
        return "\n".join(lines) if lines else "(belum ada tool)"
