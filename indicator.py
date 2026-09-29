#!/usr/bin/env python3
"""GNOME top panel indicator showing Claude Code (`claude -p "/usage"`) and
Codex (`codex app-server` account/rateLimits/read) usage."""

import json
import logging
import os
import re
import select
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3, GLib, Gtk

REFRESH_SECONDS = 30
CLAUDE_BIN = "claude"
CODEX_BIN = "codex"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
STATE_DIR = Path.home() / ".local" / "share" / "claude-usage-indicator"
LOG_FILE = STATE_DIR / "indicator.log"
APP_ID = "claude-usage-indicator"
CODEX_APP_ID = "codex-usage-indicator"

# Claude's icon comes from the Claude desktop app's icon theme entry. The
# OpenAI logo isn't redistributed in this repo; install.sh offers to
# download it into ICON_DIR. Either missing -> fall back to a text prefix.
CLAUDE_ICON = "claude-desktop"
ICON_DIR = STATE_DIR / "icons"
# GNOME Shell caches panel icons by file path for the whole login session,
# so a changed icon needs a new name to show up without logging out.
CODEX_ICON = "openai-blossom-trimmed"
FALLBACK_ICON = "utilities-terminal-symbolic"

STATE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

SESSION_RE = re.compile(r"Current session:\s*(\d+)% used(?:[^\n]*?resets ([^\n(]+))?")
WEEK_RE = re.compile(r"Current week \(all models\):\s*(\d+)% used(?:[^\n]*?resets ([^\n(]+))?")

# Codex reports rate-limit windows by duration; name the ones that line up
# with Claude's so the label reads the same for both.
CODEX_WINDOW_NAMES = {300: "Session", 10080: "Week"}


# --- Claude ------------------------------------------------------------------

def fetch_usage_text():
    result = subprocess.run(
        [CLAUDE_BIN, "-p", "/usage", "--output-format", "json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    result.check_returncode()
    payload = json.loads(result.stdout)
    return payload["result"]


def parse_usage(text):
    session = SESSION_RE.search(text)
    week = WEEK_RE.search(text)
    if not session or not week:
        raise ValueError(f"could not parse /usage output: {text!r}")
    return {
        "session_pct": session.group(1),
        "session_reset": (session.group(2) or "").strip(),
        "week_pct": week.group(1),
        "week_reset": (week.group(2) or "").strip(),
        "raw": text,
    }


# --- Codex -------------------------------------------------------------------

def fetch_codex_rate_limits(timeout=15):
    """Ask a short-lived `codex app-server` for account/rateLimits/read.

    stdin has to stay open until the response arrives: the server exits on
    EOF without answering pending requests.
    """
    proc = subprocess.Popen(
        [CODEX_BIN, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        requests = [
            {"id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": APP_ID, "version": "1"}}},
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read",
             "params": {"excludeResetCreditDetails": True}},
        ]
        proc.stdin.write("".join(json.dumps(r) + "\n" for r in requests).encode())
        proc.stdin.flush()

        fd = proc.stdout.fileno()
        buf = b""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("codex app-server did not answer account/rateLimits/read")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RuntimeError(f"codex app-server exited early (code {proc.poll()})")
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                if not line.strip():
                    continue
                msg = json.loads(line)
                if msg.get("id") != 2:
                    continue
                if "error" in msg:
                    raise RuntimeError(f"account/rateLimits/read failed: {msg['error']}")
                return msg["result"]
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _codex_window(name_mins, used_pct, resets_at):
    name = CODEX_WINDOW_NAMES.get(name_mins) or f"{name_mins}m"
    return {"name": name, "mins": name_mins or 0, "pct": round(used_pct),
            "resets_at": resets_at}


def parse_codex_live(result):
    rl = result["rateLimits"]
    windows = [
        _codex_window(w.get("windowDurationMins"), w["usedPercent"], w.get("resetsAt"))
        for w in (rl.get("primary"), rl.get("secondary")) if w
    ]
    month = None
    individual = rl.get("individualLimit")
    if individual and individual.get("remainingPercent") is not None:
        # /status shows this one as "% left"; flip it to "% used" like the others.
        month = {
            "pct": 100 - individual["remainingPercent"],
            "used": individual.get("used"),
            "limit": individual.get("limit"),
            "resets_at": individual.get("resetsAt"),
        }
    return {
        "windows": sorted(windows, key=lambda w: w["mins"]),
        "month": month,
        "plan": rl.get("planType"),
        "stale_since": None,
    }


def read_codex_from_logs():
    """Fallback: last rate_limits snapshot Codex wrote to its session logs.

    Only as fresh as your last Codex turn, and doesn't include the monthly
    credit limit.
    """
    files = sorted(CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:10]:
        for line in reversed(path.read_text(errors="replace").splitlines()):
            if '"rate_limits"' not in line:
                continue
            entry = json.loads(line)
            rl = (entry.get("payload") or {}).get("rate_limits")
            if not rl:
                continue
            now = time.time()
            windows = []
            for w in (rl.get("primary"), rl.get("secondary")):
                if not w:
                    continue
                # A window that has reset since the snapshot is back to 0%.
                pct = 0 if (w.get("resets_at") or 0) < now else w["used_percent"]
                windows.append(_codex_window(w.get("window_minutes"), pct, w.get("resets_at")))
            return {
                "windows": sorted(windows, key=lambda w: w["mins"]),
                "month": None,
                "plan": rl.get("plan_type"),
                "stale_since": entry.get("timestamp"),
            }
    raise FileNotFoundError(f"no rate_limits snapshot in {CODEX_SESSIONS_DIR}")


def fetch_codex_usage():
    try:
        return parse_codex_live(fetch_codex_rate_limits())
    except Exception:
        logging.exception("codex live rate-limit read failed; falling back to session logs")
    return read_codex_from_logs()


def format_reset(epoch):
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch).strftime("%-I:%M %p on %-d %b")


# --- Indicator ---------------------------------------------------------------

def claude_icon():
    """(icon name, theme path, text prefix) for the Claude indicator."""
    if Gtk.IconTheme.get_default().has_icon(CLAUDE_ICON):
        return CLAUDE_ICON, None, ""
    return FALLBACK_ICON, None, "Claude "


def codex_icon():
    """(icon name, theme path, text prefix) for the Codex indicator."""
    if (ICON_DIR / f"{CODEX_ICON}.svg").is_file():
        return CODEX_ICON, str(ICON_DIR), ""
    return FALLBACK_ICON, None, "Codex "


class ToolIndicator:
    """One panel item: an icon plus a text label, with its own menu."""

    def __init__(self, app_id, icon, on_refresh, on_quit):
        icon_name, theme_path, self.prefix = icon
        self.indicator = AyatanaAppIndicator3.Indicator.new(
            app_id,
            icon_name,
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        if theme_path:
            self.indicator.set_icon_theme_path(theme_path)
            self.indicator.set_icon_full(icon_name, app_id)
        self.indicator.set_label(f"{self.prefix}…", "")
        self.set_visible(False)

        self.detail_item = Gtk.MenuItem(label="Loading…")
        self.detail_item.set_sensitive(False)

        menu = Gtk.Menu()
        menu.append(self.detail_item)
        menu.append(Gtk.SeparatorMenuItem())

        refresh_item = Gtk.MenuItem(label="Refresh now")
        refresh_item.connect("activate", lambda _w: on_refresh())
        menu.append(refresh_item)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _w: on_quit())
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)

    def set_visible(self, visible):
        # PASSIVE hides the item without unregistering it, so it keeps its
        # place in the panel if it comes back.
        self.indicator.set_status(
            AyatanaAppIndicator3.IndicatorStatus.ACTIVE if visible
            else AyatanaAppIndicator3.IndicatorStatus.PASSIVE
        )

    def show(self, label, detail):
        self.indicator.set_label(f"{self.prefix}{label}", "")
        self.detail_item.set_label(detail)


def installed_tools():
    """Which CLIs are on PATH right now. Checked every poll, so installing or
    removing one takes effect on the next refresh without a restart."""
    return {"claude": shutil.which(CLAUDE_BIN) is not None,
            "codex": shutil.which(CODEX_BIN) is not None}


class UsageIndicator:
    def __init__(self):
        # Both items are always registered and just hidden when their CLI is
        # missing. The AppIndicator extension inserts each new item to the
        # left of the ones before it, so Codex is created first to end up on
        # the right, and a tool installed later still lands in its usual spot.
        self.codex = ToolIndicator(CODEX_APP_ID, codex_icon(), self.poll_async, Gtk.main_quit)
        self.claude = ToolIndicator(APP_ID, claude_icon(), self.poll_async, Gtk.main_quit)
        self.installed = {}
        self.update_visibility(installed_tools())

    def update_visibility(self, installed):
        for name, item in (("claude", self.claude), ("codex", self.codex)):
            if installed[name] != self.installed.get(name):
                logging.info("%s %s", name, "found; showing its indicator"
                             if installed[name] else "not found on PATH; hiding its indicator")
                item.set_visible(installed[name])
        self.installed = installed

    def start(self):
        self.poll_async()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.on_timer)

    def on_timer(self):
        self.poll_async()
        return True

    def poll_async(self):
        threading.Thread(target=self._poll_worker, daemon=True).start()

    def _poll_worker(self):
        # Each tool is fetched independently so one failing never hides the other.
        installed = installed_tools()
        claude = codex = None
        if installed["claude"]:
            try:
                claude = parse_usage(fetch_usage_text())
            except Exception:
                logging.exception("claude usage poll failed")
        if installed["codex"]:
            try:
                codex = fetch_codex_usage()
            except Exception:
                logging.exception("codex usage poll failed")
        GLib.idle_add(self.set_usage, installed, claude, codex)

    def set_usage(self, installed, claude, codex):
        self.update_visibility(installed)
        if installed["claude"]:
            self.claude.show(self.claude_label(claude), self.claude_detail(claude))
        if installed["codex"]:
            self.codex.show(self.codex_label(codex), self.codex_detail(codex))
        return False

    @staticmethod
    def claude_label(usage):
        if not usage:
            return "Session ?% · Week ?%"
        return f"Session {usage['session_pct']}% · Week {usage['week_pct']}%"

    @staticmethod
    def claude_detail(usage):
        if not usage:
            return "Claude: failed to fetch /usage — see indicator.log"
        lines = [f"Claude Session: {usage['session_pct']}% used"]
        if usage["session_reset"]:
            lines.append(f"  resets {usage['session_reset']}")
        lines.append(f"Claude Week: {usage['week_pct']}% used")
        if usage["week_reset"]:
            lines.append(f"  resets {usage['week_reset']}")
        return "\n".join(lines)

    @staticmethod
    def codex_label(usage):
        if not usage:
            return "?%"
        parts = [f"{w['name']} {w['pct']}%" for w in usage["windows"]]
        if usage["month"]:
            parts.append(f"Month {usage['month']['pct']}%")
        stale = "*" if usage["stale_since"] else ""
        return f"{' · '.join(parts) or '?%'}{stale}"

    @staticmethod
    def codex_detail(usage):
        if not usage:
            return "Codex: failed to fetch rate limits — see indicator.log"
        lines = []
        for w in usage["windows"]:
            lines.append(f"Codex {w['name']}: {w['pct']}% used")
            if w["resets_at"]:
                lines.append(f"  resets {format_reset(w['resets_at'])}")
        month = usage["month"]
        if month:
            lines.append(f"Codex Month: {month['pct']}% used "
                         f"({month['used']} of {month['limit']} credits)")
            if month["resets_at"]:
                lines.append(f"  resets {format_reset(month['resets_at'])}")
        if usage["plan"]:
            lines.append(f"  plan: {usage['plan']}")
        if usage["stale_since"]:
            lines.append(f"* live read failed; from session logs as of {usage['stale_since']}")
        return "\n".join(lines)


def main():
    indicator = UsageIndicator()
    indicator.start()
    Gtk.main()


if __name__ == "__main__":
    main()
