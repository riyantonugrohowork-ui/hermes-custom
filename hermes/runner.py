"""Runner: eksekusi sebuah Task — susun plan lewat otak, jalankan tiap aksi."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from . import brain, tasks
from .tools import ToolRegistry

if TYPE_CHECKING:
    from .config import Config


def _log(conn: sqlite3.Connection, level: str, event: str, message: str, meta: dict | None = None) -> None:
    try:
        from . import db

        db.add_log(conn, level, event, message, meta)
    except Exception:  # noqa: BLE001 — logging tak boleh menggagalkan eksekusi
        pass


def run_task(cfg: "Config", conn: sqlite3.Connection, registry: ToolRegistry, task_id: str) -> dict:
    task = tasks.get_task(conn, task_id)
    if task is None:
        return {"ok": False, "error": f"Task '{task_id}' tidak ditemukan"}

    # Simpan pesan pengguna ke riwayat task, lalu ambil konteks SEBELUMNYA.
    # (pesan terbaru tidak ikut ke history karena sudah dikirim sebagai instruksi utama)
    tasks.add_message(conn, task_id, "user", task.instruction)
    history = tasks.get_messages(conn, task_id)[:-1]

    try:
        plan = brain.make_plan(cfg, task.instruction, task.id, registry, history=history)
    except brain.BrainError as e:
        result = {"ok": False, "error": f"Otak gagal menyusun rencana: {e}"}
        tasks.save_result(conn, task_id, result, status="error", error=result["error"])
        _log(conn, "error", "plan_failed", f"Task {task_id} gagal menyusun rencana", {"error": str(e)})
        return result

    tasks.save_plan(conn, task_id, plan)

    if plan.needs_confirmation:
        # Simpan rencana tertunda agar 'lanjut' bisa mengeksekusi tanpa menyusun ulang.
        tasks.set_pending(conn, task_id, {
            "reasoning": plan.reasoning,
            "actions": [a.__dict__ for a in plan.actions],
        })
        result = {
            "ok": False,
            "needs_confirmation": True,
            "plan": {"reasoning": plan.reasoning, "actions": [a.__dict__ for a in plan.actions]},
            "message": "Rencana butuh konfirmasi sebelum dijalankan.",
        }
        tasks.save_result(conn, task_id, result, status="partial")
        return result

    return _execute_plan(conn, registry, task_id, plan)


def execute_pending(conn: sqlite3.Connection, registry: ToolRegistry, task_id: str) -> dict | None:
    """Eksekusi rencana yang sebelumnya menunggu konfirmasi."""
    from .tasks import Plan, Action

    data = tasks.get_pending(conn, task_id)
    if not data:
        return None
    tasks.clear_pending(conn, task_id)
    actions = [Action(**a) for a in data.get("actions", [])]
    plan = Plan(task_id=task_id, reasoning=data.get("reasoning", ""), actions=actions)
    return _execute_plan(conn, registry, task_id, plan)


def _execute_plan(conn: sqlite3.Connection, registry: ToolRegistry, task_id: str, plan) -> dict:
    if not plan.actions:
        result = {"ok": False, "error": "Rencana kosong — otak tidak menghasilkan aksi."}
        tasks.save_result(conn, task_id, result, status="error", error=result["error"])
        return result

    outputs: list[dict] = []
    status = "ok"
    for action in plan.actions:
        try:
            out = registry.call(action.tool, action.args or {})
            out = out if isinstance(out, dict) else {"result": out}
            outputs.append({"tool": action.tool, "output": out})
            if out.get("ok") is False:
                status = "partial"
                _log(conn, "warn", "tool_error", f"Tool {action.tool} gagal",
                     {"task_id": task_id, "tool": action.tool, "error": out.get("error")})
            else:
                _log(conn, "success", "tool_ok", f"Tool {action.tool} sukses",
                     {"task_id": task_id, "tool": action.tool})
        except Exception as e:  # noqa: BLE001
            outputs.append({"tool": action.tool, "output": {"ok": False, "error": f"{type(e).__name__}: {e}"}})
            status = "partial"
            _log(conn, "error", "tool_exception", f"Tool {action.tool} exception",
                 {"task_id": task_id, "tool": action.tool, "error": str(e)})

    result = {
        "ok": status == "ok",
        "status": status,
        "reasoning": plan.reasoning,
        "plan": {"reasoning": plan.reasoning, "actions": [a.__dict__ for a in plan.actions]},
        "outputs": outputs,
    }
    tasks.save_result(conn, task_id, result, status=status)
    level = "success" if status == "ok" else "warn"
    _log(conn, level, "task_run", f"Task {task_id} selesai: {status}", {"task_id": task_id, "status": status})
    return result


def _fmt_tool_output(tool: str, out: dict) -> str:
    """Satu baris ringkas untuk output tool."""
    if "reply" in out:
        return out["reply"]
    if "xlsx" in out:
        s = f"  📄 Excel: {out['xlsx']}"
        if out.get("range"):
            s += f"  (rentang {out['range']})"
        return s
    if tool == "notify" and out.get("ok"):
        return "  📨 Notifikasi terkirim"
    if "path" in out:
        return f"  📄 {tool} → {out['path']}"
    if out.get("summary"):
        return f"  🌐 {out['summary']}"
    if out.get("ok") is False:
        return f"  ⚠️ {tool}: {out.get('error')}"
    return f"  • {tool} ok"


def summarize_result(result: dict) -> str:
    """Ringkas hasil untuk dibalas ke pengguna (Telegram/CLI)."""
    if not result.get("ok") and result.get("error"):
        msg = f"❌ {result['error']}"
        if result.get("log"):
            msg += "\n" + "\n".join(f"  • {x}" for x in result["log"])
        return msg
    if result.get("needs_confirmation"):
        lines = ["⚠️ Butuh konfirmasi sebelum jalan:", f"Alasan: {result.get('plan', {}).get('reasoning', '-')}"]
        for a in result.get("plan", {}).get("actions", []):
            desc = a.get("description") or a.get("tool")
            lines.append(f"  • {a.get('tool')}: {desc}")
        lines.append("\nBalas 'lanjut' untuk eksekusi.")
        return "\n".join(lines)

    # Hasil langsung dari recipe (bukan lewat outputs).
    if "outputs" not in result and ("xlsx" in result or "warning" in result):
        lines = ["✅ Selesai" if result.get("ok") else "⚠️ Sebagian selesai"]
        if "xlsx" in result:
            lines.append(f"  📄 Excel: {result['xlsx']}")
            if result.get("range"):
                lines.append(f"  📅 Rentang: {result['range']}")
        if result.get("warning"):
            lines.append(f"  ⚠️ {result['warning']}")
        if result.get("log"):
            lines.append("  Langkah: " + " → ".join(result["log"]))
        return "\n".join(lines)

    replies = [o["output"]["reply"] for o in result.get("outputs", [])
               if "reply" in o.get("output", {})]
    if replies and len(result.get("outputs", [])) == 1:
        # Murni percakapan — tampilkan balasannya saja, tanpa label status.
        return replies[0]

    lines = [f"✅ Status: {result.get('status', 'ok')}"]
    for o in result.get("outputs", []):
        lines.append(_fmt_tool_output(o["tool"], o.get("output", {})))
    return "\n".join(lines)
