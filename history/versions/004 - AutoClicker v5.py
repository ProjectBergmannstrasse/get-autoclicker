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