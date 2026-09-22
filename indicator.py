#!/usr/bin/env python3
"""GNOME top panel indicator showing Claude Code usage (`claude -p "/usage"`)."""

import json
import logging
import re
import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3, GLib, Gtk

REFRESH_SECONDS = 120
CLAUDE_BIN = "claude"
STATE_DIR = Path.home() / ".local" / "share" / "claude-usage-indicator"
LOG_FILE = STATE_DIR / "indicator.log"
APP_ID = "claude-usage-indicator"

STATE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

SESSION_RE = re.compile(r"Current session:\s*(\d+)% used(?:[^\n]*?resets ([^\n(]+))?")
WEEK_RE = re.compile(r"Current week \(all models\):\s*(\d+)% used(?:[^\n]*?resets ([^\n(]+))?")


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


class UsageIndicator:
    def __init__(self):
        self.indicator = AyatanaAppIndicator3.Indicator.new(
            APP_ID,
            "utilities-terminal-symbolic",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_label("Claude: …", "Claude: 100%")

        self.detail_item = Gtk.MenuItem(label="Loading…")
        self.detail_item.set_sensitive(False)

        self.menu = Gtk.Menu()
        self.menu.append(self.detail_item)
        self.menu.append(Gtk.SeparatorMenuItem())

        refresh_item = Gtk.MenuItem(label="Refresh now")
        refresh_item.connect("activate", self.on_refresh_clicked)
        self.menu.append(refresh_item)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self.on_quit)
        self.menu.append(quit_item)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

    def start(self):
        self.poll_async()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.on_timer)

    def on_timer(self):
        self.poll_async()
        return True

    def on_refresh_clicked(self, _widget):
        self.poll_async()

    def on_quit(self, _widget):
        Gtk.main_quit()

    def poll_async(self):
        threading.Thread(target=self._poll_worker, daemon=True).start()

    def _poll_worker(self):
        try:
            text = fetch_usage_text()
            usage = parse_usage(text)
        except Exception:
            logging.exception("usage poll failed")
            GLib.idle_add(self.set_error)
            return
        GLib.idle_add(self.set_usage, usage)

    def set_usage(self, usage):
        label = f"Session {usage['session_pct']}% · Week {usage['week_pct']}%"
        self.indicator.set_label(label, "Claude: 100%")

        detail_lines = [f"Session: {usage['session_pct']}% used"]
        if usage["session_reset"]:
            detail_lines.append(f"  resets {usage['session_reset']}")
        detail_lines.append(f"Week: {usage['week_pct']}% used")
        if usage["week_reset"]:
            detail_lines.append(f"  resets {usage['week_reset']}")
        self.detail_item.set_label("\n".join(detail_lines))
        return False

    def set_error(self):
        self.indicator.set_label("Session ?% · Week ?%", "Claude: 100%")
        self.detail_item.set_label("Failed to fetch /usage — see indicator.log")
        return False


def main():
    indicator = UsageIndicator()
    indicator.start()
    Gtk.main()


if __name__ == "__main__":
    main()
