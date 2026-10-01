# AI Usage Tray (Windows)

Two notification-area icons showing how much of your **Claude Code** and **Codex** usage limits
you've used. Windows counterpart of the [GNOME panel indicator](../README.md) in this repo, reading
the same two data sources. It is a separate implementation (pystray tray icons instead of
AppIndicator) and shares no code with `indicator.py`.

## What you see

- **Badge** = the highest "% used" across that tool's limits (the one closest to running out).
  Orange square = Claude, black square = Codex. The stripe along the bottom is green under 50 %,
  amber from 50 % and red from 80 %.
- **Hover**: every limit, e.g. `Claude  Session 20% · Week 28%`, `Codex  Session 4% · Week 18% · Month 0%`.
- **Left-click** (or **Details…**): each limit with its reset time, Codex credits and plan.
- **Right-click**: Details…, Refresh now, Open log, Quit.
- Grey `?` = the last read failed (logged out, offline). Grey `!` = Claude polling paused for
  safety (see below). An icon is hidden while its CLI isn't installed.

Refreshes every 60 s. On Windows 11 the installer switches the icons to show on the taskbar
(instead of the `^` overflow); pass `-NoPin` to skip that. Windows keeps one switch per program, so
it appears as **Python** under Settings > Personalization > Taskbar > Other system tray icons, and
it also applies to any other tray icon run by the same `pythonw.exe`. On Windows 10, drag the icons
out of the overflow yourself.

## Where the numbers come from

- **Claude**: `claude.exe -p /usage --output-format json`. `/usage` is a local command that runs no
  model turn and costs nothing. It runs with `--safe-mode --no-session-persistence
  --setting-sources "" --strict-mcp-config --tools ""` from an empty folder, so it fires none of
  your hooks, loads no settings/plugins/MCP and writes no session transcript.
  **Safety pause:** if a call can't be shown to have run zero model turns (a turn or cost was
  reported, it timed out, the output wasn't readable, or the tray was killed mid-call), Claude
  polling stops until you click **Refresh now**. The pause is saved in
  `%LOCALAPPDATA%\ai-usage-tray\`, so it survives restarts and upgrades. `--model haiku --max-budget-usd 0.05` keep any accidental turn small.
- **Codex**: a short-lived `codex app-server` is asked `account/rateLimits/read` (the same lookup as
  `/status`). About 2 s, no model turn, no quota. Found on PATH or as the newest Codex desktop build
  under `%LOCALAPPDATA%\OpenAI\Codex\bin`.

## Requirements

- Windows 10/11.
- python.org Python 3.10+ (per-user or all-users install; the Microsoft Store build is not
  supported). `pystray` and `Pillow` are installed by the installer.
- Claude Code with the native `claude.exe` (npm `@anthropic-ai/claude-code` or the native
  installer). Tested with 2.1.280 on a Claude subscription. The locked-down flags need a recent
  version; on an older one the Claude icon shows `!` or `?`. API-key (pay-as-you-go) accounts print
  different `/usage` text and are untested.
- Codex: found as `codex.exe` on PATH, an npm install (`npm i -g @openai/codex`; the native exe
  inside the package is used, not the `codex.cmd` shim), or the Codex desktop app. Tested with the
  desktop app's CLI 0.155 on a team plan; the npm layout is handled per `@openai/codex` 0.159.
  For pnpm/bun installs set `codex_bin` (see Configuration).

Either tool may be missing; its icon simply stays hidden.

## Install / upgrade / remove

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1     # install or upgrade
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Python C:\path\to\python.exe
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -NoPin   # leave icons in the overflow
powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1   # remove
```

`install.ps1` installs pystray + Pillow, runs one test poll from a staged copy, stops the running
copy, swaps in the new one (the old one is kept in `%USERPROFILE%\.ai-usage-tray.old`), adds a
Startup-folder shortcut and starts it. If the new version doesn't start, it rolls back.

Runtime folder: `%USERPROFILE%\.ai-usage-tray` (`tray.log`, optional `config.json`). Safety state:
`%LOCALAPPDATA%\ai-usage-tray`. `uninstall.ps1` removes both, plus the Startup shortcut.

## Configuration

Optional `%USERPROFILE%\.ai-usage-tray\config.json`, read at start (Quit and relaunch, or re-run
install.ps1):

```json
{"refresh_seconds": 60, "claude_bin": "C:\\path\\to\\claude.exe", "codex_bin": "C:\\path\\to\\codex.exe"}
```

All keys optional. `refresh_seconds` minimum 15. Binaries must be `.exe` files (the npm
`claude.CMD` shim is never used).

## Troubleshooting

- Print one poll to the console:
  `python "%USERPROFILE%\.ai-usage-tray\ai_usage_tray.pyw" --once` (exit 0 = both read, 3 = a read failed, 4 = Claude paused for safety). It honours the safety
  pause and never clears it.
- `tray.log` records only failures and start/stop.
- Claude `?`: check `claude` still works and you're logged in. Codex `?`: run `codex login`.
- Grey `!` on Claude: read the reason under Details…, then Refresh now.

## Development

```powershell
python test_ai_usage_tray.py
```
