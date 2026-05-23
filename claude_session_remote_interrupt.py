"""Claude Code session remote-interrupt helpers — send SIGINT (Ctrl+C
semantic) / SIGTERM / SIGKILL to a stuck Claude session by PID, from a remote
chat command (Telegram, Slack, your bot of choice).

Background: when a Claude Code CLI session hangs (mid-tool, stuck in a
ScheduleWakeup loop, OOM-on-the-edge, etc.), Remote Control mobile + claude.ai
input bridge often can't unstick it; only a local Ctrl+C does. TIOCSTI is
kernel-blocked on modern Linux (`sysctl dev.tty.legacy_tiocsti=0`) so we
cannot inject keystrokes into another pty. BUT `os.kill(pid, SIGINT)` to the
claude process IS the available equivalent of Ctrl+C in the TTY — Claude Code
reads SIGINT and breaks out of current operation.

Issues this works around:
  - https://github.com/anthropics/claude-code/issues/51267
  - https://github.com/anthropics/claude-code/issues/61735

Three command handlers exposed:
  cmd_list_claudes(chat_id, tg_send)         — list active claude sessions
  cmd_interrupt(chat_id, tg_send, text)      — SIGINT (Ctrl+C, soft)
  cmd_kill(chat_id, tg_send, text)           — SIGTERM (graceful)
  cmd_killhard(chat_id, tg_send, text)       — SIGKILL (force; requires explicit N)

Wire `tg_send(chat_id: int, text: str) -> bool` to your bot's send function.
Wire `cmd_list_claudes` etc. to your bot's command dispatcher. Example for
python-telegram-bot or a polling getUpdates() loop at the bottom of this file.

SECURITY:
  - Restrict command access to YOUR user_id only (your bot dispatcher's job)
  - `cmd_killhard` requires explicit N to avoid accidents
  - Refuses to signal the bot's own PID (would crash mid-handler)

License: MIT.
Author: @honzastim, 2026-05-23.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Callable


def _list_claude_processes() -> list[dict]:
    """Returns running claude CLI processes with PID, pts, age, CPU."""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,tty,etime,pcpu,cmd"],
            text=True, timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    procs = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, tty, etime, pcpu, cmd = parts
        # Match the claude CLI binary; skip grep results
        if not (cmd.startswith("claude ") or cmd == "claude" or " claude " in (" " + cmd + " ")):
            continue
        if "grep" in cmd:
            continue
        try:
            procs.append({
                "pid": int(pid),
                "pts": tty,
                "age": etime,
                "cpu_pct": float(pcpu),
                "cmd": cmd[:80],
            })
        except ValueError:
            pass
    return sorted(procs, key=lambda p: p["pid"])


def _format_claude_list(procs: list[dict]) -> str:
    if not procs:
        return "🛑 No claude processes running."
    lines = ["🤖 <b>Active Claude sessions</b>"]
    for i, p in enumerate(procs, 1):
        lines.append(
            f"  <b>{i}</b>. PID <code>{p['pid']}</code> on <code>{p['pts']}</code> "
            f"| age {p['age']} | CPU {p['cpu_pct']:.1f}% | <code>{p['cmd']}</code>"
        )
    lines.append("")
    lines.append("<b>Commands:</b>")
    lines.append("  /interrupt N        — SIGINT (Ctrl+C semantic, soft)")
    lines.append("  /kill N             — SIGTERM (graceful)")
    lines.append("  /killhard N         — SIGKILL (force; requires explicit N)")
    return "\n".join(lines)


def _self_pid() -> int:
    return os.getpid()


def _resolve_target(procs: list[dict], n: int | None) -> tuple[dict | None, str | None]:
    """Resolve N (1-indexed) to a process. If n is None, default = first
    non-self claude (the oldest is usually the one stuck)."""
    if not procs:
        return None, "No claude processes running."
    if n is None:
        my_pid = _self_pid()
        non_self = [p for p in procs if p["pid"] != my_pid]
        if not non_self:
            return None, "Only the bot/handler itself is visible — refusing to suicide."
        return non_self[0], None
    if n < 1 or n > len(procs):
        return None, f"N must be 1..{len(procs)}."
    return procs[n - 1], None


def _parse_n(text: str) -> int | None:
    parts = text.strip().split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _send_signal(chat_id: int, tg_send: Callable[[int, str], bool],
                 text: str, sig: int, sig_name: str) -> None:
    procs = _list_claude_processes()
    n = _parse_n(text)
    target, err = _resolve_target(procs, n)
    if err:
        tg_send(chat_id, f"⛔ {err}\n\n" + _format_claude_list(procs))
        return
    if target["pid"] == _self_pid():
        tg_send(chat_id, "⛔ Refusing to signal myself (the handler would crash).")
        return
    try:
        os.kill(target["pid"], sig)
        time.sleep(0.5)
        try:
            os.kill(target["pid"], 0)
            alive = True
        except ProcessLookupError:
            alive = False
        tg_send(
            chat_id,
            f"📡 <b>{sig_name} → PID {target['pid']}</b> ({target['pts']})\n"
            f"  cmd: <code>{target['cmd']}</code>\n"
            f"  age: {target['age']}\n"
            f"  alive after signal: {'yes' if alive else 'no (terminated)'}\n\n"
            "If the claude was stuck, it should now wake (SIGINT) or terminate (SIGTERM/SIGKILL)."
        )
    except PermissionError:
        tg_send(chat_id, f"⛔ PermissionError — claude PID {target['pid']} runs under another user.")
    except ProcessLookupError:
        tg_send(chat_id, f"⛔ PID {target['pid']} no longer exists (race).")


def cmd_list_claudes(chat_id: int, tg_send: Callable[[int, str], bool]) -> None:
    """/claudes — list active claude sessions."""
    tg_send(chat_id, _format_claude_list(_list_claude_processes()))


def cmd_interrupt(chat_id: int, tg_send: Callable[[int, str], bool], text: str = "/interrupt") -> None:
    """/interrupt [N] — SIGINT to N-th session (default = oldest non-self)."""
    _send_signal(chat_id, tg_send, text, signal.SIGINT, "SIGINT")


def cmd_kill(chat_id: int, tg_send: Callable[[int, str], bool], text: str = "/kill") -> None:
    """/kill [N] — SIGTERM (graceful)."""
    _send_signal(chat_id, tg_send, text, signal.SIGTERM, "SIGTERM")


def cmd_killhard(chat_id: int, tg_send: Callable[[int, str], bool], text: str = "/killhard") -> None:
    """/killhard N — SIGKILL (force; requires explicit N — no default)."""
    n = _parse_n(text)
    if n is None:
        tg_send(chat_id, "⛔ /killhard requires explicit N (no default — too destructive).")
        return
    _send_signal(chat_id, tg_send, text, signal.SIGKILL, "SIGKILL")


# ─── Minimal Telegram bot dispatcher example ────────────────────────────────
# Adapt to python-telegram-bot, aiogram, or your own polling loop.
def _example_telegram_dispatcher() -> None:
    """Reference dispatcher using urllib + getUpdates polling. Replace the
    hardcoded auth check with your own user-id gate."""
    import json, urllib.parse, urllib.request

    TG_BOT_TOKEN = os.environ["BOT_TOKEN"]
    AUTHORIZED_USER = int(os.environ["AUTHORIZED_USER"])  # YOUR user_id
    API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

    def tg_send(chat_id: int, text: str) -> bool:
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": text[:4000],
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"{API}/sendMessage", data=data), timeout=10
            )
            return True
        except Exception as e:
            print(f"[tg_send] {e}", file=sys.stderr)
            return False

    COMMANDS = {
        "/claudes": cmd_list_claudes,
        "/interrupt": cmd_interrupt,
        "/kill": cmd_kill,
        "/killhard": cmd_killhard,
    }

    offset = 0
    while True:
        try:
            resp = urllib.request.urlopen(f"{API}/getUpdates?offset={offset}&timeout=30", timeout=35)
            updates = json.loads(resp.read()).get("result", [])
        except Exception:
            time.sleep(5); continue

        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message")
            if not msg or msg["from"]["id"] != AUTHORIZED_USER:
                continue
            text = msg.get("text", "").strip()
            cmd = text.split()[0].split("@")[0].lower() if text else ""
            handler = COMMANDS.get(cmd)
            if not handler:
                continue
            try:
                if cmd in ("/interrupt", "/kill", "/killhard"):
                    handler(msg["chat"]["id"], tg_send, text)
                else:
                    handler(msg["chat"]["id"], tg_send)
            except Exception as e:
                tg_send(msg["chat"]["id"], f"❌ {cmd} failed: <code>{e}</code>")


if __name__ == "__main__":
    # CLI smoke test: just print the active claude sessions
    print(_format_claude_list(_list_claude_processes()))
