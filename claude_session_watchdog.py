"""Claude Code session watchdog — alarm via custom transport if your session
silently dies (cluster recycle, OOM, crash, terminal hang, …).

Background: Claude Code's `ScheduleWakeup` is in-memory only. When the session
process dies, all pending wakeups die with it. There's no on-disk persistence,
no resume-on-restart, no remote-interrupt path. For long-running autonomous
loops this is a critical reliability gap — see
https://github.com/anthropics/claude-code/issues/61735 +
https://github.com/anthropics/claude-code/issues/51267

Workaround: run THIS script as a systemd timer / cron every 30 min. It checks
the mtime of one or more files your Claude session writes to regularly
(activity log, README, anything). If those files haven't been touched in
`--threshold-hours` (default 4 h) during waking hours, it fires an alarm via
your configured transport (default = print; wire your own Telegram/email/Slack).

License: MIT. Use, fork, embed in your own setup freely.

Author: @honzastim (Jan Štim), 2026-05-23. Workaround paired with
`claude_session_remote_interrupt.py` (the matching SIGINT-via-PID tool).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo


def _mtime_age_hours(p: Path, now: float) -> float | None:
    if not p.exists():
        return None
    return (now - p.stat().st_mtime) / 3600.0


def _load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {}


def _save_state(state_file: Path, last_alarm_ts: float) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"last_alarm_ts": last_alarm_ts}))


# ─── transports — wire your own; default is stdout print ──────────────────
def transport_print(msg: str) -> bool:
    """Default transport: just print. Replace this with your TG/email/Slack call."""
    print(msg)
    return True


def transport_telegram_bot(msg: str, *, bot_token: str, chat_id: str) -> bool:
    """Optional Telegram bot transport. Requires env BOT_TOKEN + CHAT_ID, or pass via args."""
    import urllib.parse, urllib.request
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": msg[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://api.telegram.org/bot{bot_token}/sendMessage", data=data
            ),
            timeout=10,
        )
        return True
    except Exception as e:
        print(f"[transport_telegram_bot] {e}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--watch", action="append", required=True,
        help="File whose mtime indicates session liveness. Pass multiple (--watch FILE1 --watch FILE2). Freshest wins.",
    )
    parser.add_argument("--threshold-hours", type=float, default=4.0,
                        help="Alarm if freshest signal is older than this many hours (default 4).")
    parser.add_argument("--waking-start", type=int, default=8,
                        help="Earliest hour to alarm (local TZ, default 8).")
    parser.add_argument("--waking-end", type=int, default=22,
                        help="Latest hour to alarm (local TZ, default 22).")
    parser.add_argument("--cooldown-hours", type=float, default=2.0,
                        help="Don't re-alarm within N hours (default 2).")
    parser.add_argument("--timezone", default="Europe/Prague",
                        help="Timezone for waking-hour check (default Europe/Prague).")
    parser.add_argument("--state-file", default="~/.cache/claude_session_watchdog_state.json",
                        help="Where to remember last alarm timestamp (cooldown enforcement).")
    parser.add_argument("--transport", choices=["print", "telegram"], default="print",
                        help="How to send the alarm. 'telegram' requires BOT_TOKEN + CHAT_ID env vars.")
    args = parser.parse_args()

    tz = ZoneInfo(args.timezone)
    now = _dt.datetime.now().timestamp()
    now_local = _dt.datetime.now(tz)
    state_file = Path(os.path.expanduser(args.state_file))

    ages = []
    for w in args.watch:
        age = _mtime_age_hours(Path(os.path.expanduser(w)), now)
        if age is not None:
            ages.append((w, age))

    if not ages:
        print(f"[watchdog] WARN no source files found ({args.watch}), skipping", file=sys.stderr)
        return 0

    freshest_path, freshest_age = min(ages, key=lambda x: x[1])
    in_waking = args.waking_start <= now_local.hour < args.waking_end

    print(f"[watchdog] {now_local:%Y-%m-%d %H:%M:%S %Z} | "
          + " / ".join(f"{w}={a:.1f}h" for w, a in ages)
          + f" | freshest {freshest_age:.1f}h | waking={in_waking}")

    if freshest_age < args.threshold_hours:
        print(f"[watchdog] OK — session active within {args.threshold_hours}h")
        return 0

    if not in_waking:
        print(f"[watchdog] outside waking hours ({args.waking_start}-{args.waking_end}), no alarm")
        return 0

    state = _load_state(state_file)
    last_alarm = state.get("last_alarm_ts", 0)
    if last_alarm and (now - last_alarm) / 3600.0 < args.cooldown_hours:
        print(f"[watchdog] cooldown active ({(now - last_alarm) / 3600.0:.1f}h < {args.cooldown_hours}h)")
        return 0

    msg = (
        "⚠️ <b>Claude Code session watchdog</b>\n\n"
        f"No liveness signal for {freshest_age:.1f}h+. Watched files:\n"
        + "\n".join(f"  {w}: {a:.1f}h ago" for w, a in ages)
        + "\n\nProbably the Claude session is stuck or terminated. "
        "If you have the matching remote-interrupt tool, run /claudes + /interrupt.\n\n"
        f"Cooldown: next alarm earliest in {args.cooldown_hours}h."
    )

    if args.transport == "telegram":
        token = os.environ.get("BOT_TOKEN")
        chat = os.environ.get("CHAT_ID")
        if not token or not chat:
            print("[watchdog] --transport telegram requires BOT_TOKEN + CHAT_ID env vars", file=sys.stderr)
            return 1
        ok = transport_telegram_bot(msg, bot_token=token, chat_id=chat)
    else:
        ok = transport_print(msg)

    if ok:
        _save_state(state_file, now)
        print(f"[watchdog] ALARM sent — freshest signal {freshest_age:.1f}h stale")
        return 0
    print("[watchdog] ALARM transport FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
