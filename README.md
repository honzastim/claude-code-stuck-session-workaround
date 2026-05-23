# claude-code-stuck-session-workaround

Workaround scripts for the Claude Code silent-stuck-session reliability gap, tracked upstream as:

- [#51267 — Remote Control: session silently hangs mid-execution](https://github.com/anthropics/claude-code/issues/51267) (parent issue, open since 2026-04-20)
- [#61735 — ScheduleWakeup non-persistent + no remote interrupt](https://github.com/anthropics/claude-code/issues/61735) (ScheduleWakeup-specific facet)
- Companion docs PR: [#61737](https://github.com/anthropics/claude-code/pull/61737)

## Problem

Claude Code's `ScheduleWakeup` (the mechanism that powers `/loop` dynamic mode and similar self-paced agentic loops) stores its pending wakeup queue **in process memory only**. There is no on-disk persistence, no resume-on-restart, no remote-interrupt API.

When the Claude Code CLI session dies for any reason (cluster recycle, OOM, terminal hang, crash), all pending wakeups die with the process. The session is silently stuck — and from a remote location (mobile, another machine), there is no way to send a Ctrl+C-equivalent to wake it.

A real-world cost: a 3-day silent gap on a vacation cost the author a Max plan subscription (which is why this exists).

## What's here

| File | Purpose |
|---|---|
| `claude_session_watchdog.py` | Systemd-timer-friendly script that watches an artifact file mtime (chronicle, log, any file Claude writes to). Alarms via Telegram or print if the freshest signal is older than `--threshold-hours` during waking hours. Cooldown enforced so it doesn't spam. |
| `claude_session_remote_interrupt.py` | Telegram bot command handlers: `/claudes` (list active claude PIDs), `/interrupt N` (SIGINT, Ctrl+C semantic), `/kill N` (SIGTERM), `/killhard N` (SIGKILL, requires explicit N). Self-protection refuses to signal the bot's own PID. |

## Why SIGINT and not Esc

TIOCSTI keystroke injection is **kernel-blocked on modern Linux** (`sysctl dev.tty.legacy_tiocsti=0`), so we cannot inject `\x1b` or `\x03` into another pty from a script. `os.kill(pid, signal.SIGINT)` is the available equivalent — Claude Code reads SIGINT and breaks out of the current operation, equivalent to a local Ctrl+C in the TTY.

## Quick start

### 1. Watchdog (systemd timer)

```bash
# Pick any file your Claude session writes to regularly:
$ python3 claude_session_watchdog.py \
    --watch ~/my-project/CHANGELOG.md \
    --watch ~/my-project/work-log.md \
    --threshold-hours 4 \
    --transport telegram

# With Telegram transport: set env first
$ export BOT_TOKEN=...; export CHAT_ID=...
```

Wrap in a systemd `--user` timer firing every 30 min — example .service + .timer in `examples/`.

### 2. Remote-interrupt (Telegram bot)

```bash
$ export BOT_TOKEN=...; export AUTHORIZED_USER=<your_telegram_user_id>
$ python3 -c "from claude_session_remote_interrupt import _example_telegram_dispatcher; _example_telegram_dispatcher()"
```

Then from any Telegram client:

```
/claudes
/interrupt        # default: oldest non-self claude PID
/interrupt 2      # explicit: 2nd in /claudes list
/kill 1
/killhard 1
```

Or wire into your existing python-telegram-bot / aiogram bot via the per-command handlers (`cmd_list_claudes`, `cmd_interrupt`, …).

## Security notes

- **No hardcoded credentials.** Tokens via env vars (`BOT_TOKEN`, `AUTHORIZED_USER`, `CHAT_ID`).
- **Authorization is YOUR job.** The example dispatcher gates on `AUTHORIZED_USER` env; if you build something fancier, enforce user_id check before dispatching `/interrupt` etc.
- **Self-protection.** `claude_session_remote_interrupt.py` refuses to signal the bot's own PID.
- **No privilege escalation surface.** SIGINT to processes you already own; never elevates.
- **No daemon, no socket.** Just a polling bot and a oneshot watchdog timer.

## Compatibility

- Linux (uses `ps -eo`, `os.kill`, systemd user timers)
- Python 3.11+ (uses `zoneinfo`)
- Modern kernels where TIOCSTI is disabled — but the SIGINT mechanism doesn't depend on TIOCSTI

## License

MIT. Use freely.

## Credits

- @honzastim (Jan Štim) — author, 2026-05-23
- @giruuuuj — for the documentation PR (#61737) and confirming distinct-facet framing
- The other reporters on #51267 (@InsaneTrilobyte, @alex-rosenberg35, @ntabor82, @davidwyly, @GustavoVzla) — same problem, validates this isn't a one-off

If you're seeing the same bug: please 👍 #51267 + #61735 to raise upstream priority for a proper fix.
