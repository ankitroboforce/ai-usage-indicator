"""Windows notification-area indicator for Claude Code and Codex usage.

Shows one tray icon per tool. Each icon is a badge with the highest "% used"
across that tool's limit windows; the tooltip and the Details popup show
every window and when it resets.

Data sources:
  * Claude: `claude.exe -p /usage --output-format json`, run in a locked-down
    profile (no hooks, settings, tools or session transcript). /usage is a
    local command and normally runs no model turn; any call that cannot be
    proven turn-free pauses Claude polling until "Refresh now".
  * Codex: a short-lived `codex app-server`, asked `account/rateLimits/read`
    over JSON-RPC on stdio. No model turn, no quota.

Run with pythonw.exe for the tray, or `python.exe ai_usage_tray.pyw --once`
to print one poll to stdout (exit 0 = all fetched, 3 = a fetch failed,
4 = Claude polling is paused for safety).
"""

import argparse
import ctypes
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent
EMPTY_CWD = INSTALL_DIR / "empty-cwd"
LOG_FILE = INSTALL_DIR / "tray.log"
PID_FILE = INSTALL_DIR / "tray.pid"
READY_FILE = INSTALL_DIR / "tray.ready"   # written once both icons are up
CONFIG_FILE = INSTALL_DIR / "config.json"
# Safety state lives outside the install dir so upgrades (which swap that
# dir) and the installer's staged smoke test all see the same pause.
STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ai-usage-tray"

MUTEX_NAME = "Local\\ai-usage-tray"
QUIT_EVENT_NAME = "Local\\ai-usage-tray-quit"

DEFAULT_REFRESH_SECONDS = 60
MIN_REFRESH_SECONDS = 15
CLAUDE_TIMEOUT_SECONDS = 30
CODEX_TIMEOUT_SECONDS = 15
READY_TIMEOUT_SECONDS = 15
SHUTDOWN_POLL_WAIT_SECONDS = 8   # < install.ps1's 10 s graceful-stop wait
EXIT_FETCH_FAILED = 3            # --once: a tool could not be read
EXIT_CLAUDE_PAUSED = 4           # --once: Claude polling is paused for safety
TOOLTIP_MAX_CHARS = 127  # NOTIFYICONDATAW.szTip holds 128 WCHARs.

CREATE_NO_WINDOW = 0x08000000

# Locked-down profile: no hooks/settings/plugins/MCP (--safe-mode,
# --setting-sources "", --strict-mcp-config), no tools, no transcript.
# --model haiku and --max-budget-usd keep an accidental model turn small.
# Do NOT add --disable-slash-commands (it disables /usage) or --bare (it
# breaks subscription OAuth).
CLAUDE_ARGS = [
    "-p", "/usage",
    "--output-format", "json",
    "--safe-mode",
    "--no-session-persistence",
    "--setting-sources", "",
    "--strict-mcp-config",
    "--tools", "",
    "--model", "haiku",
    "--max-budget-usd", "0.05",
]

CLAUDE_SESSION_RE = re.compile(
    r"Current session:\s*(\d+)%\s*used(?:\s*\S\s*resets\s+([^\n]+))?")
CLAUDE_WEEK_RE = re.compile(
    r"Current week \(all models\):\s*(\d+)%\s*used(?:\s*\S\s*resets\s+([^\n]+))?")

CODEX_WINDOW_NAMES = {300: "Session", 10080: "Week"}

CLAUDE_COLOUR = (217, 119, 87)   # #D97757
CODEX_COLOUR = (32, 33, 35)      # #202123
UNKNOWN_COLOUR = (110, 110, 110)
OUTLINE_COLOUR = (255, 255, 255, 170)  # keeps dark badges visible on a dark taskbar
LEVEL_COLOURS = ((80, (220, 50, 47)), (50, (230, 160, 20)), (0, (60, 170, 80)))

log = logging.getLogger("ai-usage-tray")


# --- Errors -------------------------------------------------------------------

class FetchError(Exception):
    """A fetch failed in a way that is known not to have run a model turn."""


class AmbiguousClaudeRun(Exception):
    """A Claude call ended without proof that it ran no model turn."""


# --- State --------------------------------------------------------------------

@dataclass
class Window:
    name: str
    pct: int | None          # None = unknown
    resets: str = ""


@dataclass
class ToolState:
    status: str = "pending"  # pending | ok | error | paused | missing
    windows: list = field(default_factory=list)
    extra: list = field(default_factory=list)   # extra detail lines
    error: str = ""
    updated: str = ""

    def binding_pct(self):
        """Highest % used, or None if any window is unknown (can't vouch for it)."""
        if not self.windows or any(w.pct is None for w in self.windows):
            return None
        return max(w.pct for w in self.windows)

    def label(self):
        if self.status == "paused":
            return "paused (safety) - use Refresh now"
        if self.status == "missing":
            return "CLI not found"
        if self.status != "ok":
            return "?% - fetch failed" if self.status == "error" else "loading"
        parts = [f"{w.name} {'?' if w.pct is None else w.pct}%" for w in self.windows]
        return " · ".join(parts) or "?%"


# --- Formatting ---------------------------------------------------------------

def format_epoch(epoch):
    """'6:50 PM on 30 Sep' in local time. Windows strftime has no %-I/%-d."""
    if not epoch:
        return ""
    dt = datetime.fromtimestamp(epoch)
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {ampm} on {dt.day} {dt.strftime('%b')}"


def tooltip(tool_name, state):
    text = f"{tool_name}  {state.label()}"
    if len(text) > TOOLTIP_MAX_CHARS:
        text = text[:TOOLTIP_MAX_CHARS - 1] + "…"
    return text


def details_text(tool_name, state):
    lines = [f"{tool_name}: {state.label()}", ""]
    for w in state.windows:
        pct = "?" if w.pct is None else w.pct
        lines.append(f"{w.name}: {pct}% used")
        if w.resets:
            lines.append(f"    resets {w.resets}")
    lines.extend(state.extra)
    if state.error:
        lines.append(f"Error: {state.error}")
    if state.updated:
        lines.append(f"Updated {state.updated}")
    return "\n".join(lines).strip()


def level_colour(pct):
    for threshold, colour in LEVEL_COLOURS:
        if pct >= threshold:
            return colour
    return LEVEL_COLOURS[-1][1]


# --- Binary resolution --------------------------------------------------------

def resolve_claude(override=None):
    """Path to the native claude.exe, never the npm claude.CMD shim."""
    candidates = []
    if override:
        candidates.append(Path(override))
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / "@anthropic-ai"
                          / "claude-code" / "bin" / "claude.exe")
    candidates.append(Path.home() / ".local" / "bin" / "claude.exe")
    on_path = shutil.which("claude.exe")
    if on_path:
        candidates.append(Path(on_path))
    for path in candidates:
        if path.suffix.lower() == ".exe" and path.is_file():
            return path
    return None


def _npm_codex_exe(prefix):
    """Native codex.exe inside an npm global install rooted at `prefix`.

    npm installs @openai/codex as a node launcher (codex.cmd -> codex.js)
    plus a platform package holding the real binary at
    @openai/codex-win32-<arch>/vendor/<triple>/bin/codex.exe, either nested
    under @openai/codex or hoisted. Running the exe directly avoids the cmd
    shim and an extra node process that a timeout kill would orphan.
    """
    modules = Path(prefix) / "node_modules" / "@openai"
    patterns = (
        (modules / "codex" / "node_modules" / "@openai", "codex-win32-*/vendor/*/bin/codex.exe"),
        (modules, "codex-win32-*/vendor/*/bin/codex.exe"),
        (modules / "codex" / "vendor", "*/bin/codex.exe"),
    )
    for base, pattern in patterns:
        found = sorted(base.glob(pattern))
        if found:
            return found[0]
    return None


def resolve_codex(override=None):
    """Codex CLI: override, codex.exe on PATH, npm install, desktop app."""
    if override:
        path = Path(override)
        return path if path.suffix.lower() == ".exe" and path.is_file() else None
    on_path = shutil.which("codex.exe")
    if on_path:
        return Path(on_path)
    prefixes = []
    shim = shutil.which("codex")
    if shim:
        prefixes.append(Path(shim).parent)
    appdata = os.environ.get("APPDATA")
    if appdata:
        prefixes.append(Path(appdata) / "npm")
    for prefix in prefixes:
        exe = _npm_codex_exe(prefix)
        if exe:
            return exe
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    builds = list((Path(local) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))
    if not builds:
        return None
    return max(builds, key=lambda p: p.stat().st_mtime)


def empty_cwd():
    """An empty working dir for child processes, so no project config loads."""
    for path in (EMPTY_CWD, Path(tempfile.gettempdir()) / "ai-usage-tray-empty-cwd"):
        try:
            path.mkdir(exist_ok=True)
            if not any(path.iterdir()):
                return path
        except OSError:
            continue
    raise FetchError("no empty working directory available")


# --- Claude -------------------------------------------------------------------

def check_claude_envelope(stdout):
    """Return the parsed envelope, or raise if it can't prove zero turns."""
    try:
        envelope = json.loads(stdout)
    except (ValueError, TypeError) as exc:
        raise AmbiguousClaudeRun(f"output is not JSON: {exc}") from None
    if not isinstance(envelope, dict):
        raise AmbiguousClaudeRun("output is not a JSON object")
    turns = envelope.get("num_turns")
    cost = envelope.get("total_cost_usd")
    if not isinstance(turns, int) or not isinstance(cost, (int, float)):
        raise AmbiguousClaudeRun("envelope lacks num_turns/total_cost_usd")
    if turns != 0 or cost > 0:
        raise AmbiguousClaudeRun(f"/usage ran a model turn (turns={turns}, cost=${cost})")
    return envelope


def run_claude_usage(argv, timeout=CLAUDE_TIMEOUT_SECONDS):
    """Run the /usage command line `argv` and return the usage text."""
    try:
        proc = subprocess.run(
            argv, cwd=empty_cwd(),
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise AmbiguousClaudeRun(f"timed out after {timeout}s") from None
    except OSError as exc:
        raise FetchError(f"could not start claude: {exc}") from None
    envelope = check_claude_envelope(proc.stdout.decode("utf-8", errors="replace"))
    result = envelope.get("result")
    if envelope.get("is_error") or not isinstance(result, str):
        raise FetchError(f"claude reported an error: {str(result)[:200]}")
    return result


def parse_claude_usage(text):
    """Session and Week windows; a missing line becomes an unknown window.

    Both are always returned so a vanished line shows as '?' instead of the
    badge quietly reporting only the other (possibly lower) limit.
    """
    windows = []
    for name, regex in (("Session", CLAUDE_SESSION_RE), ("Week", CLAUDE_WEEK_RE)):
        match = regex.search(text)
        if match:
            windows.append(Window(name, int(match.group(1)), (match.group(2) or "").strip()))
        else:
            windows.append(Window(name, None))
    if all(w.pct is None for w in windows):
        raise FetchError(f"could not parse /usage output: {text[:200]!r}")
    return windows


# --- Codex --------------------------------------------------------------------

def fetch_codex_rate_limits(argv, timeout=CODEX_TIMEOUT_SECONDS):
    """Ask a short-lived app-server (`argv`) for account/rateLimits/read.

    stdin stays open until the answer arrives: the server exits on EOF
    without answering pending requests. stdout is read on a thread because
    select() does not work on Windows pipes.
    """
    try:
        proc = subprocess.Popen(
            argv, cwd=empty_cwd(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    except OSError as exc:
        raise FetchError(f"could not start codex: {exc}") from None
    lines = queue.Queue()

    def pump():
        for raw in proc.stdout:
            lines.put(raw)
        lines.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        requests = [
            {"id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "ai-usage-tray", "version": "1"}}},
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read",
             "params": {"excludeResetCreditDetails": True}},
        ]
        proc.stdin.write("".join(json.dumps(r) + "\n" for r in requests).encode())
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("codex app-server did not answer in time")
            try:
                raw = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if raw is None:
                raise FetchError(f"codex app-server exited early (code {proc.poll()})")
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict) or msg.get("id") != 2:
                continue
            if "error" in msg:
                raise FetchError(f"rateLimits/read failed: {str(msg['error'])[:200]}")
            if not isinstance(msg.get("result"), dict):
                raise FetchError("rateLimits/read answer has no result object")
            return msg["result"]
    except OSError as exc:
        raise FetchError(f"codex app-server pipe error: {exc}") from None
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        reader.join(timeout=2)
        if not reader.is_alive():
            proc.stdout.close()


def _window_name(mins):
    if mins in CODEX_WINDOW_NAMES:
        return CODEX_WINDOW_NAMES[mins]
    if mins and mins % 1440 == 0:
        return f"{mins // 1440}d"
    if mins and mins % 60 == 0:
        return f"{mins // 60}h"
    return f"{mins}m" if mins else "Window"


def parse_codex(result):
    """Return (windows, extra detail lines) from a rateLimits/read result."""
    limits = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(limits, dict):
        raise FetchError("rateLimits missing from app-server answer")
    raw_windows = []
    for key in ("primary", "secondary"):
        w = limits.get(key)
        if w is None:
            continue
        if not isinstance(w, dict):
            raise FetchError(f"rateLimits.{key} is not an object")
        mins = w.get("windowDurationMins")
        raw_windows.append((mins if isinstance(mins, int) else 0, w))
    raw_windows.sort(key=lambda pair: pair[0])
    windows = []
    for mins, w in raw_windows:
        pct = w.get("usedPercent")
        resets = w.get("resetsAt")
        windows.append(Window(
            _window_name(mins),
            round(pct) if isinstance(pct, (int, float)) else None,
            format_epoch(resets) if isinstance(resets, (int, float)) else ""))
    extra = []
    individual = limits.get("individualLimit")
    if not isinstance(individual, dict):
        individual = {}
    remaining = individual.get("remainingPercent")
    if isinstance(remaining, (int, float)):
        resets = individual.get("resetsAt")
        windows.append(Window("Month", round(100 - remaining),
                              format_epoch(resets) if isinstance(resets, (int, float)) else ""))
        if individual.get("used") is not None and individual.get("limit") is not None:
            extra.append(f"Credits: {individual['used']} of {individual['limit']} used")
    if limits.get("planType"):
        extra.append(f"Plan: {limits['planType']}")
    if not windows:
        raise FetchError("app-server answer has no limit windows")
    return windows, extra


# --- Polling ------------------------------------------------------------------

def load_config():
    config = {"refresh_seconds": DEFAULT_REFRESH_SECONDS, "claude_bin": None, "codex_bin": None}
    if not CONFIG_FILE.is_file():
        return config
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        log.error("ignoring invalid %s: %s", CONFIG_FILE, exc)
        return config
    refresh = loaded.get("refresh_seconds")
    if isinstance(refresh, (int, float)) and refresh >= MIN_REFRESH_SECONDS:
        config["refresh_seconds"] = refresh
    elif refresh is not None:
        log.error("refresh_seconds must be a number >= %s; using default", MIN_REFRESH_SECONDS)
    for key in ("claude_bin", "codex_bin"):
        if isinstance(loaded.get(key), str) and loaded[key]:
            config[key] = loaded[key]
    return config


def _process_alive(pid):
    """True if `pid` is a running process. (os.kill(pid, 0) would KILL it on Windows.)"""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(_kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


class Poller:
    """Fetches both tools; owns the Claude safety pause.

    The pause is a file in STATE_DIR, shared by every process (tray, --once,
    the installer's smoke test) and surviving restarts and upgrades. Each
    Claude call is bracketed by a per-process in-flight marker; a marker left
    by a dead process means a call whose result was never checked, which
    also pauses. Only "Refresh now" (resume_claude) clears the pause.
    """

    def __init__(self, config, state_dir=None):
        self.config = config
        self.state_dir = Path(state_dir) if state_dir else STATE_DIR
        self.pause_file = self.state_dir / "claude-paused.txt"
        self.inflight_file = self.state_dir / f"claude-inflight-{os.getpid()}"
        self._memory_pause = ""   # fallback if the pause file can't be written
        for marker in self.state_dir.glob("claude-inflight-*"):
            try:
                pid = int(marker.name.rsplit("-", 1)[1])
            except ValueError:
                pid = 0
            if pid != os.getpid() and _process_alive(pid):
                continue   # another live instance is mid-call
            self._pause("a previous Claude call was interrupted before its result was checked")
            marker.unlink(missing_ok=True)

    def claude_paused(self):
        """Pause reason, or '' when polling is allowed. Fails closed."""
        if self._memory_pause:
            return self._memory_pause
        try:
            return self.pause_file.read_text(encoding="utf-8").strip() or "paused"
        except FileNotFoundError:
            return ""
        except OSError as exc:
            return f"pause state unreadable: {exc}"

    def _pause(self, reason):
        reason = f"{reason} (at {datetime.now():%Y-%m-%d %H:%M})"
        log.error("Claude polling PAUSED until Refresh now: %s", reason)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.pause_file.write_text(reason, encoding="utf-8")
        except OSError:
            log.exception("could not persist the Claude pause; keeping it in memory")
            self._memory_pause = reason

    def poll_claude(self):
        paused = self.claude_paused()
        if paused:
            return ToolState("paused", error=paused, updated=_now())
        exe = resolve_claude(self.config["claude_bin"])
        if not exe:
            return ToolState("missing")
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.inflight_file.write_text(str(os.getpid()), encoding="ascii")
        except OSError as exc:
            return ToolState("error", error=f"cannot write state dir: {exc}", updated=_now())
        try:
            text = run_claude_usage([str(exe), *CLAUDE_ARGS])
            return ToolState("ok", parse_claude_usage(text), updated=_now())
        except AmbiguousClaudeRun as exc:
            self._pause(str(exc))
            return ToolState("paused", error=self.claude_paused(), updated=_now())
        except FetchError as exc:
            log.error("claude fetch failed: %s", exc)
            return ToolState("error", error=str(exc), updated=_now())
        finally:
            # Reached only once the call's outcome is classified (pausing first
            # if needed). A crash or kill before this leaves the marker behind.
            self.inflight_file.unlink(missing_ok=True)

    def poll_codex(self):
        exe = resolve_codex(self.config["codex_bin"])
        if not exe:
            return ToolState("missing")
        try:
            windows, extra = parse_codex(fetch_codex_rate_limits([str(exe), "app-server"]))
            return ToolState("ok", windows, extra, updated=_now())
        except FetchError as exc:
            log.error("codex fetch failed: %s", exc)
            return ToolState("error", error=str(exc), updated=_now())

    def resume_claude(self):
        if self.claude_paused():
            log.info("Claude polling resumed by user")
        self._memory_pause = ""
        try:
            self.pause_file.unlink(missing_ok=True)
        except OSError:
            log.exception("could not clear the Claude pause file")


def _now():
    return datetime.now().strftime("%H:%M:%S")


# --- Win32 helpers ------------------------------------------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
_kernel32.ResetEvent.argtypes = (wintypes.HANDLE,)
_kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

MB_OK = 0x0
MB_ICONINFORMATION = 0x40
MB_TOPMOST = 0x40000
MB_SETFOREGROUND = 0x10000


def acquire_single_instance():
    """Return the mutex handle, or None if another instance holds it."""
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def create_quit_event():
    """Auto-reset named event; cleared now so a stale signal can't stop us."""
    handle = _kernel32.CreateEventW(None, False, False, QUIT_EVENT_NAME)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateEventW failed")
    _kernel32.ResetEvent(handle)
    return handle


# --- Tray ---------------------------------------------------------------------

def render_badge(state, brand_colour):
    """64x64 badge: brand square, binding % in white, level stripe."""
    from PIL import Image, ImageDraw, ImageFont

    size = 64
    pct = state.binding_pct() if state.status == "ok" else None
    if state.status == "paused":
        text, fill = "!", UNKNOWN_COLOUR
    elif pct is None:
        text, fill = "?", UNKNOWN_COLOUR
    else:
        text, fill = str(pct), brand_colour
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=12, fill=fill,
                           outline=OUTLINE_COLOUR, width=3)
    if pct is not None:
        draw.rectangle((4, size - 10, size - 5, size - 4), fill=level_colour(pct))
    font_size = 44 if len(text) <= 2 else 32
    try:
        font = ImageFont.truetype("segoeuib.ttf", font_size)
    except OSError:
        font = ImageFont.load_default(size=font_size)
    box = draw.textbbox((0, 0), text, font=font)
    x = (size - (box[2] - box[0])) / 2 - box[0]
    y = (size - 8 - (box[3] - box[1])) / 2 - box[1]
    draw.text((x, y), text, font=font, fill=(255, 255, 255))
    return image


def stable_icon_class(uid):
    """pystray Icon subclass with a fixed notify-icon ID.

    pystray identifies icons by id(self), which changes every launch, so
    Windows treats each run as a new icon and forgets "show on taskbar".
    """
    import pystray
    from pystray._util import win32

    class StableIcon(pystray.Icon):
        def _message(self, code, flags, **kwargs):
            win32.Shell_NotifyIcon(code, win32.NOTIFYICONDATAW(
                cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW),
                hWnd=self._hwnd, hID=uid, uFlags=flags, **kwargs))

    return StableIcon


class ToolIcon:
    """One tray icon. The menu is built once and never changed."""

    def __init__(self, app, key, name, colour, uid):
        import pystray

        self.app = app
        self.name = name
        self.colour = colour
        self.state = ToolState()
        self.lock = threading.Lock()
        self.popup_open = threading.Event()
        menu = pystray.Menu(
            pystray.MenuItem("Details…", self.show_details, default=True),
            pystray.MenuItem("Refresh now", app.refresh_now),
            pystray.MenuItem("Open log", app.open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", app.request_quit))
        self.icon = stable_icon_class(uid)(f"ai-usage-{key}", render_badge(self.state, colour),
                                 tooltip(name, self.state), menu)
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"tray-{key}", daemon=True)

    def _run(self):
        # setup runs once the icon's message loop is live; the icon stays
        # hidden until the first poll says whether the CLI exists.
        try:
            self.icon.run(setup=lambda icon: self.ready.set())
        except Exception:
            log.exception("%s icon loop failed", self.name)
        if not self.app.stopping.is_set():
            log.error("%s icon loop ended unexpectedly; quitting", self.name)
            self.app.request_quit(failed=True)

    def update(self, state):
        with self.lock:
            self.state = state
        visible = state.status != "missing"
        if visible:
            self.icon.icon = render_badge(state, self.colour)
            self.icon.title = tooltip(self.name, state)
        if self.icon.visible != visible:
            self.icon.visible = visible

    def show_details(self):
        if self.popup_open.is_set():
            return
        with self.lock:
            text = details_text(self.name, self.state)
        self.popup_open.set()

        def popup():
            try:
                ctypes.windll.user32.MessageBoxW(
                    None, text, f"{self.name} usage",
                    MB_OK | MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND)
            finally:
                self.popup_open.clear()

        threading.Thread(target=popup, daemon=True).start()


class TrayApp:
    def __init__(self, config, quit_event):
        self.config = config
        self.quit_event = quit_event
        self.poller = Poller(config)
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.failed = False
        self.icons = {
            "claude": ToolIcon(self, "claude", "Claude", CLAUDE_COLOUR, uid=1),
            "codex": ToolIcon(self, "codex", "Codex", CODEX_COLOUR, uid=2),
        }
        self.poll_thread = threading.Thread(target=self._poll_loop, name="poller", daemon=True)

    def run(self):
        """Run until Quit; returns the process exit code."""
        for item in self.icons.values():
            item.thread.start()
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        for item in self.icons.values():
            if not item.ready.wait(max(0, deadline - time.monotonic())):
                log.error("%s icon did not start within %ss", item.name, READY_TIMEOUT_SECONDS)
                self.failed = True
        if not self.failed:
            READY_FILE.write_text(str(os.getpid()), encoding="ascii")
            self.poll_thread.start()
            # Main thread: wait for Quit (menu), the installer's quit signal,
            # or an icon loop dying (which also signals quit, failed=True).
            while _kernel32.WaitForSingleObject(self.quit_event, 1000) != WAIT_OBJECT_0:
                pass
        self.stopping.set()
        self.wake.set()
        if self.poll_thread.is_alive():
            # Let an in-flight Claude call finish and be checked (normally
            # ~2 s). If it can't, its in-flight marker pauses the next start.
            self.poll_thread.join(timeout=SHUTDOWN_POLL_WAIT_SECONDS)
        for item in self.icons.values():
            item.icon.stop()
        for item in self.icons.values():
            item.thread.join(timeout=5)
        return 1 if self.failed else 0

    def _poll_loop(self):
        polls = (("claude", self.poller.poll_claude), ("codex", self.poller.poll_codex))
        while not self.stopping.is_set():
            for key, poll in polls:
                if self.stopping.is_set():
                    break
                try:
                    state = poll()
                except Exception as exc:  # Never leave a stale badge or kill the thread.
                    log.exception("%s poll failed unexpectedly", key)
                    state = ToolState("error", error=f"internal error: {exc}", updated=_now())
                try:
                    self.icons[key].update(state)
                except Exception:
                    log.exception("%s icon update failed", key)
            self.wake.wait(self.config["refresh_seconds"])
            self.wake.clear()

    def refresh_now(self):
        self.poller.resume_claude()
        self.wake.set()

    def open_log(self):
        LOG_FILE.touch(exist_ok=True)
        os.startfile(LOG_FILE)

    def request_quit(self, failed=False):
        if failed:
            self.failed = True
        _kernel32.SetEvent(self.quit_event)


# --- Entry points -------------------------------------------------------------

def setup_logging(to_console):
    handler = (logging.StreamHandler(sys.stdout) if to_console else
               RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2,
                                   encoding="utf-8"))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def run_once():
    setup_logging(to_console=True)
    poller = Poller(load_config())
    claude, codex = poller.poll_claude(), poller.poll_codex()
    for name, state in (("Claude", claude), ("Codex", codex)):
        print(tooltip(name, state))
        print("  " + details_text(name, state).replace("\n", "\n  "))
    if claude.status == "paused":
        return EXIT_CLAUDE_PAUSED
    if "error" in (claude.status, codex.status):
        return EXIT_FETCH_FAILED
    return 0


def run_tray():
    setup_logging(to_console=False)
    mutex = acquire_single_instance()
    if mutex is None:
        return 0
    quit_event = create_quit_event()
    READY_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    log.info("started (pid %s)", os.getpid())
    code = 1
    try:
        code = TrayApp(load_config(), quit_event).run()
    except Exception:
        log.exception("tray crashed")
        raise
    finally:
        for path in (READY_FILE, PID_FILE):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        log.info("stopped (exit %s)", code)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true",
                        help="poll once, print to stdout, exit (0 ok, 3 a fetch failed, "
                             "4 Claude paused)")
    args = parser.parse_args()
    return run_once() if args.once else run_tray()


if __name__ == "__main__":
    sys.exit(main())
