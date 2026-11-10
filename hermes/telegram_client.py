"""Klien Telegram minimal (HTTP API langsung, tanpa dependency bot framework)."""
from __future__ import annotations

from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from .config import Config

API = "https://api.telegram.org/bot{token}/{method}"


def send_message(cfg: "Config", chat_id: int, text: str) -> dict:
    try:
        r = requests.post(
            API.format(token=cfg.telegram_bot_token, method="sendMessage"),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
        r.raise_for_status()
        return {"ok": True}
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}


def send_document(cfg: "Config", chat_id: int, path: str, caption: str = "") -> dict:
    try:
        with open(path, "rb") as f:
            r = requests.post(
                API.format(token=cfg.telegram_bot_token, method="sendDocument"),
                data={"chat_id": chat_id, "caption": caption},
                files={"document": f},
                timeout=120,
            )
        r.raise_for_status()
        return {"ok": True}
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}
