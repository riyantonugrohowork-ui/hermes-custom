"""Eksekusi portal web via Playwright headless.

Otak memberi instruksi bebas; runner membuka halaman, isi field, klik, dan ambil hasil.
Ini sengaja generik — bukan template kaku — supaya AI yang menentukan isian.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

STEP_HINT = """Kamu adalah operator browser. Ubah instruksi menjadi langkah Playwright JSON:
{"url": "path/URL tujuan", "steps": [
   {"act":"goto","url":"..."},
   {"act":"fill","selector":"css","value":"..."},
   {"act":"click","selector":"css"},
   {"act":"select","selector":"css","value":"..."},
   {"act":"screenshot","path":"nama.png"},
   {"act":"extract","selector":"css","attr":"innerText"},
   {"act":"wait","ms":500},
   {"act":"done","summary":"ringkasan hasil"}
]}
Balas HANYA JSON."""


def _run_steps(page, steps: list[dict]) -> dict:
    extracted: list = []
    summary = ""
    for s in steps:
        act = s.get("act")
        if act == "goto":
            page.goto(s["url"], wait_until="domcontentloaded")
        elif act == "fill":
            page.fill(s["selector"], str(s.get("value", "")))
        elif act == "click":
            page.click(s["selector"])
        elif act == "select":
            page.select_option(s["selector"], str(s.get("value", "")))
        elif act == "wait":
            page.wait_for_timeout(int(s.get("ms", 300)))
        elif act == "screenshot":
            page.screenshot(path=str(s.get("path", "screenshot.png")), full_page=True)
        elif act == "extract":
            el = page.query_selector(s["selector"])
            val = el.inner_text() if el else None
            if s.get("attr") and el:
                val = el.get_attribute(s["attr"])
            extracted.append({"selector": s["selector"], "value": val})
        elif act == "done":
            summary = s.get("summary", "")
    return {"summary": summary, "extracted": extracted}


def _plan_steps(cfg: "Config", instruction: str) -> dict:
    """Minta otak mengubah instruksi jadi langkah Playwright."""
    from . import brain

    provider = cfg.brain_provider
    prompt = f"{STEP_HINT}\n\nURL dasar: {cfg.portal_base_url}\n\nINSTRUKSI:\n{instruction}"

    if provider == "rule":
        # Tanpa LLM: tidak aman menebak selector. Jelaskan cara aktifkan LLM.
        return {
            "ok": False,
            "error": (
                "Mode 'rule' tidak bisa menebak selector web. "
                "Set HERMES_BRAIN_PROVIDER=openai|anthropic|google|ollama dan isi API key, "
                "lalu otak akan menyusun langkah Playwright otomatis."
            ),
        }

    import re

    if provider == "openai":
        import requests

        r = requests.post(
            f"{cfg.openai_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
            json={
                "model": cfg.openai_model,
                "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": instruction}],
                "temperature": 0,
            },
            timeout=120,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
    elif provider == "ollama":
        import requests

        r = requests.post(
            f"{cfg.ollama_base_url}/api/generate",
            json={"model": cfg.ollama_model, "prompt": prompt, "stream": False, "format": "json"},
            timeout=120,
        )
        r.raise_for_status()
        raw = r.json()["response"]
    elif provider == "anthropic":
        import requests

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": cfg.anthropic_api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": cfg.anthropic_model,
                "max_tokens": 2048,
                "system": prompt,
                "messages": [{"role": "user", "content": instruction}],
            },
            timeout=60,
        )
        r.raise_for_status()
        raw = r.json()["content"][0]["text"]
    elif provider == "google":
        import google.generativeai as genai

        genai.configure(api_key=cfg.google_api_key)
        raw = genai.GenerativeModel(cfg.google_model).generate_content(prompt).text
    else:
        return {"ok": False, "error": f"Provider '{provider}' tidak didukung untuk web."}

    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        return {"ok": False, "error": "Otak tidak menghasilkan langkah web yang valid."}
    return {"ok": True, "steps_plan": __import__("json").loads(text[start : end + 1])}


def run_web_fill(cfg: "Config", instruction: str) -> dict:
    planned = _plan_steps(cfg, instruction)
    if not planned.get("ok"):
        return planned
    return _execute(cfg, planned["steps_plan"])


def run_web_read(cfg: "Config", instruction: str) -> dict:
    return run_web_fill(cfg, instruction)


def _execute(cfg: "Config", steps_plan: dict) -> dict:
    from playwright.sync_api import sync_playwright

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    url = steps_plan.get("url") or cfg.portal_base_url
    steps = steps_plan.get("steps", [])
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()
            if url:
                page.goto(url, wait_until="domcontentloaded")
            result = _run_steps(page, steps)
            browser.close()
        return {"ok": True, **result}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
