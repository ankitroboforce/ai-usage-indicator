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
