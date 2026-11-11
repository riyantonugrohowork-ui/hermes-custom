"""Hermes CLI — titik masuk utama.

Pakai:
  python -m hermes status
  python -m hermes chat            # mode interaktif di terminal
  python -m hermes run "instruksi" # sekali jalan
  python -m hermes telegram        # jalankan bot Telegram (long-polling)
  python -m hermes schedule        # jalankan scheduler task berjadwal
  python -m hermes web             # buka antarmuka web (localhost:10000)
  python -m hermes autopilot       # jalankan e-cuti otomatis (start + harian)
  python -m hermes tasks           # daftar task
"""
from __future__ import annotations

import json
import logging
import sys

from . import builtin_tools, config, runner, scheduler, tasks
from .brain import make_plan

log = logging.getLogger("hermes")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def _conn(cfg):
    from pathlib import Path

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    return tasks.init(Path(cfg.db_path))


def cmd_status(cfg) -> int:
    print(f"Hermes Agent v0.1.0")
    print(f"  Otak        : {cfg.brain_provider}")
    if cfg.brain_provider != "rule":
        has_key = {
            "openai": cfg.openai_api_key,
            "anthropic": cfg.anthropic_api_key,
            "google": cfg.google_api_key,
            "ollama": cfg.ollama_base_url,
        }.get(cfg.brain_provider)
        print(f"  Kredensial  : {'✅ terpasang' if has_key else '❌ kosong'}")
    print(f"  Portal web  : {'✅ ' + cfg.portal_base_url if cfg.portal_base_url else '— belum diisi'}")
    print(f"  Telegram    : {'✅' if cfg.telegram_bot_token else '— belum diisi'}")
    print(f"  Database    : {cfg.db_path}")
    print(f"  Output      : {cfg.output_dir}")
    return 0


def cmd_run(cfg, instruction: str) -> int:
    conn = _conn(cfg)
    registry = builtin_tools.build_registry(cfg, conn)
    task = tasks.create_task(conn, title=instruction[:40], instruction=instruction)
    print(f"🆔 Task {task.id}")
    result = runner.run_task(cfg, conn, registry, task.id)
    print(runner.summarize_result(result))
    if result.get("needs_confirmation"):
        print("\nJalankan: python -m hermes lanjut " + task.id)
    return 0 if result.get("ok") else 1


def cmd_lanjut(cfg, task_id: str) -> int:
    conn = _conn(cfg)
    registry = builtin_tools.build_registry(cfg, conn)
    result = runner.execute_pending(conn, registry, task_id)
    if result is None:
        print("Tidak ada rencana menunggu konfirmasi.")
        return 1
    print(runner.summarize_result(result))
    return 0


def cmd_recipes(cfg) -> int:
    """Daftar alur beserta jadwal & status terakhir."""
    from . import cron, recipes

    conn = _conn(cfg)
    rows = recipes.stats(conn)
    if not rows:
        print("Belum ada alur terdaftar.")
        return 0
    for r in rows:
        sched = r["schedule"] or "manual"
        nxt = ""
        if r["schedule"]:
            try:
                now = __import__("datetime").datetime.now()
                nxt = " → " + cron.next_run(r["schedule"], now).strftime("%d %b %H:%M")
            except Exception:  # noqa: BLE001
                nxt = ""
        status = r["last_status"] or "belum pernah"
        print(f"• {r['key']:16} {r['title']:24} [{sched}{nxt}]  {status}  {r['runs']}×")
    return 0


def cmd_recipe_run(cfg, key: str, inputs: dict | None = None) -> int:
    """Jalankan satu alur secara manual dari CLI."""
    from . import recipes

    conn = _conn(cfg)
    if recipes.get_recipe(key) is None:
        print(f"Alur '{key}' tidak dikenal.")
        return 2
    result = recipes.run_recipe(cfg, conn, key, inputs or {})
    print(runner.summarize_result(result))
    return 0 if bool(result.get("ok")) else 1


def cmd_tasks(cfg) -> int:
    conn = _conn(cfg)
    all_tasks = tasks.list_tasks(conn)
    if not all_tasks:
        print("Belum ada task.")
        return 0
    for t in all_tasks:
        flag = "🟢" if t.enabled else "⚪"
        print(f"{flag} {t.id}  {t.title}  [{t.schedule or 'sekali'}]")
    return 0


def cmd_chat(cfg) -> int:
    conn = _conn(cfg)
    registry = builtin_tools.build_registry(cfg, conn)
    print("Hermes CLI — ketik instruksi, /help untuk bantuan, /quit keluar.\n")
    while True:
        try:
            text = input("kamu> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSampai jumpa.")
            return 0
        if not text:
            continue
        if text in ("/quit", "/exit", "exit", "quit"):
            print("Sampai jumpa.")
            return 0
        if text == "/help":
            print("Ketik instruksi bahasa biasa. /tasks daftar, /quit keluar.")
            continue
        if text == "/tasks":
            for t in tasks.list_tasks(conn):
                print(f"  {t.id}  {t.title}")
            continue
        task = tasks.create_task(conn, title=text[:40], instruction=text)
        result = runner.run_task(cfg, conn, registry, task.id)
        print(f"hermes> {runner.summarize_result(result)}")
        if result.get("needs_confirmation"):
            print(f"hermes> ketik 'lanjut {task.id}' untuk setujui.")
        print()


def cmd_telegram(cfg) -> int:
    conn = _conn(cfg)
    from . import telegram_bot

    telegram_bot.poll_loop(cfg, conn)
    return 0


def cmd_schedule(cfg) -> int:
    conn = _conn(cfg)
    scheduler.loop(cfg, conn)
    return 0


def cmd_plan(cfg, instruction: str) -> int:
    """Tampilkan rencana tanpa mengeksekusi — untuk inspeksi."""
    conn = _conn(cfg)
    registry = builtin_tools.build_registry(cfg, conn)
    task = tasks.create_task(conn, title=instruction[:40], instruction=instruction)
    plan = make_plan(cfg, instruction, task.id, registry)
    print(f"Reasoning: {plan.reasoning}")
    print(f"Butuh konfirmasi: {plan.needs_confirmation}")
    print("Aksi:")
    for a in plan.actions:
        print(f"  - {a.tool}({json.dumps(a.args, ensure_ascii=False)})  # {a.description}")
    if not plan.actions:
        print("  (kosong)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = config.get_config()
    _setup_logging(cfg.log_level)

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    cmd = argv[0]
    rest = argv[1:]

    if cmd == "status":
        return cmd_status(cfg)
    if cmd == "chat":
        return cmd_chat(cfg)
    if cmd == "run" and rest:
        return cmd_run(cfg, " ".join(rest))
    if cmd == "plan" and rest:
        return cmd_plan(cfg, " ".join(rest))
    if cmd == "lanjut" and rest:
        return cmd_lanjut(cfg, rest[0])
    if cmd == "tasks":
        return cmd_tasks(cfg)
    if cmd == "recipes":
        return cmd_recipes(cfg)
    if cmd == "recipe" and len(rest) >= 2 and rest[0] == "run":
        key = rest[1]
        inputs = {}
        for pair in rest[2:]:
            if "=" in pair:
                k, _, v = pair.partition("=")
                inputs[k] = v
        return cmd_recipe_run(cfg, key, inputs)
    if cmd == "tick":
        from . import scheduler

        conn = _conn(cfg)
        out = scheduler.tick(cfg, conn)
        print(f"Recipe dijalankan: {len(out['recipes'])}, task: {len(out['tasks'])}")
        return 0
    if cmd == "telegram":
        return cmd_telegram(cfg)
    if cmd == "schedule":
        return cmd_schedule(cfg)
    if cmd == "web":
        from . import webui

        webui.serve(cfg)
        return 0
    if cmd == "autopilot":
        from . import autopilot

        autopilot.loop(cfg)
        return 0

    print(f"Perintah tidak dikenal: {cmd}\n")
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
