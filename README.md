# claude-usage-indicator

A GNOME top-panel indicator that shows Claude Code usage (session % and
weekly % from `claude -p "/usage"`), refreshing every 30 seconds.

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
3. Install and enable `claude-usage-indicator.service` as a
   `systemctl --user` service tied to `graphical-session.target`, so it
   starts on login and stops on logout.

## Manual run (for debugging)

```bash
python3 indicator.py
```

## Configuration

Edit the `REFRESH_SECONDS` constant at the top of `indicator.py` (default
`30`) to change the polling interval, then restart the service:

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

- **Label stuck on `Session ?% · Week ?%`** — the `claude -p "/usage"` call
  failed, or its output format changed. Check `indicator.log` for the
  exception, and confirm the command still works directly:
  ```bash
  claude -p "/usage" --output-format json
  ```
- **No icon/label appears in the panel at all** — confirm the
  "Ubuntu AppIndicators" GNOME Shell extension is enabled:
  ```bash
  gnome-extensions list --enabled | grep appindicator
  ```
  It should list `ubuntu-appindicators@ubuntu.com`. If you just installed
  it, log out and back in.
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
