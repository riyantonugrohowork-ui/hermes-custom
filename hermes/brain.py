"""Otak Hermes: ubah instruksi bahasa manusia -> Plan (rencana aksi).

Provider:
  - rule  : deterministik, tanpa API. Cocok untuk mulai & test. (default)
  - openai / anthropic / google / ollama : LLM sungguhan.

Kontrak keluaran LLM: JSON {reasoning, actions:[{tool,args,description}], needs_confirmation}
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from .tasks import Action, Plan

if TYPE_CHECKING:
    from .config import Config
    from .tools import ToolRegistry

SYSTEM_PROMPT = """Kamu adalah Hermes, agen otomasi. Ubah instruksi pengguna menjadi RENCANA AKSI.
Balas HANYA JSON valid, tanpa penjelasan di luar JSON:
{
  "reasoning": "alasan singkat",
  "needs_confirmation": false,
  "actions": [
    {"tool": "nama_tool", "args": {"...": "..."}, "description": "apa langkah ini lakukan"}
  ]
}
Gunakan HANYA tool yang tersedia (lihat daftar di bawah).
Isi `args` dengan nilai konkret dari instruksi — jangan minta pengguna mengisi form.
Jika instruksi tidak jelas atau berisiko (hapus data, submit final), set needs_confirmation=true.

PENTING tentang `reply`:
- Sapaan, basa-basi, pertanyaan tentang dirimu, atau obrolan biasa BUKAN tugas otomasi.
  Gunakan tool `reply` dengan kalimat ramah untuk membalasnya secara wajar.
- HANYA jika benar-benar tidak ada tool yang cocok DAN bukan obrolan biasa,
  barulah gunakan `reply` untuk meminta instruksi yang lebih jelas.
- Jangan pernah mengembalikan `actions` kosong — selalu pilih minimal satu tool.

TRIGGER ALUR KHUSUS (recipe):
- Jika pengguna meminta "rekap e-cuti", "ambil cuti", "export rekap cuti", atau
  "impor cuti", GUNAKAN tool `ecuti_rekap` (tanpa argumen). Alur ini rumit & tetap,
  jangan coba pecah jadi langkah lain.
"""

AVAILABLE_TOOLS_HEADER = "=== TOOL TERSEDIA ===\n"


class BrainError(Exception):
    pass


def _extract_json(text: str) -> dict:
    """Ambil objek JSON pertama dari teks (tahan markdown fence).

    Fallback: jika model menjawab teks polos (bukan JSON), bungkus sebagai `reply`
    supaya Hermes tetap membalas percakapan daripada error.
    """
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or start > end:
        # Model menjawab teks biasa — perlakukan sebagai balasan percakapan.
        return {"reasoning": "Balasan percakapan.", "actions": [{"tool": "reply", "args": {"message": text}}]}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {"reasoning": "Balasan percakapan.", "actions": [{"tool": "reply", "args": {"message": text}}]}


def _coerce_plan(task_id: str, data: dict, registry: "ToolRegistry") -> Plan:
    known = {t.name for t in registry.all()}
    actions = []
    for a in data.get("actions", []):
        if not isinstance(a, dict) or "tool" not in a:
            continue
        actions.append(
            Action(
                tool=a["tool"],
                args=a.get("args", {}) or {},
                description=a.get("description", ""),
            )
        )
    unknown = [a.tool for a in actions if a.tool not in known]
    if unknown:
        raise BrainError(f"Otak memakai tool tak dikenal: {', '.join(unknown)}")
    return Plan(
        task_id=task_id,
        reasoning=data.get("reasoning", ""),
        actions=actions,
        needs_confirmation=bool(data.get("needs_confirmation", False)),
    )


# ---------- Rule-based (default, tanpa API) ----------

def _rule_plan(instruction: str, task_id: str, registry: "ToolRegistry") -> Plan:
    """Heuristik sederhana: pilih tool dari kata kunci instruksi.

    Ini bukan AI — hanya supaya sistem jalan & teruji sebelum API key dipasang.
    """
    low = instruction.lower()
    known = {t.name for t in registry.all()}
    actions: list[Action] = []

    # Ambil blok bertanda '...' pertama sebagai teks/nilai utama jika ada.
    m = re.search(r"[\"'“”](.+?)[\"'“”]", instruction)
    quoted = m.group(1) if m else ""

    # Parse pola eksplisit "key: X, value: Y" / "kunci: X nilai: Y" untuk catatan.
    kv = re.search(r"key\s*[:=]\s*(.+?)[,;]\s*value\s*[:=]\s*(.+)$", instruction, re.IGNORECASE)
    if kv:
        note_key = kv.group(1).strip().strip("\"'“”")
        note_value = kv.group(2).strip().strip("\"'“”")
    else:
        note_key, note_value = None, None

    # Notifikasi HANYA jika ada kata kerja eksplisit kirim (bukan sekadar kata 'laporan').
    if re.search(r"\b(kirim|send|notif(ikasi)?kan|laporkan)\b", low) and "notify" in known:
        actions.append(Action("notify", {"message": quoted or instruction}, "Kirim notifikasi"))

    # Catatan: prioritas format key/value eksplisit.
    if (kv or re.search(r"\b(catat|simpan|rekam)\b", low)) and "save_note" in known:
        actions.append(
            Action(
                "save_note",
                {"key": note_key or "catatan", "value": note_value or quoted or instruction},
                "Simpan catatan",
            )
        )

    # Isi web HANYA pada instruksi eksplisit (portal/web/form), bukan sekadar kata 'value'.
    if re.search(r"\b(portal|web|form)\b", low) and re.search(r"\b(isi|input|isi form|buka|kirim)\b", low) and "web_fill" in known:
        actions.append(Action("web_fill", {"instruction": instruction}, "Isi portal web via Playwright"))

    if not actions:
        # fallback aman: tidak menebak, minta konfirmasi
        return Plan(
            task_id=task_id,
            reasoning="Mode rule: instruksi tidak cocok pola apa pun. Butuh LLM untuk memutuskan aksi.",
            actions=[],
            needs_confirmation=True,
        )

    return Plan(task_id=task_id, reasoning="Mode rule (pencocokan kata kunci).", actions=actions)


# ---------- LLM providers ----------

def _openai_plan(cfg: "Config", instruction: str, task_id: str, registry: "ToolRegistry") -> Plan:
    import requests

    resp = requests.post(
        f"{cfg.openai_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
        json={
            "model": cfg.openai_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT + AVAILABLE_TOOLS_HEADER + registry.describe()},
                {"role": "user", "content": instruction},
            ],
            "temperature": 0.2,
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return _coerce_plan(task_id, _extract_json(content), registry)


def _anthropic_plan(cfg: "Config", instruction: str, task_id: str, registry: "ToolRegistry") -> Plan:
    import requests

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": cfg.anthropic_api_key, "anthropic-version": "2023-06-01"},
        json={
            "model": cfg.anthropic_model,
            "max_tokens": 2048,
            "system": SYSTEM_PROMPT + AVAILABLE_TOOLS_HEADER + registry.describe(),
            "messages": [{"role": "user", "content": instruction}],
            "temperature": 0.2,
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["content"][0]["text"]
    return _coerce_plan(task_id, _extract_json(content), registry)


def _google_plan(cfg: "Config", instruction: str, task_id: str, registry: "ToolRegistry") -> Plan:
    import google.generativeai as genai

    genai.configure(api_key=cfg.google_api_key)
    model = genai.GenerativeModel(cfg.google_model)
    prompt = SYSTEM_PROMPT + AVAILABLE_TOOLS_HEADER + registry.describe() + "\n\nINSTRUKSI:\n" + instruction
    out = model.generate_content(prompt).text
    return _coerce_plan(task_id, _extract_json(out), registry)


def _ollama_plan(cfg: "Config", instruction: str, task_id: str, registry: "ToolRegistry") -> Plan:
    import requests

    resp = requests.post(
        f"{cfg.ollama_base_url}/api/generate",
        json={
            "model": cfg.ollama_model,
            "prompt": SYSTEM_PROMPT + AVAILABLE_TOOLS_HEADER + registry.describe() + "\n\nINSTRUKSI:\n" + instruction,
            "stream": False,
            "format": "json",
        },
        timeout=120,
    )
    resp.raise_for_status()
    return _coerce_plan(task_id, _extract_json(resp.json()["response"]), registry)


_PROVIDERS = {
    "openai": _openai_plan,
    "anthropic": _anthropic_plan,
    "google": _google_plan,
    "ollama": _ollama_plan,
}


def _history_block(history: list[dict] | None) -> str:
    """Susun riwayat percakapan jadi blok konteks untuk LLM."""
    if not history:
        return ""
    lines = ["", "=== RIWAYAT PERCAKAPAN SEBELUMNYA (task yang sama) ==="]
    for m in history[-12:]:  # batasi 12 pesan terakhir agar hemat token
        role = "PENGGUNA" if m.get("role") == "user" else "HERMES"
        lines.append(f"{role}: {m.get('content','')}")
    lines.append("=== AKHIR RIWAYAT — balas pesan PENGGUNA terbaru, pertahankan konteks di atas ===")
    return "\n".join(lines) + "\n"


def make_plan(
    cfg: "Config",
    instruction: str,
    task_id: str,
    registry: "ToolRegistry",
    history: list[dict] | None = None,
) -> Plan:
    provider = cfg.brain_provider
    instruction = instruction + _history_block(history)
    if provider == "rule":
        return _rule_plan(instruction, task_id, registry)
    fn = _PROVIDERS.get(provider)
    if fn is None:
        raise BrainError(f"Provider otak '{provider}' tidak dikenal.")
    try:
        return fn(cfg, instruction, task_id, registry)
    except BrainError:
        raise
    except Exception as e:  # noqa: BLE001
        raise BrainError(f"Endpoint otak gagal: {type(e).__name__}: {e}")
