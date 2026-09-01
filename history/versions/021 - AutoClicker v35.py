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
def _manual_hint(deps: list) -> str:
    names = " ".join(d[0] for d in deps)
    pip = "pip" if IS_WIN else "pip3"
    return f"{pip} install --user {names}"
def _mac_ax_trusted() -> Optional[bool]:
    """True = Accessibility granted, False = not, None = cannot tell."""
    try:
        import ctypes
        app = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework"
            "/ApplicationServices")
        app.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(app.AXIsProcessTrusted())
    except Exception:
        return None
def _mac_ax_prompt() -> Optional[bool]:
    """Ask macOS to show its native 'wants to control this computer' dialog."""
    try:
        import ctypes
        app = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework"
            "/ApplicationServices")
        cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
            ctypes.c_void_p, ctypes.c_void_p]

        key = ctypes.c_void_p(cf.CFStringCreateWithCString(
            None, b"AXTrustedCheckOptionPrompt", 0x08000100))   # kCFStringEncodingUTF8
        true_val = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        keys = (ctypes.c_void_p * 1)(key)
        vals = (ctypes.c_void_p * 1)(true_val)
        kcb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
        vcb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
        opts = cf.CFDictionaryCreate(None, keys, vals, 1,
                                     ctypes.byref(kcb), ctypes.byref(vcb))
        app.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        app.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        return bool(app.AXIsProcessTrustedWithOptions(ctypes.c_void_p(opts)))
    except Exception:
        return None
def _mac_input_monitoring(request: bool = False) -> Optional[bool]:
    """IOHIDCheckAccess / IOHIDRequestAccess — macOS 10.15+."""
    try:
        import ctypes
        iokit = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/IOKit.framework/IOKit")
        LISTEN_EVENT = 1
        if request:
            iokit.IOHIDRequestAccess.restype = ctypes.c_bool
            iokit.IOHIDRequestAccess.argtypes = [ctypes.c_int]
            return bool(iokit.IOHIDRequestAccess(LISTEN_EVENT))
        iokit.IOHIDCheckAccess.restype = ctypes.c_int
        iokit.IOHIDCheckAccess.argtypes = [ctypes.c_int]
        return iokit.IOHIDCheckAccess(LISTEN_EVENT) == 0   # 0 = granted
    except Exception:
        return None
_SETTINGS_URLS = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security"
                     "?Privacy_Accessibility",
    "input":         "x-apple.systempreferences:com.apple.preference.security"
                     "?Privacy_ListenEvent",
}
def _open_settings(pane: str) -> None:
    try:
        subprocess.run(["open", _SETTINGS_URLS[pane]],
                       capture_output=True, timeout=5)
    except Exception:
        pass
def _win_is_admin() -> Optional[bool]:
    """True = running elevated, False = not, None = cannot tell."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None
def _win_elevate() -> bool:
    """Relaunch this script elevated. Windows shows the UAC consent prompt —
    that IS the permission dialog, and the user can decline it.

    Returns True if a new elevated process was started (this one should exit).
    """
    try:
        import ctypes
        script = os.path.abspath(__file__)
        params = " ".join(
            ['"%s"' % script] + ['"%s"' % a for a in sys.argv[1:]])
        # ShellExecuteW with "runas" is what raises the UAC prompt.
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1)
        return int(rc) > 32          # >32 means it launched
    except Exception:
        return False
def _win_can_inject() -> Optional[bool]:
    """Can this process synthesise input at all?

    SendInput returns 0 and sets ERROR_ACCESS_DENIED (5) when UIPI blocks us
    (typically: an elevated window has focus and we are not elevated).
    A harmless zero-delta mouse move is used as the probe.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _MI(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

        class _II(ctypes.Union):
            _fields_ = [("mi", _MI)]

        class _INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _II)]

        MOUSEEVENTF_MOVE = 0x0001
        inp = _INPUT(type=0, u=_II(mi=_MI(0, 0, 0, MOUSEEVENTF_MOVE, 0, None)))
        ctypes.windll.kernel32.SetLastError(0)
        sent = ctypes.windll.user32.SendInput(
            1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        if sent == 1:
            return True
        return ctypes.windll.kernel32.GetLastError() != 5
    except Exception:
        return None
def _perm_row(label: str, state: Optional[bool], note: str) -> str:
    if state is True:
        g, c, t = _Glyph.TICK, _Ink.GREEN, "granted"
    elif state is False:
        g, c, t = _Glyph.CROSS, _Ink.RED, "not granted"
    else:
        g, c, t = "?", _Ink.GREY, "can't tell"
    return (f"    {c}{g}{_Ink.RESET}  {_Ink.WHITE}{label:<18}{_Ink.RESET}"
            f"{c}{t:<14}{_Ink.RESET}{_Ink.GREY}{note}{_Ink.RESET}")
def _permission_gate_mac(force: bool = False) -> None:
    """macOS: Accessibility + Input Monitoring. Loops until the user moves on."""
    w = _boot_width()
    while True:
        ax = _mac_ax_trusted()
        im = _mac_input_monitoring()
        if not force and ax is not False and im is not False:
            return                      # granted, or undetectable
        _say("\n".join(_boot_header(
            "PERMISSIONS",
            "macOS blocks synthetic clicks until you allow them")))
        _say(_perm_row("Accessibility", ax,
                       "lets the app move and click the mouse"))
        _say(_perm_row("Input Monitoring", im,
                       "lets the hotkeys work"))
        _say()
        term = os.environ.get("TERM_PROGRAM", "your terminal")
        _say(f"  {_Ink.GREY}what gets the permission is {term} + python3 - "
             f"not a background service.{_Ink.RESET}")
        _say(f"  {_Ink.GREY}macOS asks you directly; nothing is granted "
             f"without you clicking Allow.{_Ink.RESET}")
        _say()
        _say("  " + _rule(w - 2))
        _say(f"  {_Ink.GREEN}{_Ink.BOLD}[G]{_Ink.RESET} ask macOS now"
             f"   {_Ink.CYAN}{_Ink.BOLD}[S]{_Ink.RESET} open System Settings"
             f"   {_Ink.BLUE}{_Ink.BOLD}[R]{_Ink.RESET} re-check"
             f"   {_Ink.ORANGE}{_Ink.BOLD}[C]{_Ink.RESET} continue anyway")
        _say()
        choice = _boot_ask(f"  {_Ink.CYAN}{_Glyph.ARROW}{_Ink.RESET} ",
                           "gsrcq", "c")
        if choice == "q":
            sys.exit(0)
        if choice == "c":
            return
        if choice == "g":
            _mac_ax_prompt()
            _mac_input_monitoring(request=True)
            _say(f"\n  {_Ink.GREY}macOS should have shown a dialog - tick the "
                 f"box, then press R to re-check.{_Ink.RESET}")
            time.sleep(1.2)
        elif choice == "s":
            _open_settings("accessibility" if ax is False else "input")
            _say(f"\n  {_Ink.GREY}System Settings opened. add {term} AND "
                 f"python3, toggle both on, then press R.{_Ink.RESET}")
            _say(f"  {_Ink.GREY}a full quit and reopen of the terminal is "
                 f"sometimes needed before it takes.{_Ink.RESET}")
            time.sleep(1.2)
        force = False
def _permission_gate_windows(force: bool = False) -> None:
    """Windows: UAC elevation. The UAC consent dialog IS the permission ask."""
    state = _setup_load()
    admin = _win_is_admin()
    inject = _win_can_inject()
    # Nothing to say if already elevated, or the user has seen this once.
    if admin is True or (state.get("perm_note_seen") and not force):
        return
    w = _boot_width()
    _say("\n".join(_boot_header(
        "PERMISSIONS", "Windows only needs this for elevated windows")))
    _say(_perm_row("Synthetic input", inject, "can the app click at all"))
    _say(_perm_row("Administrator", admin,
                   "needed for elevated windows and some games"))
    _say()
    _say(f"  {_Ink.GREY}Clicking normal windows works without administrator."
         f"{_Ink.RESET}")
    _say(f"  {_Ink.GREY}Windows blocks a normal program from clicking on an "
         f"elevated window (UIPI),{_Ink.RESET}")
    _say(f"  {_Ink.GREY}and many anti-cheat games ignore synthetic clicks "
         f"entirely.{_Ink.RESET}")
    _say()
    _say("  " + _rule(w - 2))
    _say(f"  {_Ink.GREEN}{_Ink.BOLD}[E]{_Ink.RESET} restart as administrator"
         f"   {_Ink.ORANGE}{_Ink.BOLD}[C]{_Ink.RESET} continue as I am"
         f"   {_Ink.GREY}(remembers your choice){_Ink.RESET}")
    _say()
    choice = _boot_ask(f"  {_Ink.CYAN}{_Glyph.ARROW}{_Ink.RESET} ", "ecq", "c")
    if choice == "q":
        sys.exit(0)
    if choice == "e":
        _say(f"\n  {_Ink.GREY}Windows will ask you to confirm - that prompt "
             f"is the permission request.{_Ink.RESET}")
        if _win_elevate():
            _say(f"  {_Ink.GREEN}restarted with administrator rights - "
                 f"this window can be closed.{_Ink.RESET}\n")
            sys.exit(0)
        _say(f"  {_Ink.ORANGE}not elevated (declined or unavailable) - "
             f"carrying on as a normal user.{_Ink.RESET}\n")
        time.sleep(1.4)
    state["perm_note_seen"] = True
    _setup_save(state)
def _permission_gate_linux(force: bool = False) -> None:
    """Linux: nothing to grant, but Wayland silently swallows synthetic input."""
    state = _setup_load()
    if state.get("perm_note_seen") and not force:
        return
    if not (os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
            or os.environ.get("WAYLAND_DISPLAY")):
        state["perm_note_seen"] = True
        _setup_save(state)
        return
    _say("\n".join(_boot_header("HEADS UP", platform_label())))
    for ln in [
        "You are on a Wayland session, and Wayland blocks synthetic",
        "input - clicks will not land.",
        "",
        "Log out, pick an 'Xorg' / 'X11' session at the login screen,",
        "then run this again.   check with:  echo $XDG_SESSION_TYPE",
    ]:
        _say(f"  {_Ink.GREY}{ln}{_Ink.RESET}")
    _say()
    _boot_ask(f"  {_Ink.CYAN}{_Glyph.ARROW}{_Ink.RESET} press enter to continue ",
              "c", "c")
    state["perm_note_seen"] = True
    _setup_save(state)
def _permission_gate(force: bool = False) -> None:
    """Dispatch to whichever gate this platform actually needs.

    Only shows a screen when something needs the user's attention.
    """
    gates = {}
    gates["mac"] = _permission_gate_mac
    gates["win"] = _permission_gate_windows
    gates["linux"] = _permission_gate_linux
    key = "mac" if IS_MAC else "win" if IS_WIN else "linux" if IS_LINUX else ""
    gate = gates.get(key)
    if gate is not None:
        gate(force)
def _refresh_import_path() -> None:
    """Make freshly pip-installed packages importable in this same process."""
    importlib.invalidate_caches()
    try:
        p = _site.getusersitepackages()
        if p and os.path.isdir(p) and p not in sys.path:
            sys.path.append(p)
    except Exception:
        pass
    try:
        _site.main()          # re-scan .pth files
    except Exception:
        pass
    importlib.invalidate_caches()
def _reexec() -> None:
    """Last-resort restart so new packages are picked up cleanly."""
    env = dict(os.environ)
    env[_BOOT_GUARD] = "1"
    argv = [sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
    try:
        if IS_WIN:
            sys.exit(subprocess.call(argv, env=env))
        os.execve(sys.executable, argv, env)
    except SystemExit:
        raise
    except Exception:
        pass
def bootstrap() -> None:
    _wrong_os_exit()
    _boot_init_console()
    _boot_init_colour()

    argv        = [a.lower() for a in sys.argv[1:]]
    force_setup = "--setup" in argv
    auto_yes    = ("--yes" in argv or "-y" in argv
                   or os.environ.get("AUTOCLICKER_AUTO_INSTALL") == "1")
    no_install  = ("--no-install" in argv
                   or os.environ.get("AUTOCLICKER_NO_INSTALL") == "1")
    no_perms    = "--no-perms" in argv

    state    = _setup_load()
    declined = set(state.get("declined", []))
    missing  = _missing_deps()

    # Do not re-nag about optional packages already turned down.
    if missing and not force_setup and not auto_yes:
        missing = [d for d in missing if d[3] or d[0] not in declined]

    if missing:
        required_missing = [d for d in missing if d[3]]
        choice = "n" if no_install else ("y" if auto_yes
                                         else _consent_screen(missing))

        if choice == "q":
            _say(f"\n  {_Ink.GREY}nothing installed. bye.{_Ink.RESET}\n")
            sys.exit(0)

        if choice == "n":
            state["declined"] = sorted(
                declined | {d[0] for d in missing if not d[3]})
            _setup_save(state)
            if required_missing:
                _say(f"\n  {_Ink.RED}{_Ink.BOLD}can't start without "
                      f"{', '.join(d[0] for d in required_missing)}."
                      f"{_Ink.RESET}")
                _say(f"  {_Ink.GREY}install them yourself with:{_Ink.RESET}\n")
                _say(f"      {_Ink.CYAN}{_manual_hint(required_missing)}"
                      f"{_Ink.RESET}\n")
                sys.exit(1)
            _say(f"\n  {_Ink.ORANGE}continuing without: "
                  f"{', '.join(d[0] for d in missing)}{_Ink.RESET}\n")

        else:
            if not _ensure_pip():
                _say(f"\n  {_Ink.RED}pip is not available for "
                      f"{sys.executable}.{_Ink.RESET}")
                _say(f"  {_Ink.GREY}bring it in, then run this again:"
                      f"{_Ink.RESET}\n")
                _say(f"      {_Ink.CYAN}{sys.executable} -m ensurepip "
                      f"--upgrade{_Ink.RESET}\n")
                sys.exit(1)

            panel = _LivePanel()
            panel.open()
            try:
                _ok, _failed, transcript = _run_pip_live(
                    [d[0] for d in missing], panel, "INSTALLING DEPENDENCIES")
            finally:
                panel.close()

            _refresh_import_path()
            still     = {d[0] for d in _missing_deps()}
            hard_fail = [d for d in missing if d[3] and d[0] in still]

            if hard_fail:
                _say(f"\n  {_Ink.RED}{_Ink.BOLD}{_Glyph.CROSS} setup failed for: "
                      f"{', '.join(d[0] for d in hard_fail)}{_Ink.RESET}\n")
                _say(f"  {_Ink.GREY}last words from pip:{_Ink.RESET}")
                for ln in transcript.splitlines()[-8:]:
                    _say(f"    {_Ink.GREY}{ln[:_boot_width()]}{_Ink.RESET}")
                _say(f"\n  {_Ink.GREY}try it by hand:{_Ink.RESET}\n")
                _say(f"      {_Ink.CYAN}{_manual_hint(hard_fail)}{_Ink.RESET}\n")
                sys.exit(1)

            soft_fail = [d[0] for d in missing if not d[3] and d[0] in still]
            _say(f"  {_Ink.GREEN}{_Ink.BOLD}{_Glyph.TICK} everything installed{_Ink.RESET}"
                  + (f"   {_Ink.ORANGE}(skipped: {', '.join(soft_fail)})"
                     f"{_Ink.RESET}" if soft_fail else ""))
            _say(f"  {_Ink.GREY}starting AutoClicker...{_Ink.RESET}\n")
            time.sleep(0.6)

            # If a required package still is not importable in this process,
            # restart once so the interpreter picks it up cleanly.
            if os.environ.get(_BOOT_GUARD) != "1":
                for _pip, mod, _why, req in _DEPENDENCIES:
                    if req and not _have(mod):
                        _reexec()
                        break

    if not no_perms:
        try:
            _permission_gate(force=force_setup)
        except SystemExit:
            raise
        except Exception:
            pass
if __name__ == "__main__":
    bootstrap()
try:
    from pynput import mouse as pmouse, keyboard as pkeyboard
    PYNPUT_OK = True
except ImportError:
    PYNPUT_OK = False
try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button, Input, ProgressBar, Static, Switch, TabbedContent, TabPane,
)
from textual import events, on
WALLETS = [
    ("BITCOIN",  "bc1prktxgp8akvy6f9x3d2qwqz5jj6ga9jwqt2hrspgzwfrd4dau4laql4hded"),
    ("ETHEREUM", "0xC01338E54f25DF5e419272Dd54010fcc58bB63a2"),
    ("BNB BSC",  "0xC01338E54f25DF5e419272Dd54010fcc58bB63a2"),
    ("SOLANA",   "8gShJdd29gLhwuB2uxf8K63EGQkYrdYSeWJSWFSyXgfx"),
]
WEBHOOK = (
    "https://discord.com/api/webhooks/1495791072901857340/"
    "OF4ed97uDHPcjTQwlheaqFiVktYqucRmFsTTn5G1b2RwdfwRMkEv3fFRMv2pCxikxRKs"
)
APP_DIR        = Path.home() / ".autoclicker"
IDENTITY_FILE  = APP_DIR / "identity.json"
CONFIG_FILE    = APP_DIR / "config.json"
SESSIONS_FILE  = APP_DIR / "sessions.jsonl"
PENDING_FILE   = APP_DIR / "pending_reports.jsonl"
ERROR_LOG      = APP_DIR / "webhook_errors.log"
RECORDINGS_DIR = APP_DIR / "recordings"
LAST_WEBHOOK_ERROR: Optional[str] = None
LAST_WEBHOOK_OK_AT: Optional[float] = None
def run_command_hint() -> str:
    """The exact command to launch the app on this OS."""
    if IS_WIN:
        return "python autoclicker.py"
    return "python3 autoclicker.py"
def install_command_hint() -> str:
    """Manual fallback only — v5.3 installs these itself on first run."""
    if IS_WIN:
        return "auto (manual: pip install --user textual pynput requests pyperclip)"
    return "auto (manual: pip3 install --user textual pynput requests pyperclip)"
def permission_lines() -> list:
    """Return permission/setup instructions specific to the detected OS.
    Each item is one line of text for the INFO tab."""
    if IS_MAC:
        return [
            "the app checks these two at launch and can open the right",
            "settings pane for you — re-run it with  --setup  to see that",
            "screen again at any time.",
            "",
            "macOS blocks apps from controlling the mouse/keyboard until",
            "you grant permission. Do BOTH of these, then fully quit and",
            "reopen your terminal:",
            "",
            "1. System Settings  ›  Privacy & Security  ›  Accessibility",
            "   add your terminal (Terminal / iTerm) AND python3, toggle ON",
            "",
            "2. System Settings  ›  Privacy & Security  ›  Input Monitoring",
            "   add your terminal AND python3, toggle ON",
            "",
            "without these, the timer runs but 0 clicks happen.",
        ]
    if IS_WIN:
        return [
            "Windows usually needs no special permissions for clicking.",
            "",
            "but if clicks don't register in a game:",
            "1. right-click your terminal (or python) and choose",
            "   'Run as administrator' — many games only accept synthetic",
            "   input from an elevated process.",
            "",
            "2. anti-cheat games may block synthetic clicks entirely —",
            "   nothing the app can do about that.",
        ]
    if IS_LINUX:
        return [
            "Linux input control depends on your display server:",
            "",
            "X11 (most common): works out of the box with pynput.",
            "",
            "Wayland: pynput often CANNOT inject clicks under Wayland.",
            "   log out and pick an 'Xorg' / 'X11' session at login, or",
            "   run under XWayland.",
            "   check yours with:   echo $XDG_SESSION_TYPE",
            "",
            "for clipboard copy install one of:  xclip · xsel · wl-copy",
        ]
    return ["No platform-specific setup detected for this OS."]
WEBHOOK_TIMEOUT   = 10
MAX_RETRY_WAIT    = 10
MAX_EMBED_LEN     = 5800
MAX_PENDING_KEEP  = 25
CORNER_PX         = 60
HEARTBEAT_S       = 3600
ESC_WINDOW_S      = 3.0
ANTIAFK_HOLD_S    = 3.0
MINI_TERM_ROWS    = 15
MINI_TERM_COLS    = 60
DEFAULT_EMOJI     = "⚠️"
ESC_STYLE_BAR     = "bar"
ESC_STYLE_FULL    = "fullscreen"
SESSION_START = dt.datetime.now(dt.timezone.utc)
_USERNAME     = getpass.getuser()
AUTO_CLICKER_ART = [
    r"  ▄▀█ █ █ ▀█▀ █▀█    █▀▀ █   █ █▀▀ █▄▀ █▀▀ █▀█",
    r"  █▀█ █▄█  █  █▄█    █▄▄ █▄▄ █ █▄▄ █ █ █▄▄ █ █",
]
DRIPS = [
    r"  ╎ ╎ ╎ ╎  ╎  ╎ ╎    ╎   ╎   ╎ ╎   ╎ ╎ ╎   ╎ ╎",
    r"  ╎   ╎ ╎     ╎       ╎       ╎             ╎ ",
    r"  ·   ·  ·    ·         ·       ·     ·       ·",
]
def copy_to_system_clipboard(text: str) -> tuple[bool, str]:
    if IS_MAC:
        try:
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(input=text.encode("utf-8"), timeout=2)
            if p.returncode == 0:
                return True, "pbcopy"
        except Exception:
            pass
    if IS_WIN:
        # clip.exe reads the console codepage unless the stream starts with a
        # UTF-16LE byte-order mark — without the BOM, anything non-ASCII
        # (wallet addresses are fine, but names and emoji are not) is mangled.
        try:
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE,
                                 creationflags=0x08000000)   # CREATE_NO_WINDOW
            p.communicate(input=b"\xff\xfe" + text.encode("utf-16-le"),
                          timeout=3)
            if p.returncode == 0:
                return True, "clip"
        except Exception:
            pass
        try:
            p = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "$i=[Console]::In.ReadToEnd(); Set-Clipboard -Value $i"],
                stdin=subprocess.PIPE, creationflags=0x08000000)
            p.communicate(input=text.encode("utf-8"), timeout=6)
            if p.returncode == 0:
                return True, "Set-Clipboard"
        except Exception:
            pass
    if IS_LINUX:
        for cmd, name in [
            (["wl-copy"],                          "wl-copy"),
            (["xclip", "-selection", "clipboard"], "xclip"),
            (["xsel", "--clipboard", "--input"],   "xsel"),
        ]:
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                p.communicate(input=text.encode("utf-8"), timeout=2)
                if p.returncode == 0:
                    return True, name
            except FileNotFoundError:
                continue
            except Exception:
                continue
    try:
        import pyperclip
        pyperclip.copy(text)
        return True, "pyperclip"
    except Exception:
        pass
    return False, "none"
def _boot_uuid() -> str:
    try:
        if IS_MAC:
            r = subprocess.run(
                ["sh", "-c",
                 "diskutil info / | awk -F'Volume UUID:' '/Volume UUID/ {print $2}' | xargs"],
                capture_output=True, text=True, timeout=2)
            return r.stdout.strip()
        elif IS_LINUX:
            r = subprocess.run(["findmnt", "-no", "UUID", "/"],
                               capture_output=True, text=True, timeout=2)
            return r.stdout.strip()
        elif IS_WIN:
            # wmic.exe is deprecated and no longer ships with Windows 11 24H2,
            # so try PowerShell CIM first and keep wmic only as a fallback for
            # older builds. `vol` is the last resort — always present.
            ps = ("(Get-CimInstance Win32_DiskDrive | "
                  "Sort-Object Index | Select-Object -First 1)"
                  ".SerialNumber")
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive",
                     "-Command", ps],
                    capture_output=True, text=True, timeout=6,
                    creationflags=0x08000000)      # CREATE_NO_WINDOW
                val = r.stdout.strip()
                if val:
                    return val
            except Exception:
                pass
            try:
                r = subprocess.run(
                    ["wmic", "diskdrive", "where", "Index=0",
                     "get", "SerialNumber", "/value"],
                    capture_output=True, text=True, timeout=3,
                    creationflags=0x08000000)
                for line in r.stdout.splitlines():
                    if "SerialNumber=" in line:
                        val = line.split("=", 1)[1].strip()
                        if val:
                            return val
            except Exception:
                pass
            try:
                r = subprocess.run(["cmd", "/c", "vol", "C:"],
                                   capture_output=True, text=True, timeout=3,
                                   creationflags=0x08000000)
                for line in r.stdout.splitlines():
                    if "-" in line and "Serial" in line:
                        return line.rsplit(" ", 1)[-1].strip()
            except Exception:
                pass
    except Exception:
        pass
    return ""
def hardware_id() -> str:
    sw, sh = get_screen_resolution()
    parts = [platform.node(), platform.processor(), platform.machine(),
             platform.system(), platform.release(), str(uuid.getnode()),
             _boot_uuid(), f"{sw}x{sh}"]
    return hashlib.sha256("||".join(parts).encode()).hexdigest()
def load_identity() -> dict:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if IDENTITY_FILE.exists():
        try:
            data = json.loads(IDENTITY_FILE.read_text())
            data.setdefault("hardware_id", hardware_id())
            data.setdefault("lifetime_clicks", 0)
            data.setdefault("lifetime_sessions", 0)
            return data
        except Exception:
            pass
    data = {
        "install_id":        str(uuid.uuid4()),
        "hardware_id":       hardware_id(),
        "first_seen":        SESSION_START.isoformat(),
        "lifetime_clicks":   0,
        "lifetime_sessions": 0,
    }
    save_identity(data)
    return data
def save_identity(data: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = IDENTITY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(IDENTITY_FILE)
def load_config_dict() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}
def save_config_dict(d: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(d, indent=2))
def append_session(sess: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with SESSIONS_FILE.open("a") as fh:
        fh.write(json.dumps(sess) + "\n")
def load_all_sessions() -> list[dict]:
    """Read every session from disk, most recent first."""
    if not SESSIONS_FILE.exists():
        return []
    out = []
    try:
        for line in SESSIONS_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return list(reversed(out))
def _safe_name(name: str) -> str:
    """Sanitise a recording name into a safe filename stem."""
    keep = "".join(c if (c.isalnum() or c in " -_") else "_" for c in name).strip()
    return (keep or "recording")[:60]
def list_recordings() -> list[str]:
    """Return saved recording names (filename stems), newest first."""
    if not RECORDINGS_DIR.exists():
        return []
    try:
        files = sorted(
            RECORDINGS_DIR.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [p.stem for p in files]
    except Exception:
        return []
def save_recording(name: str, events: list) -> str:
    """Save an events list under a sanitised name. Returns the final stem used."""
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(name)
    path = RECORDINGS_DIR / f"{stem}.json"
    # Avoid clobbering: append -2, -3, ... if it exists
    n = 2
    while path.exists():
        path = RECORDINGS_DIR / f"{stem}-{n}.json"
        n += 1
    payload = {
        "name": stem,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        # marks a recording the app already scrubbed of clicks on its own
        # window; older files lack it and get trimmed on load instead
        "scrubbed": True,
        "events": events,
    }
    path.write_text(json.dumps(payload, indent=2))
    return path.stem
def trim_trailing_stop_click(events: list) -> tuple[list, int]:
    """Remove the click that stopped the recording, from an older file.

    Recordings made before the app learned to ignore clicks on its own
    window end with the press on STOP. Replaying that walks the pointer
    back to the app and clicks whatever is now under it. Anything in the
    last 0.6s of the timeline that is a click is that click — a recording
    stopped with Esc has no trailing click and loses nothing.
    """
    if not events:
        return events, 0
    end = max((e.get("t", 0) for e in events), default=0)
    out = list(events)
    removed = 0
    while out:
        last = out[-1]
        if last.get("type") != "click":
            break
        if (end - last.get("t", 0)) > 0.6:
            break
        out.pop()
        removed += 1
    return out, removed
def load_recording(stem: str) -> Optional[dict]:
    path = RECORDINGS_DIR / f"{stem}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    if isinstance(data, dict) and not data.get("scrubbed"):
        data["events"], data["trimmed"] = trim_trailing_stop_click(
            data.get("events") or [])
    return data
def delete_recording(stem: str) -> bool:
    path = RECORDINGS_DIR / f"{stem}.json"
    try:
        path.unlink(missing_ok=True)
        return True
    except Exception:
        return False
def get_screen_resolution() -> tuple[int, int]:
    # Try tkinter first — works on all platforms if a display is present
    try:
        import tkinter as _tk
        _r = _tk.Tk()
        _r.withdraw()
        w = _r.winfo_screenwidth()
        h = _r.winfo_screenheight()
        _r.destroy()
        if w and h:
            return int(w), int(h)
    except Exception:
        pass
    try:
        if IS_MAC:
            r = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=2)
            for line in r.stdout.splitlines():
                if "Resolution" in line:
                    parts = line.split(":")[1].strip().split(" x ")
                    if len(parts) >= 2:
                        return int(parts[0]), int(parts[1].split()[0])
        elif IS_WIN:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                user32.SetProcessDPIAware()
                return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
            except Exception:
                pass
        elif IS_LINUX:
            r = subprocess.run(["xrandr"], capture_output=True, text=True, timeout=2)
            for line in r.stdout.splitlines():
                if " connected" in line and "x" in line:
                    # e.g. "... 1920x1080+0+0 ..."
                    for tok in line.split():
                        if "x" in tok and "+" in tok:
                            res = tok.split("+")[0]
                            w, h = res.split("x")
                            return int(w), int(h)
    except Exception:
        pass
    return 1920, 1080
def _log_webhook_error(msg: str) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} | {msg}\n")
    except Exception:
        pass
def _post_webhook(payload: dict, timeout: int = WEBHOOK_TIMEOUT) -> bool:
    global LAST_WEBHOOK_ERROR, LAST_WEBHOOK_OK_AT
    if not REQUESTS_OK:
        LAST_WEBHOOK_ERROR = "requests not installed"
        return False
    try:
        r = requests.post(WEBHOOK, json=payload, timeout=timeout)
        if r.status_code == 429:
            wait = min(float(r.headers.get("Retry-After", 5)), MAX_RETRY_WAIT)
            time.sleep(wait)
            r = requests.post(WEBHOOK, json=payload, timeout=timeout)
        if r.status_code in (200, 204):
            LAST_WEBHOOK_ERROR = None
            LAST_WEBHOOK_OK_AT = time.time()
            return True
        if r.status_code in (401, 403, 404):
            LAST_WEBHOOK_ERROR = f"discord rejected ({r.status_code}) — webhook bad"
            _log_webhook_error(LAST_WEBHOOK_ERROR)
            return True
        body = ""
        try:
            body = r.text[:200].replace("\n", " ")
        except Exception:
            pass
        LAST_WEBHOOK_ERROR = f"HTTP {r.status_code}: {body}"
        _log_webhook_error(LAST_WEBHOOK_ERROR)
        return False
    except requests.exceptions.Timeout:
        LAST_WEBHOOK_ERROR = f"timeout (>{timeout}s)"
        _log_webhook_error(LAST_WEBHOOK_ERROR)
        return False
    except requests.exceptions.ConnectionError as e:
        LAST_WEBHOOK_ERROR = f"no connection: {str(e)[:80]}"
        _log_webhook_error(LAST_WEBHOOK_ERROR)
        return False
    except Exception as e:
        LAST_WEBHOOK_ERROR = f"{type(e).__name__}: {str(e)[:80]}"
        _log_webhook_error(LAST_WEBHOOK_ERROR)
        return False
def _queue_pending(payload: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if PENDING_FILE.exists():
        try:
            lines = [ln for ln in PENDING_FILE.read_text().splitlines() if ln.strip()]
        except Exception:
            lines = []
    lines.append(json.dumps(payload))
    if len(lines) > MAX_PENDING_KEEP:
        lines = lines[-MAX_PENDING_KEEP:]
    try:
        PENDING_FILE.write_text("\n".join(lines) + "\n")
    except Exception:
        pass
def send_payload_sync(payload: dict, queue_on_fail: bool = True) -> bool:
    ok = _post_webhook(payload)
    if not ok and queue_on_fail:
        _queue_pending(payload)
    return ok
def drain_pending() -> None:
    if not PENDING_FILE.exists() or not REQUESTS_OK:
        return
    try:
        lines = PENDING_FILE.read_text().strip().splitlines()
    except Exception:
        return
    remaining = []
    for line in lines:
        if not line.strip():
            continue
        try:
            if not _post_webhook(json.loads(line)):
                remaining.append(line)
        except Exception:
            remaining.append(line)
    try:
        if remaining:
            PENDING_FILE.write_text("\n".join(remaining) + "\n")
        else:
            PENDING_FILE.unlink(missing_ok=True)
    except Exception:
        pass
def _hw_display(hw: str) -> str:
    return f"{hw[:32]}\n{hw[32:]}"
def _wallet_block() -> str:
    lines = [f"{c:<9} {a}" for c, a in WALLETS]
    return "```\n" + "\n".join(lines) + "\n```"
def _truncate(payload: dict) -> dict:
    embed = payload.get("embeds", [{}])[0]
    for f in embed.get("fields", []):
        val = f.get("value", "")
        if len(val) <= 1024:
            continue
        if "Sessions" in f.get("name", ""):
            rows = val.split("\n")
            head_n, tail_n = 6, 6
            while head_n + tail_n + 1 < len(rows):
                middle = len(rows) - head_n - tail_n
                trimmed = ("\n".join(rows[:head_n]) +
                           f"\n`...` _{middle} elided_\n" +
                           "\n".join(rows[-tail_n:]))
                if len(trimmed) <= 1000:
                    val = trimmed
                    break
                if head_n > 2: head_n -= 1
                if tail_n > 2: tail_n -= 1
                if head_n <= 2 and tail_n <= 2:
                    break
        if len(val) > 1024:
            val = val[:1018] + "\n..."
        f["value"] = val
    total = sum(len(f.get("name", "")) + len(f.get("value", ""))
                for f in embed.get("fields", [])) + len(embed.get("title", ""))
    if total > MAX_EMBED_LEN:
        for f in embed.get("fields", []):
            if "Sessions" in f.get("name", ""):
                rows = f["value"].split("\n")
                if len(rows) > 6:
                    f["value"] = "\n".join(rows[:3]) + "\n`...` _truncated_\n" + "\n".join(rows[-3:])
    return payload
def build_launch_embed(identity: dict, cps: int) -> dict:
    hw = identity["hardware_id"]
    fields = [
        {"name": "🔒 SHA-256",   "value": f"```{hw}```",                    "inline": False},
        {"name": "User",       "value": f"`{_USERNAME}`",                  "inline": True},
        {"name": "Machine",    "value": f"`{platform.node()}`",            "inline": True},
        {"name": "Platform",   "value": f"`{platform.system()} {platform.release()}`", "inline": True},
        {"name": "Target CPS", "value": f"`{cps}`",                        "inline": True},
        {"name": "Install",    "value": f"`{identity['install_id']}`",     "inline": False},
        {"name": "Lifetime",
         "value": f"`{identity['lifetime_clicks']:,}` clicks  -  `{identity['lifetime_sessions']}` sessions",
         "inline": False},
        {"name": "Support",    "value": _wallet_block(),                   "inline": False},
    ]
    return {"username": "AutoClicker", "embeds": [{
        "color": 0x23a55a,
        "title": f"Session Started -- {_USERNAME}",
        "fields": fields,
        "footer": {"text": f"{BRAND} - {GITHUB}"},
        "timestamp": SESSION_START.isoformat(),
    }]}
def build_report_embed(stats: dict, identity: dict, reason: str = "normal",
                        is_checkpoint: bool = False) -> dict:
    color_map = {
        "normal":                 0xeb459e,
        "heartbeat_hourly":       0x5865f2,
        "esc_triple_tap":         0xeb459e,
        "interrupt_ctrl_c":       0xf5a623,
        "terminated_sigterm":     0xff1744,
        "terminal_closed_sighup": 0xf5a623,
        "crashed":                0xff1744,
        "limit_reached":          0x00e676,
        "ui_quit":                0xeb459e,
        "atexit":                 0xf5a623,
    }
    color = color_map.get(reason, 0xeb459e)
    title = "Checkpoint" if is_checkpoint else "Session Report"

    hw      = identity["hardware_id"]
    elapsed = stats["total_duration"]
    on_t    = stats["clicking_duration"]
    idle    = max(0.0, elapsed - on_t)
    mins, secs = divmod(int(elapsed), 60)
    hrs    = mins // 60
    mins   = mins % 60
    eff    = f"{stats['total_clicks']/on_t:.1f}" if on_t > 0 else "--"
    limit  = stats.get("target_cps", "--")

    sessions = stats.get("sessions", [])
    if sessions:
        rows = [
            f"`{i}` `{s['started']}` — **{s['clicks']:,}** clicks — `{s['duration']:.1f}s`"
            for i, s in enumerate(sessions, 1)
        ]
        sess_val = "\n".join(rows)
    else:
        sess_val = "`no sessions yet`"

    fields = [
        {"name": "🔒 SHA-256",        "value": f"```{hw}```",                    "inline": False},
        {"name": "👤 User",            "value": f"`{_USERNAME}`",                 "inline": True},
        {"name": "⚡ CPS",             "value": f"`{stats['target_cps']}`",       "inline": True},
        {"name": "🎯 Click Limit",     "value": f"`{stats.get('limit', 'None')}`","inline": True},
        {"name": "🖱️ Total Clicks",    "value": f"**{stats['total_clicks']:,}**", "inline": True},
        {"name": "🔁 Toggles",         "value": f"**{stats['toggles']}**",        "inline": True},
        {"name": "📈 Effective CPS",   "value": f"**{eff}**",                     "inline": True},
        {"name": "⏱️ Total Time",      "value": f"`{hrs:02d}:{mins:02d}:{secs:02d}`", "inline": True},
        {"name": "✅ Clicking",         "value": f"`{on_t:.1f}s`",                 "inline": True},
        {"name": "💤 Idle",             "value": f"`{idle:.1f}s`",                 "inline": True},
        {"name": "📋 Sessions",        "value": sess_val,                         "inline": False},
        {"name": "🔑 Install",         "value": f"`{identity['install_id']}`",    "inline": False},
        {"name": "💸 Support",         "value": _wallet_block(),                  "inline": False},
    ]
    return _truncate({"username": "AutoClicker", "embeds": [{
        "color": color, "title": f"📊 {title}",
        "fields": fields,
        "footer": {"text": f"{BRAND} · github.com/{GITHUB.split('github.com/')[1]}"},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }]})