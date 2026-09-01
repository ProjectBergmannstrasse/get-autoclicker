# autoclicker-universal.py  v5.6
# One file for Windows, macOS and Linux — it detects which at
# runtime. Single-platform builds: autoclicker-windows.py /
# autoclicker-macos.py
# Dependencies install themselves on first run — nothing to type.
# (manual fallback:  pip3 install --user textual pynput requests pyperclip)
"""
v5.6 changes vs v5.5:
  - RECORDER no longer records clicks that land on this app's own window.
    Pressing STOP with the mouse was captured like any other click, so
    replaying the macro walked the pointer back here and clicked whatever
    was under it - STOP again, or a wallet copy pill, which silently
    replaced the clipboard with a crypto address mid-macro.
    macOS never showed it because Esc stops recording there before any
    click happens; on Windows you click the button.
      * Textual delivers mouse events only when the click hit us, so those
        are exact markers - the matching recorded clicks are removed.
      * A trailing click at the very end is dropped too, for the release
        that arrives after the listener is gone.
      * Recordings saved before this fix are trimmed when they load, so
        existing macros stop doing it without being re-recorded.

v5.5 changes vs v5.4:
  - WINDOWS HOTKEYS:
      * F8 = start/stop, F9 = pause/resume. Tapping Alt alone activates the
        menu bar of the focused window on Windows, so the Alt hotkeys were
        popping menus in the target app. F8/F9 don't.
      * AltGr is now recognised. On UK/German/French/Nordic/Polish layouts
        the right Alt reports as Key.alt_gr, not Key.alt_r, so both-Alt
        pause and single right-Alt did nothing at all on those keyboards.
      * The synthetic Ctrl that Windows fires alongside AltGr no longer
        cancels a legitimate Alt tap.

v5.3 changes vs v5.2:
  - AUTO-INSTALL bootstrap: detects missing packages, asks permission,
    installs them behind a live progress UI, then starts normally.
      * pure-stdlib UI (runs before textual/rich exist)
      * venv aware; --user by default; PEP-668 / --break-system-packages
        and no---user fallbacks; ensurepip when pip itself is missing
      * flags: --yes  --no-install  --setup  --no-perms
  - PERMISSION GATE: macOS Accessibility + Input Monitoring are checked via
    ctypes, with a native grant prompt and one-key deep links into
    System Settings; Windows elevation hint; Linux Wayland warning.
  - Remembers declined optional packages so it never nags twice.

v5.2 changes vs v5.1:
  - Tabs: MAIN · COMMANDS · HISTORY · FEEDBACK · SETTINGS · INFO
  - Esc bar SHRINKS smoothly (a width-animated progress bar, not a static block)
  - Esc behavior:
      tap 1 = arm 3-second window
      tap 2 within window = PAUSE / RESUME
      tap 3 within window = QUIT
  - Settings tab:
      esc warning style:  GUI bar  OR  fullscreen takeover (separate window)
      warning emoji:      user-editable (default ⚠️)
  - Fullscreen takeover via separate tkinter subprocess (esc_overlay.py)
  - Mini mode resizes the host terminal window (AppleScript on macOS,
    ANSI + mode con on Windows, ANSI elsewhere)
  - Hotkeys: F8 start/stop · F9 pause · Alt tap · both Alts · Esc x3 quit
"""
from __future__ import annotations

import atexit
import collections
import datetime as dt
import getpass
import hashlib
import json
import os
import platform
import random
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional


# ── Brand ─────────────────────────────────────────────────────────────────────
BRAND   = "AutoClicker"
GITHUB  = "github.com/ProjectBergmannstrasse"
VERSION = "5.6"

# Which build this file is. "universal" detects the OS at runtime and carries
# the code for all three; "windows" / "macos" are cut from this same source by
# make_builds.py and contain only their own platform's code.
BUILD_TARGET = "universal"

IS_MAC   = platform.system() == "Darwin"
IS_WIN   = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"



def _wrong_os_exit() -> None:
    """A single-platform build refusing to run on the wrong OS.

    Never fires in the universal build. Kept plain-ASCII and dependency-free
    because it may run before anything at all has been set up.
    """
    want = {"windows": "Windows", "macos": "macOS"}.get(BUILD_TARGET)
    if want is None:
        return
    here = platform.system()
    ok = (BUILD_TARGET == "windows" and here == "Windows") or \
         (BUILD_TARGET == "macos" and here == "Darwin")
    if ok:
        return
    other = {"windows": "macOS", "macos": "Windows"}[BUILD_TARGET]
    other_file = {"windows": "autoclicker-macos.py",
                  "macos": "autoclicker-windows.py"}[BUILD_TARGET]
    print(f"\n  This is the {want} build of AutoClicker, but this machine is "
          f"running {here or 'an unknown OS'}.\n")
    print(f"  For {other:<16} use  {other_file}")
    print(f"  For anything else  use  autoclicker-universal.py  "
          f"(detects the OS itself)\n")
    sys.exit(2)
def platform_label() -> str:
    if IS_MAC:
        return f"macOS {platform.mac_ver()[0] or platform.release()}"
    if IS_WIN:
        return f"Windows {platform.release()}"
    if IS_LINUX:
        return f"Linux ({platform.release()})"
    return platform.system() or "Unknown OS"
import importlib
import importlib.util
import shutil as _shutil
import site as _site
_BOOT_GUARD = "_AUTOCLICKER_BOOTSTRAPPED"
_SETUP_FILE = Path.home() / ".autoclicker" / "setup.json"
_DEPENDENCIES = [
    ("textual",   "textual",   "terminal user interface",       True),
    ("pynput",    "pynput",    "mouse + keyboard control",      True),
    ("requests",  "requests",  "session reporting",             False),
    ("pyperclip", "pyperclip", "clipboard copy fallback",       False),
]
_DEP_SIZES = {"textual": "1.4 MB", "pynput": "0.1 MB",
              "requests": "0.1 MB", "pyperclip": "0.02 MB"}
class _Glyph:
    """Box-drawing set, with an ASCII fallback for consoles that can't cope.

    A legacy Windows console (cmd.exe on cp437/cp1252, or a raster font)
    raises UnicodeEncodeError the moment you print a block character.
    _boot_init_console() decides which set is safe and swaps them out.
    """
    unicode = True
    TICK, CROSS, DOT, ARROW, BULLET = "✓", "✗", "·", "▸", "●"
    FULL, EMPTY, RULE = "█", "░", "─"
    SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    LOGO = ["▄▀█ █ █ ▀█▀ █▀█",
            "█▀█ █▄█  █  █▄█"]

    @classmethod
    def ascii_mode(cls) -> None:
        cls.unicode = False
        cls.TICK, cls.CROSS, cls.DOT = "+", "x", "."
        cls.ARROW, cls.BULLET = ">", "*"
        cls.FULL, cls.EMPTY, cls.RULE = "#", "-", "-"
        cls.SPIN = "|/-\\"
        cls.LOGO = ["AUTO", "CLICKER"]
def _boot_init_console() -> None:
    """Make this console able to print the UI, or fall back to ASCII.

    Windows: switch the console to UTF-8 (codepage 65001) and re-wrap stdout,
    because the default cp437/cp1252 console cannot encode the box glyphs and
    would crash the installer before it printed a single line.
    """
    forced_ascii = ("--ascii" in [a.lower() for a in sys.argv[1:]]
                    or os.environ.get("AUTOCLICKER_ASCII") == "1")

    cp_ok = True
    if IS_WIN:
        # cmd.exe defaults to cp437/cp850/cp1252, none of which can render the
        # box glyphs. Ask the console for UTF-8; if it refuses, go ASCII.
        cp_ok = False
        try:
            import ctypes
            k = ctypes.windll.kernel32
            cp_ok = bool(k.SetConsoleOutputCP(65001))
            k.SetConsoleCP(65001)
        except Exception:
            cp_ok = False

    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Whatever we ended up with, prove it can actually carry the glyphs.
    enc_ok = True
    enc = (getattr(sys.stdout, "encoding", None) or "ascii")
    try:
        "".join([_Glyph.FULL, _Glyph.TICK, _Glyph.SPIN[0],
                 _Glyph.LOGO[0]]).encode(enc)
    except Exception:
        enc_ok = False

    if forced_ascii or not enc_ok or not cp_ok:
        _Glyph.ascii_mode()
_ASCII_MAP = {
    "\u2014": "-", "\u2013": "-", "\u00b7": "-", "\u2026": "...",
    "\u2192": "->", "\u25b8": ">", "\u2022": "*",
    "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
}
def _ascii_safe(s: str) -> str:
    """Transliterate to plain ASCII when the console cannot do better."""
    if _Glyph.unicode:
        return s
    for k, v in _ASCII_MAP.items():
        s = s.replace(k, v)
    return s.encode("ascii", "replace").decode("ascii")
def _say(*args, **kw) -> None:
    """print(), but safe on a legacy console."""
    print(*[_ascii_safe(str(a)) for a in args], **kw)
class _Ink:
    """ANSI colours that quietly turn themselves off when unsupported."""
    on = False
    RESET = BOLD = DIM = ""
    CYAN = GREEN = RED = ORANGE = GREY = WHITE = BLUE = ""

    @classmethod
    def enable(cls) -> None:
        cls.on = True
        cls.RESET, cls.BOLD, cls.DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
        cls.CYAN   = "\x1b[38;5;45m"
        cls.GREEN  = "\x1b[38;5;42m"
        cls.RED    = "\x1b[38;5;203m"
        cls.ORANGE = "\x1b[38;5;214m"
        cls.GREY   = "\x1b[38;5;60m"
        cls.WHITE  = "\x1b[38;5;255m"
        cls.BLUE   = "\x1b[38;5;69m"
def _boot_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False
def _boot_stdin_tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False
def _boot_init_colour() -> None:
    if os.environ.get("NO_COLOR") or not _boot_tty():
        return
    if IS_WIN:
        # Enable virtual-terminal processing so ANSI works in cmd.exe
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not k.GetConsoleMode(h, ctypes.byref(mode)):
                return
            k.SetConsoleMode(h, mode.value | 0x0004)
        except Exception:
            return
    _Ink.enable()
def _boot_width() -> int:
    try:
        w = _shutil.get_terminal_size((80, 24)).columns
    except Exception:
        w = 80
    return max(52, min(w - 2, 78))
def _strip_ansi(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        if s[i] == "\x1b":
            j = s.find("m", i)
            if j == -1:
                break
            i = j + 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out)
def _rule(width: int, ch: str = "") -> str:
    return f"{_Ink.GREY}{(ch or _Glyph.RULE) * width}{_Ink.RESET}"
def _boot_header(title: str, subtitle: str = "") -> list:
    w = _boot_width()
    logo = _Glyph.LOGO
    pad = max(len(x) for x in logo)
    return [
        "",
        f"  {_Ink.RED}{_Ink.BOLD}{logo[0]:<{pad}}{_Ink.RESET}   "
        f"{_Ink.CYAN}{_Ink.BOLD}{title}{_Ink.RESET}",
        f"  {_Ink.RED}{_Ink.BOLD}{logo[1]:<{pad}}{_Ink.RESET}   "
        f"{_Ink.GREY}{subtitle}{_Ink.RESET}",
        "  " + _rule(w - 2),
        "",
    ]
class _LivePanel:
    """Draws a block of lines and redraws it in place, flicker-free."""

    def __init__(self):
        self._n = 0
        self.live = _boot_tty()

    def open(self) -> None:
        if self.live:
            sys.stdout.write("\x1b[?25l")   # hide cursor
            sys.stdout.flush()

    def close(self) -> None:
        if self.live:
            sys.stdout.write("\x1b[?25h")   # show cursor
            sys.stdout.flush()

    def draw(self, lines: list) -> None:
        if not self.live:
            return
        buf = []
        if self._n:
            buf.append(f"\x1b[{self._n}A")
        for ln in lines:
            buf.append("\x1b[2K" + _ascii_safe(ln) + "\n")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        self._n = len(lines)
def _bar(frac: float, width: int, colour: str) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return (f"{colour}{_Glyph.FULL * filled}{_Ink.RESET}"
            f"{_Ink.GREY}{_Glyph.EMPTY * (width - filled)}{_Ink.RESET}")
def _boot_ask(prompt: str, choices: str, default: str) -> str:
    """Keyed prompt. Returns a lowercase letter from `choices`."""
    if not _boot_tty() or not _boot_stdin_tty():
        return default
    while True:
        try:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            raw = sys.stdin.readline()
        except (KeyboardInterrupt, EOFError):
            _say()
            return "q" if "q" in choices else default
        if raw == "":
            return default
        ans = raw.strip().lower()
        if ans == "":
            return default
        if ans[0] in choices:
            return ans[0]
        sys.stdout.write(f"  {_Ink.RED}pick one of: "
                         f"{'  '.join(c.upper() for c in choices)}{_Ink.RESET}\n")
def _setup_load() -> dict:
    try:
        return json.loads(_SETUP_FILE.read_text())
    except Exception:
        return {}
def _setup_save(d: dict) -> None:
    try:
        _SETUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SETUP_FILE.write_text(json.dumps(d, indent=2))
    except Exception:
        pass
def _have(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False
def _missing_deps() -> list:
    return [d for d in _DEPENDENCIES if not _have(d[1])]
def _in_venv() -> bool:
    return (hasattr(sys, "real_prefix")
            or sys.prefix != getattr(sys, "base_prefix", sys.prefix)
            or bool(os.environ.get("VIRTUAL_ENV")))
def _install_target() -> str:
    if _in_venv():
        return os.environ.get("VIRTUAL_ENV") or sys.prefix
    try:
        return _site.getusersitepackages()
    except Exception:
        return "your user site-packages"
def _ensure_pip() -> bool:
    if _have("pip"):
        return True
    try:
        subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"],
                       capture_output=True, timeout=240)
    except Exception:
        pass
    importlib.invalidate_caches()
    return _have("pip")
def _pip_command(pkgs: list, user: bool, break_system: bool) -> list:
    cmd = [sys.executable, "-m", "pip", "install",
           "--disable-pip-version-check", "--no-input",
           "--progress-bar", "off"]
    if user:
        cmd.append("--user")
    if break_system:
        cmd.append("--break-system-packages")
    return cmd + pkgs
_STAGE_WORDS = [
    ("Successfully installed", "installed"),
    ("Installing collected",   "installing"),
    ("Building wheel",         "building wheel"),
    ("Preparing metadata",     "preparing"),
    ("Downloading",            "downloading"),
    ("Using cached",           "using cache"),
    ("Collecting",             "collecting"),
    ("Requirement already",    "already present"),
]
def _stage_from_line(line: str) -> str:
    for needle, word in _STAGE_WORDS:
        if needle in line:
            return word
    return ""
def _run_pip_live(pkgs: list, panel: _LivePanel, title: str) -> tuple:
    """Install `pkgs` one at a time behind a live progress panel.

    Returns (ok: bool, failed: list[str], transcript: str)
    """
    w = _boot_width()
    bar_w = max(18, w - 26)
    states = {p: "queued" for p in pkgs}
    done: list = []
    failed: list = []
    transcript: list = []
    started = time.time()

    def render(current, stage, frame):
        el = int(time.time() - started)
        lines = _boot_header(
            title,
            f"{len(done)}/{len(pkgs)} complete   ·   {el // 60}:{el % 60:02d} elapsed")
        for p in pkgs:
            st = states[p]
            if st == "done":
                glyph, col, txt = _Glyph.TICK, _Ink.GREEN, "ready"
            elif st == "failed":
                glyph, col, txt = _Glyph.CROSS, _Ink.RED, "failed"
            elif st == "active":
                glyph, col = _Glyph.SPIN[frame % len(_Glyph.SPIN)], _Ink.CYAN
                txt = stage or "working"
            else:
                glyph, col, txt = _Glyph.DOT, _Ink.GREY, "queued"
            name_col = _Ink.WHITE if st == "active" else _Ink.GREY
            name = (p[:15] + ("…" if _Glyph.unicode else "~")) if len(p) > 16 else p
            lines.append(f"    {col}{glyph}{_Ink.RESET}  "
                         f"{name_col}{name:<18}{_Ink.RESET}"
                         f"{_Ink.GREY}{txt}{_Ink.RESET}")
        frac = len(done) / max(1, len(pkgs))
        if current and states.get(current) == "active":
            frac += (1.0 / len(pkgs)) * 0.5
        last = _strip_ansi(transcript[-1]) if transcript else "starting pip..."
        lines += [
            "",
            f"    {_bar(frac, bar_w, _Ink.CYAN)}  "
            f"{_Ink.CYAN}{_Ink.BOLD}{int(frac * 100):>3}%{_Ink.RESET}",
            "",
            f"    {_Ink.GREY}{last[:w - 8]}{_Ink.RESET}",
            "",
        ]
        panel.draw(lines)

    if not panel.live:
        _say(f"  {title.lower()}: {', '.join(pkgs)}")
    render(None, "", 0)

    # Flag ladder, tried in order until one works. Whatever succeeds is
    # remembered and used first for the remaining packages.
    #   --user            keeps everything inside the user's home
    #   --break-system... the PEP-668 escape hatch (Homebrew / Debian pythons)
    if _in_venv():
        ladder = [(False, False), (False, True)]
    else:
        ladder = [(True, False), (True, True), (False, True)]

    for pkg in pkgs:
        states[pkg] = "active"
        ok = False
        for attempt, (user, break_system) in enumerate(ladder):
            cmd = _pip_command([pkg], user=user, break_system=break_system)
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, universal_newlines=True,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"})
            except Exception as e:
                transcript.append(f"could not start pip: {e}")
                break

            box = {"stage": "collecting"}
            out_lines: list = []

            def pump(p=proc, b=box, sink=out_lines):
                try:
                    for ln in p.stdout:              # type: ignore[union-attr]
                        ln = ln.rstrip()
                        if not ln:
                            continue
                        sink.append(ln)
                        st = _stage_from_line(ln)
                        if st:
                            b["stage"] = st
                        transcript.append(ln.strip()[:200])
                except Exception:
                    pass

            t = threading.Thread(target=pump, daemon=True)
            t.start()

            frame = 0
            deadline = time.time() + 900
            while proc.poll() is None:
                render(pkg, box["stage"], frame)
                frame += 1
                time.sleep(0.08)
                if time.time() > deadline:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    transcript.append("timed out after 15 minutes")
                    break
            t.join(timeout=2)

            if proc.returncode == 0:
                ok = True
                # This flag combination works here — use it first from now on.
                if attempt:
                    ladder.insert(0, ladder.pop(attempt))
                break

            low = "\n".join(out_lines).lower()
            if ("could not find a version" in low
                    or "no matching distribution" in low):
                transcript.append("pip couldn't find that package — not retrying")
                break
            if ("network is unreachable" in low
                    or "temporary failure in name resolution" in low
                    or "failed to establish a new connection" in low
                    or "connection to pypi.org timed out" in low):
                transcript.append("no route to PyPI — check your connection")
                break
            if attempt + 1 < len(ladder):
                nxt = ladder[attempt + 1]
                transcript.append(
                    "retrying with"
                    + (" --user" if nxt[0] else " no --user")
                    + (" --break-system-packages" if nxt[1] else ""))

        states[pkg] = "done" if ok else "failed"
        (done if ok else failed).append(pkg)
        if not panel.live:
            _say(f"    {'ok  ' if ok else 'FAIL'} {pkg}")
        render(pkg, "", 0)

    render(None, "", 0)
    return (not failed), failed, "\n".join(transcript[-60:])
def _consent_screen(missing: list) -> str:
    """Show exactly what will be installed and ask. Returns 'y', 'n' or 'q'."""
    w = _boot_width()
    required = [d for d in missing if d[3]]
    _say("\n".join(_boot_header("AUTO CLICKER  ·  first-run setup",
                                 f"v{VERSION}   ·   {platform_label()}")))
    n = len(missing)
    _say(f"  {_Ink.WHITE}AutoClicker needs {n} Python package"
          f"{'s' if n != 1 else ''} that {'are' if n != 1 else 'is'} not "
          f"installed on this machine yet.{_Ink.RESET}\n")
    for pip_name, _mod, why, req in missing:
        tag = (f"{_Ink.ORANGE}required{_Ink.RESET}" if req
               else f"{_Ink.GREY}optional{_Ink.RESET}")
        size = _DEP_SIZES.get(pip_name, "")
        _say(f"    {_Ink.CYAN}{_Glyph.BULLET}{_Ink.RESET} {_Ink.WHITE}{pip_name:<11}{_Ink.RESET}"
              f"{_Ink.GREY}{size:>8}   {why:<26}{_Ink.RESET}{tag}")
    _say()
    _say(f"  {_Ink.GREY}installs into  {_Ink.RESET}{_install_target()}")
    _say(f"  {_Ink.GREY}downloaded from{_Ink.RESET} PyPI (pypi.org) over HTTPS")
    _say(f"  {_Ink.GREY}scope          {_Ink.RESET}"
          + ("this virtual environment only" if _in_venv()
             else "your user account only — system Python is untouched"))
    _say()
    _say("  " + _rule(w - 2))
    if not (_boot_tty() and _boot_stdin_tty()):
        # Nothing to type into — say so plainly rather than hanging on input.
        _say(f"  {_Ink.ORANGE}no interactive terminal — proceeding with the "
              f"install shown above.{_Ink.RESET}")
        _say(f"  {_Ink.GREY}run with --no-install to stop that.{_Ink.RESET}\n")
        return "y"
    _say(f"  {_Ink.GREEN}{_Ink.BOLD}[Y]{_Ink.RESET} install now"
          f"      {_Ink.ORANGE}{_Ink.BOLD}[N]{_Ink.RESET} not now"
          f"      {_Ink.RED}{_Ink.BOLD}[Q]{_Ink.RESET} quit"
          + (f"      {_Ink.GREY}(N leaves the app unable to start)"
             f"{_Ink.RESET}" if required else ""))
    _say()
    return _boot_ask(f"  {_Ink.CYAN}{_Glyph.ARROW}{_Ink.RESET} ", "ynq", "y")