# claude-usage-indicator

GNOME top-panel indicators that show Claude Code and Codex usage side by
side, refreshing every 30 seconds:

```
[Claude icon] Session 9% · Week 4%    [OpenAI icon] Week 0% · Month 0%
```

All numbers are **% used**, so both tools read the same way. Click either
item for reset times, Codex credit counts, and "Refresh now".

## Icons

- **Claude** uses the `claude-desktop` icon installed by the Claude desktop
  app.
- **Codex** uses the white OpenAI "Blossom" logo. It's OpenAI's trademark,
  so it isn't in this repo: `install.sh` asks whether to download it from
  OpenAI's brand page (<https://openai.com/brand/>), and doing so means
  agreeing to OpenAI's Marks usage terms. The logo's canvas has a wide
  clear-space border that makes the mark only ~8px tall in the panel, so
  the installer trims the SVG's `viewBox` to the mark (the shape itself is
  unchanged) and saves it to
  `~/.local/share/claude-usage-indicator/icons/openai-blossom-trimmed.svg`.
  GNOME caches panel icons by file path for the whole login session, so if
  you ever change the icon file, give it a new name (and update
  `CODEX_ICON` in `indicator.py`) or log out and back in.

If either icon is missing, that item shows a generic terminal icon with the
word `Claude` or `Codex` in front of its numbers instead.

## Where the numbers come from

- **Claude**: `claude -p "/usage" --output-format json` (a local command;
  no tokens used).
- **Codex**: Codex has no non-interactive `/status`, so the indicator
  starts a short-lived `codex app-server` (Codex's JSON-RPC interface) and
  calls `account/rateLimits/read`, the same lookup `/status` does. It
  takes ~0.5s, runs no model turn, and uses no quota.
  - `Week` is the weekly rate-limit window (`usedPercent`). A 5-hour window
    shows as `Session` if your plan has one.
  - `Month` is the monthly credit limit. `/status` shows it as "% left";
    the indicator shows `100 − remaining` so it matches everything else.
  - `app-server` is marked experimental by Codex. If the call fails, the
    indicator falls back to the last rate-limit snapshot in
    `~/.codex/sessions/` and adds a **`*`** (e.g. `Week 0%*`). That snapshot is only as
    fresh as your last Codex turn and has no `Month` figure; a window whose
    reset time has passed is shown as 0%.

Built for Ubuntu 22.04 / GNOME Shell 42, using `AyatanaAppIndicator3`
(the cross-desktop StatusNotifierItem protocol) rather than a GNOME Shell
extension, so it isn't tied to GNOME's internal API surface and should
keep working across GNOME version upgrades.

## Install

```bash
./install.sh
```

This will:
1. `apt install gir1.2-ayatanaappindicator3-0.1` (the only missing system
   dependency; `python3-gi`, `gir1.2-gtk-3.0` are installed by default on
   Ubuntu GNOME).
2. Copy `indicator.py` to `~/.local/share/claude-usage-indicator/`.
3. Ask whether to download the OpenAI logo for the Codex item (see
   [Icons](#icons)). Set `INSTALL_OPENAI_LOGO=yes` or `=no` to skip the
   prompt; it's skipped automatically if the logo is already installed.
4. Install and enable `claude-usage-indicator.service` as a
   `systemctl --user` service tied to `graphical-session.target`, so it
   starts on login and stops on logout. Both panel items run from this one
   service. Re-running `install.sh` restarts it to pick up changes.

## Manual run (for debugging)

```bash
python3 indicator.py
```

## Configuration

Edit the `REFRESH_SECONDS` constant at the top of `indicator.py` (default
`30`) to change the polling interval, or `CLAUDE_BIN` / `CODEX_BIN` to
point at a specific binary, then restart the service:

```bash
systemctl --user restart claude-usage-indicator.service
```

## Logs

Errors are written to `~/.local/share/claude-usage-indicator/indicator.log`.

```bash
journalctl --user -u claude-usage-indicator.service -f
```

## Debugging

If the panel label isn't showing or isn't updating:

1. Check the service is actually running:
   ```bash
   systemctl --user status claude-usage-indicator.service
   ```
2. Check both logs for errors:
   ```bash
   journalctl --user -u claude-usage-indicator.service -n 50 --no-pager
   cat ~/.local/share/claude-usage-indicator/indicator.log
   ```
3. Stop the service and run it in the foreground to see errors live:
   ```bash
   systemctl --user stop claude-usage-indicator.service
   python3 ~/.local/share/claude-usage-indicator/indicator.py
   ```

Common issues:

- **Claude item shows `Session ?% · Week ?%`** — the `claude -p "/usage"` call
  failed, or its output format changed. Check `indicator.log` for the
  exception, and confirm the command still works directly:
  ```bash
  claude -p "/usage" --output-format json
  ```
- **An item shows a terminal icon and the word `Claude`/`Codex`** — its
  icon wasn't found. For Claude, the Claude desktop app isn't installed.
  For Codex, the OpenAI logo wasn't downloaded; re-run
  `INSTALL_OPENAI_LOGO=yes ./install.sh`.
- **No icon/label appears in the panel at all** — confirm the
  "Ubuntu AppIndicators" GNOME Shell extension is enabled:
  ```bash
  gnome-extensions list --enabled | grep appindicator
  ```
  It should list `ubuntu-appindicators@ubuntu.com`. If you just installed
  it, log out and back in.
- **Codex item ends in `*`** (e.g. `Week 0%*`) — the live `codex app-server` read failed and
  the numbers are from the session logs. The menu shows how old they are,
  and `indicator.log` has the exception. Common causes are a Codex update
  that changed the app-server protocol, or being logged out
  (`codex login`). To test the live read directly:
  ```bash
  python3 -c "import sys; sys.path.insert(0, '$HOME/.local/share/claude-usage-indicator'); import indicator; print(indicator.fetch_codex_rate_limits())"
  ```
- **Codex item shows `?%`** — both the live read and the log fallback
  failed (e.g. `codex` isn't installed or has never been used). Check
  `which codex`, or set `CODEX_BIN` at the top of `indicator.py` to the
  absolute path.
- **`Namespace AyatanaAppIndicator3 not available`** — the apt dependency
  is missing:
  ```bash
  sudo apt install gir1.2-ayatanaappindicator3-0.1
  ```
- **`claude: command not found` in the log** — the service can't resolve
  the `claude` binary. `systemctl --user` services do inherit your login
  `PATH` (including `~/.local/bin`), so this usually means `claude` isn't
  actually on `PATH` for a fresh login shell — check with `which claude`.
  As a fallback, hardcode the absolute path in the `CLAUDE_BIN` constant
  at the top of `indicator.py`.

## Disable / uninstall

Stop it for now (it restarts automatically on next login, since it's
still enabled):
```bash
systemctl --user stop claude-usage-indicator.service
```

Stop it and prevent it from auto-starting again:
```bash
systemctl --user disable --now claude-usage-indicator.service
```

Remove it entirely:
```bash
systemctl --user disable --now claude-usage-indicator.service
rm ~/.config/systemd/user/claude-usage-indicator.service
rm -rf ~/.local/share/claude-usage-indicator
systemctl --user daemon-reload
```
