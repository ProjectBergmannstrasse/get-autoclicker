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


# ══════════════════════════════════════════════════════════════════════════════
#  BOOTSTRAP  —  dependency auto-install + OS permission gate
#  ---------------------------------------------------------------------------
#  Runs BEFORE any third-party import, so it uses the standard library only.
#  Nothing is installed and no permission is requested without you saying yes.
#
#  Flags:   --yes / -y      approve the install without being asked
#           --no-install    never install, just report what is missing
#           --setup         force the full setup screens even if all is well
#           --no-perms      skip the OS permission gate
#  Env:     AUTOCLICKER_AUTO_INSTALL=1   same as --yes
#           AUTOCLICKER_NO_INSTALL=1     same as --no-install
#           NO_COLOR=1                   plain text, no ANSI colour
# ══════════════════════════════════════════════════════════════════════════════

import importlib
import importlib.util
import shutil as _shutil
import site as _site

_BOOT_GUARD = "_AUTOCLICKER_BOOTSTRAPPED"
_SETUP_FILE = Path.home() / ".autoclicker" / "setup.json"

#   pip name       import name    what it is for                  required
_DEPENDENCIES = [
    ("textual",   "textual",   "terminal user interface",       True),
    ("pynput",    "pynput",    "mouse + keyboard control",      True),
    ("requests",  "requests",  "session reporting",             False),
    ("pyperclip", "pyperclip", "clipboard copy fallback",       False),
]

# Approximate wheel sizes — only so the consent screen shows an honest number.
_DEP_SIZES = {"textual": "1.4 MB", "pynput": "0.1 MB",
              "requests": "0.1 MB", "pyperclip": "0.02 MB"}


# ─── tiny ANSI toolkit (no rich, no textual — they may not exist yet) ─────────

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


# ─── setup memory (so we do not nag on every launch) ─────────────────────────

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


# ─── dependency detection ────────────────────────────────────────────────────

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


# ─── the live install screen ─────────────────────────────────────────────────

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


# ─── consent screen ──────────────────────────────────────────────────────────

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


# ─── OS permission gate ──────────────────────────────────────────────────────

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


# ─── orchestration ───────────────────────────────────────────────────────────

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

# ══════════════════════════════════════════════════════════════════════════════
#  END BOOTSTRAP — third-party imports below are safe from here on
# ══════════════════════════════════════════════════════════════════════════════


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


# ── Wallets / webhook ─────────────────────────────────────────────────────────

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


# ─── ASCII art header ─────────────────────────────────────────────────────────

AUTO_CLICKER_ART = [
    r"  ▄▀█ █ █ ▀█▀ █▀█    █▀▀ █   █ █▀▀ █▄▀ █▀▀ █▀█",
    r"  █▀█ █▄█  █  █▄█    █▄▄ █▄▄ █ █▄▄ █ █ █▄▄ █ █",
]
DRIPS = [
    r"  ╎ ╎ ╎ ╎  ╎  ╎ ╎    ╎   ╎   ╎ ╎   ╎ ╎ ╎   ╎ ╎",
    r"  ╎   ╎ ╎     ╎       ╎       ╎             ╎ ",
    r"  ·   ·  ·    ·         ·       ·     ·       ·",
]


# ─── Clipboard ────────────────────────────────────────────────────────────────

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


# ─── Identity ─────────────────────────────────────────────────────────────────

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


# ─── Macro recordings (EMULATION) ─────────────────────────────────────────────

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


# ─── Discord webhook ──────────────────────────────────────────────────────────

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


def build_crash_embed(tb: str, identity: dict) -> dict:
    hw = identity["hardware_id"]
    return {"username": "AutoClicker", "embeds": [{
        "color": 0xed4245, "title": "Crashed",
        "fields": [
            {"name": "Hardware",  "value": f"```\n{_hw_display(hw)}\n```", "inline": False},
            {"name": "Install",   "value": f"`{identity['install_id']}`",  "inline": False},
            {"name": "Traceback", "value": f"```\n{tb[-1800:]}\n```",      "inline": False},
        ],
        "footer": {"text": BRAND},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }]}


def build_feedback_embed(stars: int, comment: str, identity: dict) -> dict:
    star_str = "★" * stars + "☆" * (5 - stars)
    color_map = {1: 0xff1744, 2: 0xff9100, 3: 0xeb459e, 4: 0x5865f2, 5: 0x00e676}
    color = color_map.get(stars, 0xeb459e)
    return {"username": "AutoClicker", "embeds": [{
        "color": color,
        "title": f"Feedback — {star_str}  ({stars}/5)",
        "fields": [
            {"name": "User",     "value": f"`{_USERNAME}`",           "inline": True},
            {"name": "Machine",  "value": f"`{platform.node()}`",     "inline": True},
            {"name": "Version",  "value": f"`v{VERSION}`",            "inline": True},
            {"name": "Comment",  "value": (comment[:900] if comment.strip() else "_no comment_"),
                                 "inline": False},
            {"name": "Install",  "value": f"`{identity['install_id']}`", "inline": False},
        ],
        "footer": {"text": f"{BRAND} - feedback"},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }]}


# ─── Terminal-window resize (Mac: AppleScript for Terminal.app / iTerm) ──────

_ORIG_TERM_SIZE: Optional[tuple[int, int]] = None  # remembered cols,rows


def _get_term_size() -> tuple[int, int]:
    try:
        size = os.get_terminal_size()
        return size.columns, size.lines
    except Exception:
        return 120, 40


def _ansi_resize(rows: int, cols: int) -> None:
    """The XTerm window-resize sequence. Honoured by Windows Terminal, iTerm,
    GNOME Terminal, konsole and most others; harmless where it isn't."""
    try:
        sys.stdout.write(f"\x1b[8;{rows};{cols}t")
        sys.stdout.flush()
    except Exception:
        pass


def _term_set_size_mac(rows: int, cols: int) -> None:
    """Terminal.app and iTerm only answer to AppleScript."""
    for s in (
        f'tell application "Terminal" to set number of columns of front window to {cols}',
        f'tell application "Terminal" to set number of rows of front window to {rows}',
        # iTerm: bounds in pixels (rough char metrics)
        f'tell application "iTerm" to tell current window to set bounds to '
        f'{{100, 100, {100 + cols * 8}, {100 + rows * 17}}}',
    ):
        try:
            subprocess.run(["osascript", "-e", s],
                           capture_output=True, timeout=1)
        except Exception:
            pass
    _ansi_resize(rows, cols)


def _term_set_size_win(rows: int, cols: int) -> None:
    """Windows Terminal / ConPTY take the ANSI sequence; the old conhost.exe
    does not, so `mode con` is tried as well."""
    _ansi_resize(rows, cols)
    try:
        subprocess.run(["cmd", "/c", f"mode con: cols={cols} lines={rows}"],
                       capture_output=True, timeout=2,
                       creationflags=0x08000000)   # CREATE_NO_WINDOW
    except Exception:
        pass


def _term_set_size(rows: int, cols: int) -> None:
    """Apply a terminal size using whatever this platform understands."""
    setters = {}
    setters["mac"] = _term_set_size_mac
    setters["win"] = _term_set_size_win
    fn = setters.get("mac" if IS_MAC else "win" if IS_WIN else "")
    (fn or _ansi_resize)(rows, cols)


def term_resize(rows: int, cols: int) -> None:
    """Shrink the host terminal window for mini mode. Best-effort everywhere:
    AppleScript on macOS, ANSI + `mode con` on Windows, ANSI on Linux."""
    global _ORIG_TERM_SIZE
    if _ORIG_TERM_SIZE is None:
        _ORIG_TERM_SIZE = _get_term_size()
    _term_set_size(rows, cols)


def term_maximize() -> None:
    """Restore the terminal to the size it had before mini mode."""
    cols, rows = (_ORIG_TERM_SIZE or (140, 50))
    _term_set_size(rows, cols)


# ─── Widgets ──────────────────────────────────────────────────────────────────

class CopyPill(Static):
    DEFAULT_CSS = """
    CopyPill {
        color: #6060a0;
        padding: 0 2;
        height: 1;
        width: auto;
    }
    CopyPill:hover { color: #00c8f0; text-style: bold; }
    """

    def __init__(self, label: str, value: str, **kw):
        super().__init__(label, **kw)
        self._label = label
        self._value = value

    def on_click(self, event: events.Click) -> None:
        ok, method = copy_to_system_clipboard(self._value)
        if not ok:
            try:
                self.app.copy_to_clipboard(self._value)
                ok = True
                method = "osc52"
            except Exception:
                pass
        self.update(f"[#00e676 bold]✓ {self._label} ({method})[/]" if ok
                    else f"[#ff1744 bold]× {self._label} (no clipboard)[/]")
        self.set_timer(1.6, lambda: self.update(self._label))


class SegOption(Static):
    class Selected(Message):
        def __init__(self, group: str, option_id: str) -> None:
            self.group = group
            self.option_id = option_id
            super().__init__()

    DEFAULT_CSS = """
    SegOption {
        padding: 0 2;
        height: 1;
        width: auto;
        color: #5a5a7a;
    }
    SegOption:hover { color: #eeeeff; }
    SegOption.-selected { color: #00c8f0; text-style: bold; }
    """

    def __init__(self, group: str, option_id: str, label: str, **kw):
        super().__init__("  " + label, id=f"seg-{group}-{option_id}", **kw)
        self.group = group
        self.option_id = option_id
        self.label_text = label

    def on_click(self, event: events.Click) -> None:
        self.post_message(SegOption.Selected(self.group, self.option_id))

    def set_selected(self, is_sel: bool) -> None:
        self.update(("▸ " if is_sel else "  ") + self.label_text)
        if is_sel:
            self.add_class("-selected")
        else:
            self.remove_class("-selected")


class Segmented(Horizontal):
    DEFAULT_CSS = """
    Segmented { height: 1; width: auto; padding: 0 0 0 2; }
    """

    def __init__(self, group: str, options: list, selected: str, **kw):
        super().__init__(**kw)
        self.group = group
        self.options = options
        self.selected = selected

    def compose(self) -> ComposeResult:
        for opt_id, label in self.options:
            opt = SegOption(self.group, opt_id, label)
            if opt_id == self.selected:
                opt.set_selected(True)
            yield opt

    def on_seg_option_selected(self, message: SegOption.Selected) -> None:
        if message.group != self.group:
            return
        self.selected = message.option_id
        for opt in self.query(SegOption):
            opt.set_selected(opt.option_id == self.selected)


# ─── Info modal screen ────────────────────────────────────────────────────────

INFO_TEXT = """\
[#00c8f0 bold]CLICKS PER SECOND[/]
  how fast to click — 1 (very slow) up to 200 (extreme).
  start at 20 if you're not sure.

[#00c8f0 bold]CLICK LIMIT[/]
  auto-stop after N clicks. switch ON, type a number.
  switch OFF = click forever until you press stop.

[#00c8f0 bold]CLICK BUTTON[/]
  LEFT    normal click (most uses).
  RIGHT   right-click — opens context menus.
  MIDDLE  middle / scroll-wheel click.
  DOUBLE  two quick left-clicks per cycle (like opening a file).

[#00c8f0 bold]CLICK PATTERN[/]
  CONSTANT  every click perfectly evenly spaced. predictable.
  JITTER    ±10% random timing variation. looks human, harder for anti-bot.
  BURST     5 rapid clicks, 1-second pause, repeat. mimics human bursts.

[#00c8f0 bold]POSITION[/]
  FOLLOW CURSOR  clicks land wherever the mouse is right now.
  LOCK POSITION  clicks always land where the mouse was when you pressed start.

[#00c8f0 bold]FAILSAFE[/]
  flick mouse to TOP-LEFT corner of screen = instant stop.

[#00c8f0 bold]ROBLOX ANTI-AFK[/]
  click the anti-afk button. holds W for 3s, then S for 3s, repeating.
  stops Roblox from kicking you for being idle. clicking continues normally.

[#00c8f0 bold]EMULATION (macro recorder)[/]
  the EMULATION tab records your real mouse + keyboard, then replays it.
  press RECORD, do your actions, press STOP (or Esc), name it, save.
  play any saved recording back at 0.5x / 1x / 2x speed, once or looping.
  a 3-2-1 countdown shows before playback so you can get ready.
  note: for games that lock/hide the cursor, plain recording may not capture
  camera motion — that needs a raw input backend.

[#00c8f0 bold]SHORTCUTS[/]
  F8                       start / stop clicking
  F9                       pause / resume
  Option / Alt (tap)       start / stop clicking
  both Option / Alt keys   pause / resume
    (Mac: tap both Options · Win/Linux: hold left Alt + right Alt)
  Esc × 3                  quit (shows 2s countdown bar, then exits)

  on Windows, prefer F8 and F9. tapping Alt on its own tells Windows to
  open the menu bar of whatever window has focus, so the Alt hotkeys will
  also pop menus in the app you are clicking on. F8 and F9 do not.
  the right Alt is AltGr on UK, German, French, Nordic and Polish
  layouts — it is handled, and still counts as the right Alt.
"""


class InfoScreen(Screen):
    """Modal info overlay — auto-dismisses after 10s, or click/key to close."""

    CSS = """
    InfoScreen {
        background: #02020a;
        align: center middle;
    }
    #info-card {
        background: #0c0c18;
        width: 80%;
        height: auto;
        max-height: 90%;
        padding: 1 3;
    }
    #info-title {
        color: #00c8f0;
        text-style: bold;
        height: 1;
        text-align: center;
        margin: 0 0 1 0;
    }
    #info-countdown {
        color: #5a5a7a;
        text-align: center;
        height: 1;
        margin: 1 0 0 0;
    }
    #info-body {
        color: #aaaacc;
        height: auto;
    }
    """

    BINDINGS = [Binding("escape", "close", show=False)]

    def __init__(self):
        super().__init__()
        self._remaining = 10

    def compose(self) -> ComposeResult:
        with Container(id="info-card"):
            yield Static("INFO — every option explained", id="info-title")
            yield Static(INFO_TEXT, id="info-body")
            yield Static(f"closes in 10s · click or press any key to close", id="info-countdown")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        self._remaining -= 1
        try:
            self.query_one("#info-countdown", Static).update(
                f"[#5a5a7a]closes in {max(0,self._remaining)}s · click or press any key to close[/]"
            )
        except Exception:
            pass
        if self._remaining <= 0:
            self.action_close()

    def on_click(self, event: events.Click) -> None:
        self.action_close()

    def on_key(self, event: events.Key) -> None:
        self.action_close()

    def action_close(self) -> None:
        try:
            self.app.pop_screen()
        except Exception:
            pass


# ─── History screen ───────────────────────────────────────────────────────────

class HistoryScreen(Screen):
    """Scrollable list of every past session."""

    CSS = """
    HistoryScreen {
        background: #07070d;
    }
    #hist-header {
        color: #00c8f0;
        text-style: bold;
        height: 1;
        text-align: center;
        margin: 1 0;
    }
    #hist-sub {
        color: #5a5a7a;
        height: 1;
        text-align: center;
        margin: 0 0 1 0;
    }
    #hist-scroll {
        height: 1fr;
        padding: 0 2;
    }
    .hist-row {
        height: 1;
        color: #aaaacc;
    }
    .hist-row:hover { color: #eeeeff; background: #161625; }
    #hist-footer {
        color: #5a5a7a;
        height: 1;
        text-align: center;
        margin: 1 0;
    }
    """

    BINDINGS = [Binding("escape", "close", show=False),
                Binding("q",      "close", show=False)]

    def compose(self) -> ComposeResult:
        yield Static("SESSION HISTORY", id="hist-header")
        sessions = load_all_sessions()
        yield Static(f"{len(sessions)} session(s) on file", id="hist-sub")
        with VerticalScroll(id="hist-scroll"):
            if not sessions:
                yield Static("[#5a5a7a]no sessions yet — start clicking to build history[/]",
                             classes="hist-row")
            else:
                yield Static("[#5a5a7a]   #     started       clicks       duration[/]",
                             classes="hist-row")
                for i, s in enumerate(sessions, 1):
                    started  = s.get("started", "?")
                    clicks   = s.get("clicks", 0)
                    duration = s.get("duration", 0.0)
                    yield Static(
                        f"  {i:>3}    {started:>8}    {clicks:>7,}     {duration:>7.1f}s",
                        classes="hist-row")
        yield Static("press Esc or q to close", id="hist-footer")

    def action_close(self) -> None:
        try:
            self.app.pop_screen()
        except Exception:
            pass


# ─── Feedback screen ──────────────────────────────────────────────────────────

class Star(Static):
    """One clickable star — fires StarClicked(value=1..5)."""

    class Clicked(Message):
        def __init__(self, value: int) -> None:
            self.value = value
            super().__init__()

    DEFAULT_CSS = """
    Star {
        width: 4;
        height: 1;
        content-align: center middle;
        color: #5a5a7a;
        text-style: bold;
    }
    Star:hover { color: #ff9100; }
    Star.-lit  { color: #ff9100; text-style: bold; }
    """

    def __init__(self, value: int, **kw):
        super().__init__("☆", id=f"star-{value}", **kw)
        self.value = value

    def on_click(self, event: events.Click) -> None:
        self.post_message(Star.Clicked(self.value))

    def light(self, lit: bool) -> None:
        if lit:
            self.update("★")
            self.add_class("-lit")
        else:
            self.update("☆")
            self.remove_class("-lit")


class RecLabel(Static):
    """A clickable text label inside a RecordingRow (play or delete)."""

    class Clicked(Message):
        def __init__(self, action: str, stem: str) -> None:
            self.action = action
            self.stem = stem
            super().__init__()

    def __init__(self, text: str, action: str, stem: str, **kw):
        super().__init__(text, **kw)
        self._action = action
        self._stem = stem

    def on_click(self, event: events.Click) -> None:
        self.post_message(RecLabel.Clicked(self._action, self._stem))


class RecordingRow(Horizontal):
    """One saved recording: name + play + delete (clickable text)."""

    DEFAULT_CSS = """
    RecordingRow {
        height: 1;
        margin: 0 0 0 2;
    }
    RecordingRow > .rec-name {
        width: 28;
        color: #aaaacc;
        height: 1;
        content-align: left middle;
    }
    RecordingRow > .rec-play {
        width: 10;
        height: 1;
        color: #00e676;
        text-style: bold;
        content-align: left middle;
    }
    RecordingRow > .rec-del {
        width: 12;
        height: 1;
        color: #ff1744;
        text-style: bold;
        content-align: left middle;
    }
    RecordingRow > .rec-play:hover,
    RecordingRow > .rec-del:hover { color: #eeeeff; }
    """

    def __init__(self, stem: str, **kw):
        super().__init__(**kw)
        self.stem = stem

    def compose(self) -> ComposeResult:
        yield Static(self.stem, classes="rec-name")
        yield RecLabel("▶ play",   "play",   self.stem, classes="rec-play")
        yield RecLabel("✕ delete", "delete", self.stem, classes="rec-del")

    @on(RecLabel.Clicked)
    def _on_rec_label(self, msg: RecLabel.Clicked) -> None:
        if msg.action == "play":
            self.app.emu_play(msg.stem)
        elif msg.action == "delete":
            self.app.emu_delete(msg.stem)


class FeedbackScreen(Screen):
    """Rate the app + leave a comment. Sent to webhook on submit."""

    CSS = """
    FeedbackScreen { background: #02020a; align: center middle; }
    #fb-card {
        background: #0c0c18;
        width: 70;
        height: 22;
        padding: 1 3;
    }
    #fb-title {
        color: #00c8f0;
        text-style: bold;
        height: 1;
        text-align: center;
        margin: 0 0 1 0;
    }
    #fb-sub {
        color: #5a5a7a;
        height: 1;
        text-align: center;
        margin: 0 0 1 0;
    }
    #fb-stars {
        height: 1;
        align: center middle;
        margin: 0 0 1 0;
    }
    #fb-rating-label {
        height: 1;
        color: #ff9100;
        text-align: center;
        margin: 0 0 1 0;
    }
    #fb-comment-label {
        color: #5a5a7a;
        height: 1;
    }
    #fb-comment {
        background: #161625;
        color: #eeeeff;
        border: none;
        height: 3;
        width: 100%;
        margin: 0 0 1 0;
        padding: 0 1;
    }
    #fb-buttons {
        height: 1;
        align: center middle;
    }
    #fb-buttons Button {
        background: transparent;
        border: none;
        margin: 0 2;
        height: 1;
        min-width: 10;
        text-style: bold;
    }
    #fb-submit { color: #00e676; }
    #fb-cancel { color: #5a5a7a; }
    #fb-submit:hover, #fb-cancel:hover { color: #eeeeff; }
    #fb-status {
        height: 1;
        text-align: center;
        color: #5a5a7a;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [Binding("escape", "close", show=False)]

    def __init__(self):
        super().__init__()
        self.stars = 0
        self._sent = False

    def compose(self) -> ComposeResult:
        with Container(id="fb-card"):
            yield Static("FEEDBACK", id="fb-title")
            yield Static(f"how's {BRAND.lower()} v{VERSION} working for you?", id="fb-sub")
            with Horizontal(id="fb-stars"):
                for i in range(1, 6):
                    yield Star(i)
            yield Static("tap a star to rate", id="fb-rating-label")
            yield Static("comment (optional):", id="fb-comment-label")
            yield Input(placeholder="anything you want to say...", id="fb-comment")
            with Horizontal(id="fb-buttons"):
                yield Button("submit", id="fb-submit")
                yield Button("cancel", id="fb-cancel")
            yield Static("", id="fb-status")

    @on(Star.Clicked)
    def _on_star(self, message: Star.Clicked) -> None:
        self.stars = message.value
        for star in self.query(Star):
            star.light(star.value <= self.stars)
        labels = {1: "needs work", 2: "meh", 3: "fine", 4: "good", 5: "great!"}
        self.query_one("#fb-rating-label", Static).update(
            f"[#ff9100 bold]{self.stars}/5  ·  {labels[self.stars]}[/]"
        )

    @on(Button.Pressed, "#fb-cancel")
    def _cancel(self, _) -> None:
        self.action_close()

    @on(Button.Pressed, "#fb-submit")
    def _submit(self, _) -> None:
        if self._sent:
            return
        if self.stars == 0:
            self.query_one("#fb-status", Static).update(
                "[#ff1744]pick a star rating first[/]")
            return
        comment = self.query_one("#fb-comment", Input).value
        self.query_one("#fb-status", Static).update("[#5a5a7a]sending...[/]")
        self._sent = True
        # Fire send in a thread — don't block UI
        threading.Thread(
            target=self._send,
            args=(self.stars, comment),
            daemon=True).start()

    def _send(self, stars: int, comment: str) -> None:
        identity = self.app.identity
        ok = send_payload_sync(build_feedback_embed(stars, comment, identity))
        try:
            self.app.call_from_thread(self._after_send, ok)
        except Exception:
            pass

    def _after_send(self, ok: bool) -> None:
        try:
            status = self.query_one("#fb-status", Static)
            if ok:
                status.update("[#00e676]✓ thanks — feedback sent[/]")
                # Auto-close after 1.5s
                self.set_timer(1.5, self.action_close)
            else:
                err = (LAST_WEBHOOK_ERROR or "unknown")[:40]
                status.update(f"[#ff1744]× couldn't send: {err} — queued for retry[/]")
                self.set_timer(2.5, self.action_close)
        except Exception:
            pass

    def action_close(self) -> None:
        try:
            self.app.pop_screen()
        except Exception:
            pass


# ─── Mini screen ──────────────────────────────────────────────────────────────

class MiniScreen(Screen):
    CSS = """
    MiniScreen {
        background: #03030a;
        align: center middle;
    }
    #mini-card {
        background: #0c0c18;
        width: 52;
        height: 22;
        padding: 1 2;
    }
    #mini-brand { color: #2e2e48; height: 1; }
    #mini-status { height: 1; text-style: bold; margin: 1 0; }
    #mini-cps-row { height: 3; align: center middle; margin: 0 0 1 0; }
    #mini-cps-dec, #mini-cps-inc {
        width: 5; height: 3;
        background: transparent;
        color: #6060a0;
        text-style: bold;
        border: none;
        min-width: 5;
    }
    #mini-cps-dec:hover, #mini-cps-inc:hover { color: #00c8f0; }
    #mini-cps-input {
        width: 8; height: 3;
        background: transparent;
        color: #00c8f0;
        border: none;
        content-align: center middle;
    }
    #mini-cps-input:focus { background: #161625; }
    #mini-eff   { color: #aaaacc; height: 1; margin: 0 0 1 0; }
    #mini-antiafk { color: #5a5a7a; height: 1; }
    #mini-esc-banner {
        color: #ff1744;
        text-style: bold;
        height: 1;
        text-align: center;
    }
    #mini-buttons-row {
        height: 1;
        align: center middle;
        margin: 1 0 0 0;
    }
    #mini-buttons-row Button {
        background: transparent;
        border: none;
        height: 1;
        min-width: 12;
        margin: 0 1;
        text-style: bold;
    }
    #mini-restore { color: #00c8f0; }
    #mini-restore:hover { color: #eeeeff; }
    #mini-toggle  { color: #00e676; }
    #mini-toggle:hover  { color: #eeeeff; }
    #mini-hint { color: #3a3a5a; height: 1; margin: 1 0 0 0; text-align: center; }
    """

    BINDINGS = []   # all global keys handled by app-level pynput hotkeys

    def compose(self) -> ComposeResult:
        with Container(id="mini-card"):
            yield Static(BRAND, id="mini-brand")
            yield Static("● IDLE", id="mini-status")
            yield Static("target cps", id="mini-cps-label")
            with Horizontal(id="mini-cps-row"):
                yield Button("−", id="mini-cps-dec")
                yield Input(id="mini-cps-input", restrict=r"[0-9]*", value="20")
                yield Button("+", id="mini-cps-inc")
            yield Static("eff cps  0.0", id="mini-eff")
            yield Static("anti-afk: off", id="mini-antiafk")
            yield Static("", id="mini-esc-banner")
            with Horizontal(id="mini-buttons-row"):
                yield Button("◀ RESTORE", id="mini-restore")
                yield Button("start/stop", id="mini-toggle")
            yield Static("alt = on/off · both alts = pause · top-left = stop",
                         id="mini-hint")

    def on_mount(self) -> None:
        try:
            val = self.app.query_one("#cps-input", Input).value
            self.query_one("#mini-cps-input", Input).value = val
        except Exception:
            pass
        self.set_interval(0.15, self._tick)

    def _tick(self) -> None:
        app = self.app
        status = self.query_one("#mini-status", Static)
        if app.active.is_set() and not app.paused.is_set():
            status.update("[#00e676 bold]● clicking[/]")
        elif app.paused.is_set():
            status.update("[#ff9100 bold]● paused[/]")
        else:
            status.update("[#5a5a7a bold]● idle[/]")
        with app.stats_lock:
            now_ns = time.monotonic_ns()
            cutoff = now_ns - int(2.0 * 1e9)
            recent = sum(1 for t in app.cps_window if t > cutoff)
        eff = recent / 2.0
        self.query_one("#mini-eff", Static).update(
            f"eff cps  [#00c8f0 bold]{eff:.1f}[/]")
        self.query_one("#mini-antiafk", Static).update(
            "anti-afk: [#00e676]ON[/]" if app.antiafk_active.is_set()
            else "anti-afk: [#5a5a7a]off[/]")
        # Esc countdown banner
        banner = app._esc_banner_text()
        self.query_one("#mini-esc-banner", Static).update(banner)

    @on(Button.Pressed, "#mini-cps-dec")
    def _dec(self, _) -> None:
        inp = self.query_one("#mini-cps-input", Input)
        try:
            v = max(1, int(inp.value or "20") - 1)
            inp.value = str(v)
        except Exception:
            pass

    @on(Button.Pressed, "#mini-cps-inc")
    def _inc(self, _) -> None:
        inp = self.query_one("#mini-cps-input", Input)
        try:
            v = min(200, int(inp.value or "20") + 1)
            inp.value = str(v)
        except Exception:
            pass

    @on(Button.Pressed, "#mini-restore")
    def _restore(self, _) -> None:
        """Restore from mini mode back to main."""
        self.app.action_mini()   # toggles → pops back to main

    @on(Button.Pressed, "#mini-toggle")
    def _toggle(self, _) -> None:
        """Inline start/stop button in mini."""
        self.app._toggle_start_stop()

    @on(Input.Changed, "#mini-cps-input")
    def _cps_changed(self, event: Input.Changed) -> None:
        try:
            self.app.query_one("#cps-input", Input).value = event.value
        except Exception:
            pass


# ─── Main app ─────────────────────────────────────────────────────────────────

class AutoClickerApp(App):

    CSS = """
    Screen { background: #07070d; color: #eeeeff; }

    /* ── esc countdown bar (pinned top, shrinks left→0 as time runs out) ── */
    #esc-bar-frame {
        height: 0;
        width: 100%;
        background: #0d0005;
        padding: 0;
    }
    #esc-bar-frame.-active {
        height: 4;
        background: #0d0005;
    }
    #esc-bar-fill {
        height: 4;
        width: 100%;
        background: #c0001a;
        color: #ffffff;
        text-style: bold;
        content-align: center middle;
        padding: 0 2;
    }
    /* Arm states — intensifies as time runs out */
    #esc-bar-fill.-warn    { background: #d81428; }
    #esc-bar-fill.-final   { background: #ff1744; }
    /* Quit countdown — orange */
    #esc-bar-fill.-quitting {
        background: #cc5500;
        color: #ffffff;
    }

    /* ── top brand ── */
    #top-row {
        height: 2;
        padding: 1 2 0 2;
        layout: horizontal;
    }
    #top-brand   { width: 1fr; color: #5a5a7a; height: 1; }
    #discord-status { width: auto; color: #3a3a5a; height: 1; }

    /* ── ASCII art ── */
    #art-block {
        height: 5;
        padding: 0 2;
        margin: 0 0 1 0;
    }
    .art-line { height: 1; color: #ff1744; text-style: bold; }
    .drip-1   { height: 1; color: #aa1010; }
    .drip-2   { height: 1; color: #661010; }
    .drip-3   { height: 1; color: #331010; }

    /* ── tabs ── */
    TabbedContent { height: 1fr; }
    Tabs { background: transparent; }
    Tab {
        color: #5a5a7a;
        padding: 0 2;
        background: transparent;
    }
    Tab.-active {
        color: #00c8f0;
        text-style: bold;
        background: transparent;
    }
    Tab:hover { color: #aaaacc; }
    TabPane { padding: 1 0; }

    /* ── form sections ── */
    .section { padding: 0 2; height: auto; margin: 0 0 1 0; }
    .section-title-row { height: 1; layout: horizontal; }
    .section-title { width: 1fr; color: #00c8f0; text-style: bold; height: 1; }
    .section-hint  { color: #5a5a7a; height: 1; padding: 0 0 0 2; }

    /* ── command-list pages ── */
    .cmd-page { padding: 0 2; height: auto; }
    .cmd-line { color: #aaaacc; height: 1; padding: 0 0 0 2; }
    .cmd-blank { height: 1; }
    .info-block { color: #aaaacc; height: auto; }

    /* ── inputs ── */
    Input {
        background: transparent;
        color: #00c8f0;
        border: none;
        height: 1;
        padding: 0 1;
        margin: 0 0 0 2;
        width: 12;
    }
    Input:focus { background: #161625; }
    Input.-disabled { color: #2e2e48; }
    #cps-row, #limit-row, #emoji-row { height: 1; }
    #lock-row {
        height: 1;
        margin: 1 0 0 2;
    }
    #btn-capture-lock {
        background: transparent;
        border: none;
        color: #00c8f0;
        height: 1;
        min-width: 24;
        text-style: bold;
        margin: 0 2 0 0;
    }
    #btn-capture-lock:hover { color: #eeeeff; }
    #lock-display {
        color: #5a5a7a;
        height: 1;
        content-align: left middle;
    }
    #cps-desc, #limit-desc, #emoji-suggestions {
        color: #5a5a7a;
        height: 1;
        width: 1fr;
        padding: 0 0 0 2;
    }
    #emoji-input { width: 8; }

    Switch { background: transparent; border: none; }

    .divider { background: #161625; height: 1; margin: 1 2; }

    /* ── status / progress (main tab) ── */
    #status-row { height: 1; padding: 0 2; }
    #status-label { width: 1fr; text-style: bold; height: 1; }
    #click-count  { width: 22; color: #aaaacc; content-align: right middle; height: 1; }
    ProgressBar { margin: 0 2; height: 1; width: 1fr; }

    /* ── action groups ── */
    #actions {
        height: 3;
        align: center middle;
        padding: 1 0 0 0;
        layout: horizontal;
    }
    .action-group { width: auto; height: 1; padding: 0 3; layout: horizontal; }
    .action-group-divider { width: 1; height: 1; color: #161625; }
    #actions Button {
        margin: 0 1;
        height: 1;
        min-width: 9;
        background: transparent;
        border: none;
        color: #6060a0;
        text-style: bold;
    }
    #actions Button:hover { color: #eeeeff; }
    #btn-start    { color: #00e676; }
    #btn-pause    { color: #ff9100; }
    #btn-stop     { color: #ff1744; }
    #btn-reset    { color: #5a5a7a; }
    #btn-mini     { color: #00c8f0; }
    #btn-antiafk  { color: #5a5a7a; }
    #btn-antiafk.-on { color: #00e676; }
    Button.-disabled, Button:disabled { color: #2e2e48; }

    #stats-row { height: 1; padding: 0 2; color: #5a5a7a; }
    #stats-row > Static { width: 1fr; }
    #stats-eff     { content-align: left middle; }
    #stats-sess    { content-align: center middle; }
    #stats-antiafk { content-align: center middle; }
    #stats-time    { content-align: right middle; }

    /* ── feedback tab ── */
    #fb-stars-row {
        height: 1;
        padding: 0 0 0 2;
        margin: 1 0;
    }
    #fb-comment {
        background: #161625;
        color: #eeeeff;
        width: 100%;
        height: 3;
        margin: 1 0;
    }
    #fb-buttons-row { height: 1; margin: 1 0; }
    #fb-submit {
        background: transparent;
        border: none;
        color: #00e676;
        text-style: bold;
        height: 1;
        margin: 0 0 0 2;
    }
    #fb-submit:hover { color: #eeeeff; }
    #fb-status { color: #5a5a7a; }
    #fb-rating-label { color: #ff9100; }

    /* ── history tab ── */
    #hist-loading { color: #5a5a7a; padding: 0 2; height: 1; }
    #hist-container {
        height: 1fr;
        padding: 0 2;
    }

    /* ── emulation tab ── */
    #emu-rec-row { height: 1; margin: 1 0 0 2; }
    #btn-emu-record {
        background: transparent;
        border: none;
        color: #ff1744;
        height: 1;
        min-width: 12;
        text-style: bold;
        margin: 0 2 0 0;
    }
    #btn-emu-record:hover { color: #eeeeff; }
    #emu-rec-status { color: #5a5a7a; height: 1; content-align: left middle; }
    #emu-save-row { height: 1; margin: 1 0 0 2; }
    #emu-name-input {
        background: #161625;
        color: #eeeeff;
        border: none;
        height: 1;
        width: 30;
        margin: 0 2 0 0;
        padding: 0 1;
    }
    #btn-emu-save {
        background: transparent;
        border: none;
        color: #00c8f0;
        height: 1;
        min-width: 6;
        text-style: bold;
    }
    #btn-emu-save:hover { color: #eeeeff; }
    #emu-list {
        height: 1fr;
        min-height: 6;
        padding: 0 0 0 0;
    }
    #emu-loading { color: #5a5a7a; height: 1; }

    /* ── bottom strip ── */
    .warn-strip {
        color: #5a1010;
        text-align: center;
        height: 1;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "force_quit", show=False, priority=True),
        # Letter shortcuts ARE handled via pynput GlobalHotKeys so they work
        # system-wide (e.g. when Roblox is focused). Local Textual bindings
        # only fire when the terminal has focus.
    ]

    def __init__(self, **kw):
        super().__init__(**kw)
        self.identity   = load_identity()
        self.saved_cfg  = load_config_dict()

        self.mouse_ctrl  = pmouse.Controller()    if PYNPUT_OK else None
        self.kb_ctrl     = pkeyboard.Controller() if PYNPUT_OK else None
        self.hotkeys: Optional["pkeyboard.GlobalHotKeys"] = None
        self.esc_listener: Optional["pkeyboard.Listener"] = None

        self.active           = threading.Event()
        self.paused           = threading.Event()
        self.stop_flag        = threading.Event()
        self.antiafk_active   = threading.Event()
        self.stats_lock       = threading.Lock()

        self.total_clicks  = 0
        self.toggles       = 0
        self.clicking_time = 0.0
        self.sub_start: Optional[float] = None
        self.sub_clicks    = 0
        self.sessions: list = []
        self.app_start     = time.time()
        self.lock_pos: Optional[tuple[int, int]] = None
        self.cps_window: collections.deque = collections.deque(maxlen=4000)
        self.screen_w, self.screen_h = get_screen_resolution()

        # Esc countdown state
        self.esc_count    = 0
        self.esc_first_at = 0.0
        self._esc_overlay_proc: Optional[subprocess.Popen] = None
        # Quit countdown state
        self._quitting       = False
        self._quit_start     = 0.0
        # Lock-point capture state
        self._capturing       = False        # True while waiting for 5 clicks
        self._capture_count   = 0
        self._capture_positions: list = []
        self._capture_listener = None        # pynput mouse Listener
        # Opt/Alt key tracking
        # macOS:   single side → Key.alt (vk=58); both sides → raw vk=160
        # Win/Lin: left → Key.alt_l, right → Key.alt_r; both tracked separately
        self._opt_down        = False   # True while a single Alt is held
        self._opt_press_time  = 0.0
        self._opt_contaminated = False  # True if another key pressed while Opt held
        self._alt_l_down      = False   # Win/Linux: left Alt currently down
        self._alt_r_down      = False   # Win/Linux: right Alt currently down
        self._both_alt_fired  = False   # Win/Linux: both-alt pause already fired

        # ── EMULATION (macro recorder/player) state ─────────────────────────
        self._rec_active      = False          # currently recording
        self._rec_events: list = []            # captured events
        self._rec_start       = 0.0            # monotonic start time
        self._rec_mouse_listener = None        # pynput mouse Listener
        self._rec_kb_listener    = None        # pynput keyboard Listener
        self._rec_ui_clicks: list = []         # clicks that hit OUR window
        self._rec_counts      = {"move": 0, "click": 0, "key": 0}
        self._play_active     = False          # currently playing back
        self._play_thread     = None

        self._shutdown_done = False
        self._shutdown_lock = threading.Lock()

    # ── compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # Esc countdown bar — pinned to top, hidden by default
        with Container(id="esc-bar-frame"):
            yield Static("", id="esc-bar-fill")

        # Top brand + discord
        with Horizontal(id="top-row"):
            yield Static(BRAND.lower(), id="top-brand")
            yield Static("● discord connecting...", id="discord-status")

        # ASCII art
        with Container(id="art-block"):
            for line in AUTO_CLICKER_ART:
                yield Static(line, classes="art-line")
            yield Static(DRIPS[0], classes="drip-1")
            yield Static(DRIPS[1], classes="drip-2")
            yield Static(DRIPS[2], classes="drip-3")

        # Tabs — MAIN · INFO · HISTORY · FEEDBACK · SETTINGS · COMMANDS
        with TabbedContent(id="main-tabs"):
            # ── MAIN TAB ────────────────────────────────────────
            with TabPane("MAIN", id="t-main"):
                with VerticalScroll():
                    with Container(classes="section"):
                        yield Static("CLICKS PER SECOND", classes="section-title")
                        yield Static("how fast to click — start at 20, max 200",
                                     classes="section-hint")
                        with Horizontal(id="cps-row"):
                            yield Input(value=str(self.saved_cfg.get("cps", 20)),
                                        id="cps-input", placeholder="1-200",
                                        restrict=r"[0-9]*")
                            yield Static(self._describe_cps(self.saved_cfg.get("cps", 20)),
                                         id="cps-desc")

                    with Container(classes="section"):
                        with Horizontal(classes="section-title-row"):
                            yield Static("CLICK LIMIT", classes="section-title")
                            yield Switch(value=bool(self.saved_cfg.get("limit")),
                                         id="limit-switch")
                        yield Static("auto-stop after N clicks — off to run forever",
                                     classes="section-hint")
                        with Horizontal(id="limit-row"):
                            yield Input(value=str(self.saved_cfg.get("limit") or 1000),
                                        id="limit-input", placeholder="N",
                                        restrict=r"[0-9]*",
                                        disabled=not bool(self.saved_cfg.get("limit")))
                            yield Static("", id="limit-desc")

                    with Container(classes="section"):
                        yield Static("CLICK BUTTON", classes="section-title")
                        yield Segmented("button", [
                            ("left", "LEFT"), ("right", "RIGHT"),
                            ("middle", "MIDDLE"), ("double", "DOUBLE"),
                        ], self.saved_cfg.get("button", "left"))

                    with Container(classes="section"):
                        yield Static("CLICK PATTERN", classes="section-title")
                        yield Segmented("pattern", [
                            ("constant", "CONSTANT"),
                            ("jitter",   "JITTER"),
                            ("burst",    "BURST"),
                        ], self.saved_cfg.get("pattern", "constant"))

                    with Container(classes="section"):
                        yield Static("POSITION", classes="section-title")
                        yield Static(
                            "follow = wherever your mouse is  ·  lock = fixed point",
                            classes="section-hint")
                        yield Segmented("position", [
                            ("follow", "FOLLOW CURSOR"),
                            ("lock",   "LOCK POSITION"),
                        ], self.saved_cfg.get("position_mode", "follow"))
                        with Horizontal(id="lock-row"):
                            yield Button("◎ CAPTURE LOCK POINT", id="btn-capture-lock")
                            yield Static("not set", id="lock-display")

                    with Container(classes="section"):
                        with Horizontal(classes="section-title-row"):
                            yield Static("FAILSAFE", classes="section-title")
                            yield Switch(
                                value=bool(self.saved_cfg.get("failsafe", True)),
                                id="failsafe-switch")
                        yield Static("top-left corner of screen = emergency stop",
                                     classes="section-hint")

                    yield Static("", classes="divider")

                    with Horizontal(id="status-row"):
                        yield Static("[#5a5a7a bold]● idle[/]", id="status-label")
                        yield Static("0 clicks", id="click-count")

                    yield ProgressBar(total=100, show_percentage=False,
                                      show_eta=False, id="limit-progress")

                    with Horizontal(id="actions"):
                        with Horizontal(classes="action-group"):
                            yield Button("start",   id="btn-start")
                            yield Button("pause",   id="btn-pause", disabled=True)
                            yield Button("stop",    id="btn-stop",  disabled=True)
                        yield Static("│", classes="action-group-divider")
                        with Horizontal(classes="action-group"):
                            yield Button("reset",    id="btn-reset")
                            yield Button("anti-afk", id="btn-antiafk")
                        yield Static("│", classes="action-group-divider")
                        with Horizontal(classes="action-group"):
                            yield Button("mini", id="btn-mini")

                    with Horizontal(id="stats-row"):
                        yield Static("eff cps 0.0",  id="stats-eff")
                        yield Static("sessions 0",   id="stats-sess")
                        yield Static("anti-afk off", id="stats-antiafk")
                        yield Static("00:00:00",     id="stats-time")

            # ── INFO TAB (second — next to MAIN) ────────────────
            with TabPane("INFO", id="t-info"):
                with VerticalScroll():
                    with Container(classes="cmd-page"):
                        yield Static(INFO_TEXT, id="info-body", classes="info-block")

                    # Detected-OS setup + permissions (auto-personalised)
                    with Container(classes="section"):
                        yield Static("SETUP  —  YOUR SYSTEM", classes="section-title")
                        yield Static(f"  detected:  {platform_label()}",
                                     classes="cmd-line")
                        yield Static(f"  install:   {install_command_hint()}",
                                     classes="cmd-line")
                        yield Static(f"  run:       {run_command_hint()}",
                                     classes="cmd-line")
                    with Container(classes="section"):
                        yield Static("PERMISSIONS  —  YOUR SYSTEM",
                                     classes="section-title")
                        for line in permission_lines():
                            if line:
                                yield Static(f"  {line}", classes="cmd-line")
                            else:
                                yield Static("", classes="cmd-blank")

                    with Container(classes="section"):
                        yield Static("COPY ADDRESSES", classes="section-title")
                        yield Static("  click any label to copy to clipboard",
                                     classes="section-hint")
                        with Horizontal(id="copy-row"):
                            yield CopyPill("github",   f"https://{GITHUB}")
                            yield CopyPill("bitcoin",  WALLETS[0][1])
                            yield CopyPill("ethereum", WALLETS[1][1])
                            yield CopyPill("solana",   WALLETS[3][1])
                    with Container(classes="section"):
                        yield Static("INSTALL", classes="section-title")
                        yield Static(f"  id        {self.identity['install_id']}",
                                     classes="cmd-line")
                        yield Static(
                            f"  sha-256   {self.identity['hardware_id'][:32]}",
                            classes="cmd-line")
                        yield Static(
                            f"            {self.identity['hardware_id'][32:]}",
                            classes="cmd-line")
                    with Container(classes="section"):
                        yield Static("CREDITS", classes="section-title")
                        yield Static(f"  {BRAND}  v{VERSION}", classes="cmd-line")
                        yield Static(f"  {GITHUB}", classes="cmd-line")
                        yield Static("", classes="cmd-blank")
                        yield Static("  built with  Textual · pynput · requests",
                                     classes="cmd-line")
                        yield Static("  sha-256 fingerprinting · discord webhooks",
                                     classes="cmd-line")

            # ── EMULATION TAB (macro recorder/player) ───────────
            with TabPane("EMULATION", id="t-emu"):
                with VerticalScroll():
                    with Container(classes="section"):
                        yield Static("MACRO RECORDER", classes="section-title")
                        yield Static(
                            "record your mouse + keyboard, then play it back",
                            classes="section-hint")
                        with Horizontal(id="emu-rec-row"):
                            yield Button("● RECORD", id="btn-emu-record")
                            yield Static("idle", id="emu-rec-status")
                        with Horizontal(id="emu-save-row"):
                            yield Input(placeholder="name this recording…",
                                        id="emu-name-input")
                            yield Button("save", id="btn-emu-save")

                    with Container(classes="section"):
                        yield Static("PLAYBACK", classes="section-title")
                        yield Static("speed", classes="section-hint")
                        yield Segmented("emuspeed", [
                            ("half", "0.5×"),
                            ("one",  "1×"),
                            ("two",  "2×"),
                        ], "one")
                        yield Static("repeat", classes="section-hint")
                        yield Segmented("emurepeat", [
                            ("once", "ONCE"),
                            ("loop", "LOOP"),
                        ], "once")
                        yield Static("a 3-2-1 countdown shows before playback starts",
                                     classes="section-hint")

                    with Container(classes="section"):
                        yield Static("SAVED RECORDINGS", classes="section-title")
                        with VerticalScroll(id="emu-list"):
                            yield Static("loading…", id="emu-loading",
                                         classes="cmd-line")

            # ── HISTORY TAB ─────────────────────────────────────
            with TabPane("HISTORY", id="t-hist"):
                yield Static("loading…", id="hist-loading", classes="cmd-line")
                with VerticalScroll(id="hist-container"):
                    pass   # dynamically populated on tab activate

            # ── FEEDBACK TAB ────────────────────────────────────
            with TabPane("FEEDBACK", id="t-fb"):
                with VerticalScroll():
                    with Container(classes="cmd-page"):
                        yield Static(
                            f"how's {BRAND.lower()} v{VERSION} working for you?",
                            classes="cmd-line")
                        with Horizontal(id="fb-stars-row"):
                            for i in range(1, 6):
                                yield Star(i)
                        yield Static("tap a star to rate", id="fb-rating-label",
                                     classes="cmd-line")
                        yield Static("comment (optional):", classes="cmd-line")
                        yield Input(placeholder="say anything…", id="fb-comment")
                        with Horizontal(id="fb-buttons-row"):
                            yield Button("submit feedback", id="fb-submit")
                        yield Static("", id="fb-status", classes="cmd-line")

        # Bottom bar
        yield Static("educational use only · not liable for misuse",
                     classes="warn-strip")

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.title = f"{BRAND}  v{VERSION}"
        term_maximize()
        self._install_signal_handlers()
        self._install_keyboard_listeners()

        threading.Thread(target=self._click_loop,     daemon=True).start()
        threading.Thread(target=self._antiafk_loop,   daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        threading.Thread(target=drain_pending,        daemon=True).start()

        cps_now = self._read_cps()
        threading.Thread(target=self._launch_and_update_discord,
                         args=(cps_now,), daemon=True).start()

        self.set_interval(0.1, self._tick_ui)
        self.set_interval(0.1, self._tick_esc)

    def on_screen_resume(self) -> None:
        self._sync_button_states()

    # ── Discord status ────────────────────────────────────────────────────────

    def _launch_and_update_discord(self, cps: int) -> None:
        ok = send_payload_sync(build_launch_embed(self.identity, cps))
        try:
            self.call_from_thread(self._update_discord_status, ok)
        except Exception:
            pass

    def _update_discord_status(self, ok: bool) -> None:
        try:
            widget = self.query_one("#discord-status", Static)
            if ok:
                widget.update("[#00e676]● discord ok[/]")
            else:
                err = (LAST_WEBHOOK_ERROR or "unknown")[:35]
                widget.update(f"[#ff1744]● discord {err}[/]")
        except Exception:
            pass

    # ── signals / shutdown ────────────────────────────────────────────────────

    def _install_signal_handlers(self) -> None:
        try:
            signal.signal(signal.SIGTERM,
                          lambda s, f: self._sync_shutdown("terminated_sigterm"))
            signal.signal(signal.SIGINT,
                          lambda s, f: self._sync_shutdown("interrupt_ctrl_c"))
            if hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP,
                              lambda s, f: self._sync_shutdown("terminal_closed_sighup"))
        except (ValueError, OSError):
            pass
        atexit.register(lambda: self._sync_shutdown("atexit"))

    def _sync_shutdown(self, reason: str) -> None:
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        self.stop_flag.set()
        self.active.set()
        self.antiafk_active.clear()

        # Make sure the held key (if any) is released
        try:
            if self.kb_ctrl:
                for k in ('w', 's'):
                    try: self.kb_ctrl.release(k)
                    except Exception: pass
        except Exception:
            pass

        try:
            with self.stats_lock:
                self._end_sub_session_unlocked()
        except Exception:
            pass

        try:
            self.identity["lifetime_clicks"]   += self.total_clicks
            self.identity["lifetime_sessions"] += len(self.sessions)
            save_identity(self.identity)
        except Exception:
            pass

        try:
            self._save_current_config()
        except Exception:
            pass

        try:
            send_payload_sync(build_report_embed(
                self._build_stats(), self.identity, reason=reason))
            time.sleep(0.3)
        except Exception:
            pass

        try:
            if self.esc_listener:
                self.esc_listener.stop()
        except Exception:
            pass

        try:
            if self._capture_listener:
                self._capture_listener.stop()
        except Exception:
            pass

        # Stop any active recording/playback
        self._rec_active  = False
        self._play_active = False
        for lst in (getattr(self, "_rec_mouse_listener", None),
                    getattr(self, "_rec_kb_listener", None)):
            try:
                if lst:
                    lst.stop()
            except Exception:
                pass

        term_maximize()   # restore terminal size on exit
        try:
            self.exit()
        except Exception:
            pass

    # ── reading state ─────────────────────────────────────────────────────────

    def _read_cps(self) -> int:
        try:
            v = int(self.query_one("#cps-input", Input).value.strip() or "20")
            return max(1, min(200, v))
        except Exception:
            return 20

    def _read_limit(self) -> Optional[int]:
        try:
            if not self.query_one("#limit-switch", Switch).value:
                return None
            v = int(self.query_one("#limit-input", Input).value.strip() or "0")
            return v if v > 0 else None
        except Exception:
            return None

    def _read_segmented(self, group: str, default: str) -> str:
        try:
            for seg in self.query(Segmented):
                if seg.group == group:
                    return seg.selected
        except Exception:
            pass
        return default

    def _read_button(self) -> str:   return self._read_segmented("button",   "left")
    def _read_pattern(self) -> str:  return self._read_segmented("pattern",  "constant")
    def _read_position(self) -> str: return self._read_segmented("position", "follow")
    def _read_esc_style(self) -> str:
        return self._read_segmented("escstyle", self.saved_cfg.get("esc_style", ESC_STYLE_BAR))

    def _read_emoji(self) -> str:
        try:
            v = self.query_one("#emoji-input", Input).value.strip()
            return v if v else DEFAULT_EMOJI
        except Exception:
            return self.saved_cfg.get("emoji", DEFAULT_EMOJI)

    def _read_failsafe(self) -> bool:
        try:
            return self.query_one("#failsafe-switch", Switch).value
        except Exception:
            return True

    def _save_current_config(self) -> None:
        save_config_dict({
            "cps":           self._read_cps(),
            "limit":         self._read_limit(),
            "button":        self._read_button(),
            "pattern":       self._read_pattern(),
            "position_mode": self._read_position(),
            "failsafe":      self._read_failsafe(),
            "esc_style":     self._read_esc_style(),
            "emoji":         self._read_emoji(),
        })

    @staticmethod
    def _describe_cps(v) -> str:
        try:
            v = int(v)
        except Exception:
            v = 20
        if v <= 5:  return "[#5a5a7a]slow · natural[/]"
        if v <= 15: return "[#5a5a7a]moderate · comfortable[/]"
        if v <= 30: return "[#00c8f0]fast · default[/]"
        if v <= 60: return "[#ff9100]very fast[/]"
        return "[#ff1744]extreme · may stress system[/]"

    # ── ui events ─────────────────────────────────────────────────────────────

    @on(Input.Changed, "#cps-input")
    def _cps_changed(self, event: Input.Changed) -> None:
        try:
            v = int(event.value.strip() or "0")
            self.query_one("#cps-desc", Static).update(self._describe_cps(v))
        except Exception:
            pass

    @on(Switch.Changed, "#limit-switch")
    def _limit_toggle(self, event: Switch.Changed) -> None:
        try:
            self.query_one("#limit-input", Input).disabled = not event.value
        except Exception:
            pass

    @on(Button.Pressed, "#btn-start")
    def _on_start(self, _) -> None: self._do_start()

    @on(Button.Pressed, "#btn-pause")
    def _on_pause(self, _) -> None: self._do_pause()

    @on(Button.Pressed, "#btn-stop")
    def _on_stop(self, _) -> None: self._do_stop()

    @on(Button.Pressed, "#btn-reset")
    def _on_reset(self, _) -> None: self._do_reset()

    @on(Button.Pressed, "#btn-capture-lock")
    def _on_capture_lock(self, _) -> None:
        """Enter capture mode — user must click the target spot 5 times."""
        if self._capturing:
            # Already capturing — cancel
            self._cancel_capture()
            return
        if not PYNPUT_OK:
            return
        self._capturing = True
        self._capture_count = 0
        self._capture_positions = []
        self._update_lock_display("click your target  0 / 5")
        try:
            self.query_one("#btn-capture-lock", Button).label = "✕ cancel capture"
        except Exception:
            pass
        # Start a background mouse listener just for capturing clicks
        try:
            self._capture_listener = pmouse.Listener(
                on_click=self._on_capture_click)
            self._capture_listener.daemon = True
            self._capture_listener.start()
        except Exception:
            self._capturing = False

    def _on_capture_click(self, x: int, y: int, button, pressed: bool) -> Optional[bool]:
        """Pynput mouse callback — counts left-clicks while in capture mode."""
        if not self._capturing:
            return False   # stop listener
        if not pressed or button != pmouse.Button.left:
            return None    # only count left-press, ignore release / other buttons
        self._capture_count += 1
        self._capture_positions.append((int(x), int(y)))
        remaining = 5 - self._capture_count
        try:
            self.call_from_thread(
                self._update_lock_display,
                f"click your target  {self._capture_count} / 5"
            )
        except Exception:
            pass
        if self._capture_count >= 5:
            # Average the 5 positions for accuracy
            avg_x = int(sum(p[0] for p in self._capture_positions) / 5)
            avg_y = int(sum(p[1] for p in self._capture_positions) / 5)
            self.lock_pos = (avg_x, avg_y)
            try:
                self.call_from_thread(self._finish_capture)
            except Exception:
                pass
            return False   # stop listener
        return None

    def _finish_capture(self) -> None:
        self._capturing = False
        self._capture_listener = None
        x, y = self.lock_pos
        self._update_lock_display(f"[#00e676]locked  x {x}  y {y}[/]")
        try:
            self.query_one("#btn-capture-lock", Button).label = "◎ CAPTURE LOCK POINT"
        except Exception:
            pass

    def _cancel_capture(self) -> None:
        self._capturing = False
        try:
            if self._capture_listener:
                self._capture_listener.stop()
                self._capture_listener = None
        except Exception:
            pass
        self._update_lock_display("not set" if self.lock_pos is None
                                  else f"[#00c8f0]x {self.lock_pos[0]}  y {self.lock_pos[1]}[/]")
        try:
            self.query_one("#btn-capture-lock", Button).label = "◎ CAPTURE LOCK POINT"
        except Exception:
            pass

    def _update_lock_display(self, text: str) -> None:
        try:
            self.query_one("#lock-display", Static).update(text)
        except Exception:
            pass

    @on(Button.Pressed, "#btn-mini")
    def _on_mini(self, _) -> None: self.action_mini()

    @on(Button.Pressed, "#btn-antiafk")
    def _on_antiafk(self, _) -> None: self.action_antiafk()

    # ── inline feedback handlers (feedback now lives on its tab) ──────────────

    fb_stars: int = 0
    fb_sent:  bool = False

    @on(Star.Clicked)
    def _fb_star_clicked(self, message: Star.Clicked) -> None:
        self.fb_stars = message.value
        for star in self.query(Star):
            star.light(star.value <= self.fb_stars)
        labels = {1: "needs work", 2: "meh", 3: "fine", 4: "good", 5: "great!"}
        try:
            self.query_one("#fb-rating-label", Static).update(
                f"[#ff9100 bold]{self.fb_stars}/5  ·  {labels[self.fb_stars]}[/]")
        except Exception:
            pass

    @on(Button.Pressed, "#fb-submit")
    def _fb_submit_clicked(self, _) -> None:
        if self.fb_sent:
            return
        if self.fb_stars == 0:
            try:
                self.query_one("#fb-status", Static).update(
                    "[#ff1744]tap a star first[/]")
            except Exception:
                pass
            return
        try:
            comment = self.query_one("#fb-comment", Input).value
        except Exception:
            comment = ""
        self.fb_sent = True
        try:
            self.query_one("#fb-status", Static).update("[#5a5a7a]sending…[/]")
        except Exception:
            pass
        threading.Thread(
            target=self._fb_send_worker,
            args=(self.fb_stars, comment),
            daemon=True).start()

    def _fb_send_worker(self, stars: int, comment: str) -> None:
        ok = send_payload_sync(build_feedback_embed(stars, comment, self.identity))
        try:
            self.call_from_thread(self._fb_after_send, ok)
        except Exception:
            pass

    def _fb_after_send(self, ok: bool) -> None:
        try:
            status = self.query_one("#fb-status", Static)
            if ok:
                status.update("[#00e676]✓ thanks — feedback sent[/]")
            else:
                err = (LAST_WEBHOOK_ERROR or "unknown")[:40]
                status.update(f"[#ff1744]× couldn't send: {err} — queued for retry[/]")
        except Exception:
            pass

    # ── history tab population (refresh whenever shown) ───────────────────────

    @on(TabbedContent.TabActivated)
    def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab_id = getattr(event.tab, "id", None) or getattr(event.pane, "id", "")
        # Textual prefixes tab ids with "--content-tab-"; check pane id instead
        pane_id = getattr(event.pane, "id", "")
        if pane_id == "t-hist":
            self._refresh_history_tab()
        elif pane_id == "t-emu":
            self._refresh_emu_list()

    def _refresh_history_tab(self) -> None:
        # Hide the loading label
        try:
            self.query_one("#hist-loading", Static).update("")
        except Exception:
            pass
        try:
            container = self.query_one("#hist-container", VerticalScroll)
        except Exception:
            return
        # Clear existing rows
        for child in list(container.children):
            try:
                child.remove()
            except Exception:
                pass
        sessions = load_all_sessions()
        if not sessions:
            container.mount(Static(
                "[#5a5a7a]no sessions yet — start clicking[/]",
                classes="cmd-line"))
            return
        container.mount(Static(
            f"[#00c8f0]{len(sessions)} session(s)[/]",
            classes="cmd-line"))
        container.mount(Static(
            "[#5a5a7a]   #      started        clicks       duration[/]",
            classes="cmd-line"))
        for i, s in enumerate(sessions[:500], 1):
            started  = s.get("started", "?")
            clicks   = s.get("clicks", 0)
            duration = s.get("duration", 0.0)
            container.mount(Static(
                f"  {i:>3}     {started:>8}     {clicks:>7,}     {duration:>7.1f}s",
                classes="cmd-line"))

    # ── EMULATION: recorder ────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-emu-record")
    def _on_emu_record(self, _) -> None:
        if self._play_active:
            # Stop playback (including loops)
            self._play_active = False
            self._update_emu_status("[#ff9100]playback stopped[/]")
            return
        if self._rec_active:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        if not PYNPUT_OK:
            self.notify("pynput not installed", severity="error")
            return
        if self._play_active:
            return
        # Use a 3-2-1 countdown so the click on the RECORD button (and the
        # user moving away from the app window) isn't captured into the macro.
        self._rec_active = True
        self._rec_events = []
        self._rec_ui_clicks = []
        self._rec_counts = {"move": 0, "click": 0, "key": 0}
        try:
            self.query_one("#btn-emu-record", Button).label = "■ STOP"
        except Exception:
            pass
        threading.Thread(target=self._recording_preroll, daemon=True).start()

    def _recording_preroll(self) -> None:
        for n in (3, 2, 1):
            if not self._rec_active:
                return
            try:
                self.call_from_thread(
                    self._update_emu_status,
                    f"[#ff9100]recording starts in {n}…  (press Esc to stop)[/]")
            except Exception:
                pass
            time.sleep(1.0)
        if not self._rec_active:
            return
        # NOW begin capturing
        self._rec_start = time.monotonic()
        try:
            self.call_from_thread(
                self._update_emu_status,
                "[#ff1744]● recording…  move / click / type  (Esc to stop)[/]")
        except Exception:
            pass
        try:
            self._rec_mouse_listener = pmouse.Listener(
                on_move=self._rec_on_move,
                on_click=self._rec_on_click)
            self._rec_mouse_listener.daemon = True
            self._rec_mouse_listener.start()
        except Exception:
            self._rec_mouse_listener = None
        try:
            self._rec_kb_listener = pkeyboard.Listener(
                on_press=lambda k: self._rec_on_key(k, True),
                on_release=lambda k: self._rec_on_key(k, False))
            self._rec_kb_listener.daemon = True
            self._rec_kb_listener.start()
        except Exception:
            self._rec_kb_listener = None

    UI_CLICK_WINDOW = 0.18   # seconds either side of a click on our own UI

    def _scrub_own_clicks(self) -> int:
        """Drop the clicks that landed on this app's own window.

        Two passes, because either one alone leaves a hole:
          1. Anything matching a mouse event Textual delivered to us — the
             STOP button, a wallet copy pill, any control clicked mid-record.
          2. A trailing click at the very end, which is the press on STOP
             when the release arrived after the listener had already been
             torn down and Textual never told us about it.
        """
        ui = sorted(getattr(self, "_rec_ui_clicks", []))
        removed = 0

        if ui:
            kept = []
            for ev in self._rec_events:
                if ev.get("type") == "click":
                    mono = ev.get("_mono")
                    if mono is not None and any(
                            abs(mono - u) <= self.UI_CLICK_WINDOW for u in ui):
                        removed += 1
                        continue
                kept.append(ev)
            self._rec_events = kept

        # Pass 2: a click still sitting in the last moments of the recording
        # is the one that stopped it.
        stop_mono = time.monotonic()
        while self._rec_events:
            last = self._rec_events[-1]
            if last.get("type") != "click":
                break
            mono = last.get("_mono")
            if mono is None or (stop_mono - mono) > 0.6:
                break
            self._rec_events.pop()
            removed += 1

        for ev in self._rec_events:
            ev.pop("_mono", None)

        if removed:
            self._rec_counts["click"] = max(
                0, sum(1 for e in self._rec_events
                       if e.get("type") == "click" and e.get("pressed")))
        return removed

    def _stop_recording(self) -> None:
        self._rec_active = False
        for lst in (self._rec_mouse_listener, self._rec_kb_listener):
            try:
                if lst:
                    lst.stop()
            except Exception:
                pass
        self._rec_mouse_listener = None
        self._rec_kb_listener    = None

        try:
            dropped = self._scrub_own_clicks()
        except Exception:
            dropped = 0

        n = len(self._rec_events)
        c = self._rec_counts
        tail = (f"  [#5a5a7a]· dropped {dropped} click"
                f"{'' if dropped == 1 else 's'} on this window[/]") if dropped else ""
        self._update_emu_status(
            f"[#00e676]recorded {n} events[/]  "
            f"[#5a5a7a]({c['move']} moves · {c['click']} clicks · {c['key']} keys) — "
            f"name it & save[/]" + tail)
        try:
            self.query_one("#btn-emu-record", Button).label = "● RECORD"
        except Exception:
            pass

    def _rec_on_move(self, x, y) -> None:
        if not self._rec_active:
            return False
        # Throttle moves to ~120/sec to keep files reasonable
        t = time.monotonic() - self._rec_start
        if self._rec_events:
            last = self._rec_events[-1]
            if last["type"] == "move" and (t - last["t"]) < 0.008:
                last["x"], last["y"] = int(x), int(y)   # coalesce
                return None
        self._rec_events.append({"t": t, "type": "move", "x": int(x), "y": int(y)})
        self._rec_counts["move"] += 1
        self._bump_emu_counter()

    def on_mouse_down(self, event) -> None:
        """Every mouse press that lands on THIS app's own window.

        The global recorder cannot tell our window from any other, so a click
        on our own UI — the STOP button, a wallet copy pill — gets recorded
        like any other and replays later, walking the pointer back here and
        clicking whatever now sits at those coordinates.

        Textual only delivers this event when the click actually hit us, so
        it is an exact marker. The timestamps collected here are used to
        remove the matching events from the recording.
        """
        if getattr(self, "_rec_active", False):
            try:
                self._rec_ui_clicks.append(time.monotonic())
            except Exception:
                pass

    def on_mouse_up(self, event) -> None:
        if getattr(self, "_rec_active", False):
            try:
                self._rec_ui_clicks.append(time.monotonic())
            except Exception:
                pass

    def _rec_on_click(self, x, y, button, pressed) -> None:
        if not self._rec_active:
            return False
        t = time.monotonic() - self._rec_start
        name = getattr(button, "name", "left")
        self._rec_events.append({
            "t": t, "type": "click", "x": int(x), "y": int(y),
            "button": name, "pressed": bool(pressed),
            # wall-clock, kept only until the recording is scrubbed
            "_mono": time.monotonic()})
        if pressed:
            self._rec_counts["click"] += 1
        self._bump_emu_counter()

    def _rec_on_key(self, key, pressed) -> None:
        if not self._rec_active:
            return False
        # Stop recording cleanly if Esc pressed
        if key == pkeyboard.Key.esc:
            try:
                self.call_from_thread(self._stop_recording)
            except Exception:
                pass
            return False
        t = time.monotonic() - self._rec_start
        self._rec_events.append({
            "t": t, "type": "key",
            "key": self._serialise_key(key), "pressed": bool(pressed)})
        if pressed:
            self._rec_counts["key"] += 1
        self._bump_emu_counter()

    def _serialise_key(self, key) -> str:
        """Turn a pynput key into a string we can replay."""
        try:
            if hasattr(key, "char") and key.char is not None:
                return key.char
            # Special key e.g. Key.space → "Key.space"
            return str(key)
        except Exception:
            return ""

    def _bump_emu_counter(self) -> None:
        if not self._rec_active:
            return
        c = self._rec_counts
        try:
            self.call_from_thread(
                self._update_emu_status,
                f"[#ff1744]● recording…[/]  "
                f"[#5a5a7a]{c['move']} moves · {c['click']} clicks · {c['key']} keys[/]")
        except Exception:
            pass

    def _update_emu_status(self, text: str) -> None:
        try:
            self.query_one("#emu-rec-status", Static).update(text)
        except Exception:
            pass

    @on(Button.Pressed, "#btn-emu-save")
    def _on_emu_save(self, _) -> None:
        if self._rec_active:
            self._stop_recording()
        if not self._rec_events:
            self._update_emu_status("[#ff1744]nothing recorded yet[/]")
            return
        try:
            name = self.query_one("#emu-name-input", Input).value.strip()
        except Exception:
            name = ""
        if not name:
            name = dt.datetime.now().strftime("rec-%H%M%S")
        stem = save_recording(name, self._rec_events)
        self._update_emu_status(f"[#00e676]saved as '{stem}'[/]")
        try:
            self.query_one("#emu-name-input", Input).value = ""
        except Exception:
            pass
        self._refresh_emu_list()

    # ── EMULATION: saved list ───────────────────────────────────────────────────

    def _refresh_emu_list(self) -> None:
        try:
            self.query_one("#emu-loading", Static).update("")
        except Exception:
            pass
        try:
            container = self.query_one("#emu-list", VerticalScroll)
        except Exception:
            return
        for child in list(container.children):
            try:
                child.remove()
            except Exception:
                pass
        recs = list_recordings()
        if not recs:
            container.mount(Static("[#5a5a7a]no recordings yet — record one above[/]",
                                   classes="cmd-line"))
            return
        for stem in recs:
            container.mount(RecordingRow(stem))

    def emu_play(self, stem: str) -> None:
        """Play a saved recording (called by RecordingRow)."""
        if self._play_active or self._rec_active:
            return
        rec = load_recording(stem)
        if not rec or not rec.get("events"):
            self._update_emu_status(f"[#ff1744]'{stem}' is empty[/]")
            return
        speed = {"half": 0.5, "one": 1.0, "two": 2.0}.get(
            self._read_segmented("emuspeed", "one"), 1.0)
        loop = self._read_segmented("emurepeat", "once") == "loop"
        self._play_active = True
        # Turn the record button into a stop control during playback
        try:
            self.query_one("#btn-emu-record", Button).label = "■ STOP PLAYBACK"
        except Exception:
            pass
        self._play_thread = threading.Thread(
            target=self._play_worker,
            args=(rec["events"], speed, loop, stem),
            daemon=True)
        self._play_thread.start()

    def emu_delete(self, stem: str) -> None:
        delete_recording(stem)
        self._refresh_emu_list()

    def _play_worker(self, events: list, speed: float, loop: bool, stem: str) -> None:
        if not PYNPUT_OK:
            self._play_active = False
            return
        mc = self.mouse_ctrl
        kc = self.kb_ctrl
        btn_map = {
            "left":   pmouse.Button.left,
            "right":  pmouse.Button.right,
            "middle": pmouse.Button.middle,
        }
        # 3-2-1 preroll
        for n in (3, 2, 1):
            if not self._play_active:
                self._play_active = False
                self._reset_emu_button()
                return
            try:
                self.call_from_thread(
                    self._update_emu_status,
                    f"[#ff9100]playing '{stem}' in {n}…  (STOP to cancel)[/]")
            except Exception:
                pass
            time.sleep(1.0)

        loop_n = 0
        try:
            while True:
                loop_n += 1
                if loop:
                    try:
                        self.call_from_thread(
                            self._update_emu_status,
                            f"[#ff1744]▶ playing '{stem}'  (loop {loop_n}) — STOP to cancel[/]")
                    except Exception:
                        pass
                t_prev = 0.0
                for ev in events:
                    if not self._play_active:
                        break
                    # Honor inter-event timing, scaled by speed
                    dt_ev = (ev["t"] - t_prev) / max(speed, 0.01)
                    if dt_ev > 0:
                        # Sleep in small slices so STOP responds quickly
                        slept = 0.0
                        target = min(dt_ev, 10.0)
                        while slept < target and self._play_active:
                            chunk = min(0.05, target - slept)
                            time.sleep(chunk)
                            slept += chunk
                    t_prev = ev["t"]
                    if not self._play_active:
                        break
                    try:
                        if ev["type"] == "move":
                            mc.position = (ev["x"], ev["y"])
                        elif ev["type"] == "click":
                            mc.position = (ev["x"], ev["y"])
                            b = btn_map.get(ev.get("button", "left"),
                                            pmouse.Button.left)
                            if ev["pressed"]:
                                mc.press(b)
                            else:
                                mc.release(b)
                        elif ev["type"] == "key":
                            k = self._deserialise_key(ev["key"])
                            if k is not None:
                                if ev["pressed"]:
                                    kc.press(k)
                                else:
                                    kc.release(k)
                    except Exception:
                        pass
                if not loop or not self._play_active:
                    break
        finally:
            self._play_active = False
            try:
                self.call_from_thread(
                    self._update_emu_status, "[#00e676]playback done[/]")
                self.call_from_thread(self._reset_emu_button)
            except Exception:
                pass

    def _reset_emu_button(self) -> None:
        try:
            self.query_one("#btn-emu-record", Button).label = "● RECORD"
        except Exception:
            pass

    def _deserialise_key(self, s: str):
        """Turn a serialised key string back into a pynput key."""
        if not s:
            return None
        try:
            if s.startswith("Key."):
                name = s.split(".", 1)[1]
                return getattr(pkeyboard.Key, name, None)
            # Single character
            return s
        except Exception:
            return None

    # ── core actions (called from UI + hotkeys) ───────────────────────────────

    def _do_start(self) -> None:
        if not PYNPUT_OK:
            self.notify("pynput not installed — run: pip install pynput requests",
                        severity="error")
            return
        if self.active.is_set() and not self.paused.is_set():
            return
        with self.stats_lock:
            self._begin_sub_session_unlocked()
        self.toggles += 1
        self.active.set()
        self.paused.clear()
        self._set_status("[#00e676 bold]● clicking[/]")
        self._sync_button_states()

    def _do_pause(self) -> None:
        if not self.active.is_set():
            return
        if self.paused.is_set():
            self.paused.clear()
            with self.stats_lock:
                self._begin_sub_session_unlocked()
            self._set_status("[#00e676 bold]● clicking[/]")
        else:
            with self.stats_lock:
                self._end_sub_session_unlocked()
            self.paused.set()
            self._set_status("[#ff9100 bold]● paused[/]")
        self._sync_button_states()

    def _do_stop(self) -> None:
        self.active.clear()
        self.paused.clear()
        with self.stats_lock:
            self._end_sub_session_unlocked()
        self._set_status("[#ff1744 bold]● stopped[/]")
        self._sync_button_states()

    def _do_reset(self) -> None:
        if self.active.is_set():
            self._do_stop()
        if self._capturing:
            self._cancel_capture()
        with self.stats_lock:
            self.total_clicks  = 0
            self.toggles       = 0
            self.clicking_time = 0.0
            self.sessions.clear()
            self.cps_window.clear()
        self.lock_pos = None
        self._update_lock_display("not set")
        self._set_status("[#5a5a7a bold]● idle[/]")
        try:
            self.query_one("#limit-progress", ProgressBar).update(progress=0)
            self.query_one("#click-count",    Static).update("0 clicks")
        except Exception:
            pass

    def _toggle_start_stop(self) -> None:
        if self.active.is_set():
            self._do_stop()
        else:
            self._do_start()

    def _sync_button_states(self) -> None:
        running = self.active.is_set() and not self.paused.is_set()
        paused  = self.paused.is_set()
        try:
            self.query_one("#btn-start", Button).disabled = running or paused
            self.query_one("#btn-pause", Button).disabled = not (running or paused)
            self.query_one("#btn-stop",  Button).disabled = not (running or paused)
            pause_btn = self.query_one("#btn-pause", Button)
            pause_btn.label = "resume" if paused else "pause"
            # anti-afk style
            aa = self.query_one("#btn-antiafk", Button)
            if self.antiafk_active.is_set():
                aa.add_class("-on")
                aa.label = "anti-afk ON"
            else:
                aa.remove_class("-on")
                aa.label = "anti-afk"
        except Exception:
            pass

    # ── action_* (used by both pynput hotkeys via call_from_thread and UI) ────

    def action_mini(self) -> None:
        """Toggle mini mode. Resizes the host terminal window on macOS."""
        if isinstance(self.screen, MiniScreen):
            term_maximize()
            self.pop_screen()
            return
        while isinstance(self.screen, (InfoScreen, HistoryScreen, FeedbackScreen)):
            try:
                self.pop_screen()
            except Exception:
                break
        term_resize(MINI_TERM_ROWS, MINI_TERM_COLS)
        self.push_screen(MiniScreen())

    def action_restore_from_mini(self) -> None:
        try:
            if isinstance(self.screen, MiniScreen):
                self.pop_screen()
        except Exception:
            pass

    def action_info(self) -> None:
        # Don't stack multiple
        if not isinstance(self.screen, InfoScreen):
            self.push_screen(InfoScreen())

    def action_history(self) -> None:
        if not isinstance(self.screen, HistoryScreen):
            self.push_screen(HistoryScreen())

    def action_feedback(self) -> None:
        if not isinstance(self.screen, FeedbackScreen):
            self.push_screen(FeedbackScreen())

    def action_antiafk(self) -> None:
        if self.antiafk_active.is_set():
            self.antiafk_active.clear()
            # Release any held keys immediately
            try:
                if self.kb_ctrl:
                    for k in ('w', 's'):
                        try: self.kb_ctrl.release(k)
                        except Exception: pass
            except Exception:
                pass
        else:
            self.antiafk_active.set()
        self._sync_button_states()

    def action_force_quit(self) -> None:
        self._sync_shutdown("interrupt_ctrl_c")

    # ── periodic ticks ────────────────────────────────────────────────────────

    def _tick_ui(self) -> None:
        try:
            now_ns = time.monotonic_ns()
            cutoff = now_ns - int(2.0 * 1e9)
            with self.stats_lock:
                while self.cps_window and self.cps_window[0] < cutoff:
                    self.cps_window.popleft()
                eff    = len(self.cps_window) / 2.0
                clicks = self.total_clicks
                nsess  = len(self.sessions)

            self.query_one("#click-count", Static).update(f"{clicks:,} clicks")
            self.query_one("#stats-eff",   Static).update(f"eff cps {eff:.1f}")
            self.query_one("#stats-sess",  Static).update(f"sessions {nsess}")

            elapsed = time.time() - self.app_start
            h = int(elapsed) // 3600
            m = (int(elapsed) % 3600) // 60
            s = int(elapsed) % 60
            self.query_one("#stats-time", Static).update(f"{h:02d}:{m:02d}:{s:02d}")

            # anti-afk ribbon
            self.query_one("#ribbon-antiafk", Static).update(
                "anti-afk: [#00e676]ON[/]" if self.antiafk_active.is_set()
                else "anti-afk: off"
            )

            limit = self._read_limit()
            pb = self.query_one("#limit-progress", ProgressBar)
            if limit:
                pb.update(total=limit, progress=min(clicks, limit))
                if clicks >= limit and self.active.is_set():
                    self._do_stop()
                    self._set_status("[#00e676 bold]● limit reached[/]")
                    threading.Thread(
                        target=send_payload_sync,
                        args=(build_report_embed(self._build_stats(), self.identity,
                                                 reason="limit_reached",
                                                 is_checkpoint=True),),
                        daemon=True).start()
            else:
                pb.update(total=100, progress=0)
        except Exception:
            pass

    def _esc_banner_text(self) -> str:
        """One-liner for the mini screen. Only shown during quit countdown."""
        if self._quitting:
            elapsed   = time.monotonic() - self._quit_start
            remaining = max(0.0, 2.0 - elapsed)
            cnt = max(1, int(remaining) + (1 if remaining > 0 else 0))
            return f"[#ffffff bold on #e06000]  QUITTING IN {cnt}…  [/]"
        return ""

    def _tick_esc(self) -> None:
        """Drive the bar at the top of the main screen (arm warning OR quit countdown)."""
        try:
            frame = self.query_one("#esc-bar-frame", Container)
            fill  = self.query_one("#esc-bar-fill",  Static)
        except Exception:
            return

        # ── Quit countdown (Esc×3) ───────────────────────────────────────────
        if self._quitting:
            elapsed   = time.monotonic() - self._quit_start
            remaining = max(0.0, 2.0 - elapsed)
            pct       = remaining / 2.0
            cnt = max(1, int(remaining) + (1 if remaining > 0 else 0))
            frame.styles.height = 4
            fill.styles.width = f"{max(1, int(pct * 100))}%"
            fill.remove_class("-warn")
            fill.remove_class("-final")
            fill.add_class("-quitting")
            fill.update(
                f"\n  ✕  QUITTING IN  [bold]{cnt}[/]  ✕"
                f"                                                    \n"
            )
            return

        # ── Esc arm window (taps 1 and 2): show a shrinking red bar ──────────
        if self.esc_count > 0:
            elapsed = time.monotonic() - self.esc_first_at
            if elapsed > ESC_WINDOW_S:
                # Window expired without 3 taps — reset, hide bar
                self.esc_count = 0
                self.esc_first_at = 0.0
                frame.styles.height = 0
                fill.remove_class("-warn")
                fill.remove_class("-final")
                fill.remove_class("-quitting")
                fill.styles.width = "100%"
                fill.update("")
                return
            remaining = max(0.0, ESC_WINDOW_S - elapsed)
            pct       = max(0.0, min(1.0, remaining / ESC_WINDOW_S))
            taps_left = 3 - self.esc_count
            emoji     = self._read_emoji()
            frame.styles.height = 4
            fill.styles.width = f"{max(1, int(pct * 100))}%"
            fill.remove_class("-quitting")
            fill.remove_class("-warn")
            fill.remove_class("-final")
            if self.esc_count == 2:
                fill.add_class("-final")    # brightest — one more to quit
            else:
                fill.add_class("-warn")
            fill.update(
                f"\n  {emoji}  ESC {self.esc_count}/3  —  tap {taps_left} more to QUIT  "
                f"[{remaining:.1f}s]                              \n"
            )
            return

        # ── Idle — bar hidden ────────────────────────────────────────────────
        frame.styles.height = 0
        fill.remove_class("-warn")
        fill.remove_class("-final")
        fill.remove_class("-quitting")
        fill.styles.width = "100%"
        fill.update("")

    def _set_status(self, markup: str) -> None:
        try:
            self.query_one("#status-label", Static).update(markup)
        except Exception:
            pass

    # ── stats helpers ─────────────────────────────────────────────────────────

    def _build_stats(self) -> dict:
        lim = self._read_limit()
        return {
            "total_clicks":      self.total_clicks,
            "toggles":           self.toggles,
            "total_duration":    time.time() - self.app_start,
            "clicking_duration": self.clicking_time,
            "target_cps":        self._read_cps(),
            "limit":             str(lim) if lim else "None",
            "pattern":           self._read_pattern(),
            "button":            self._read_button(),
            "sessions":          list(self.sessions),
        }

    def _begin_sub_session_unlocked(self) -> None:
        if self._read_position() == "lock" and self.lock_pos is None and self.mouse_ctrl:
            try:
                p = self.mouse_ctrl.position
                self.lock_pos = (int(p[0]), int(p[1]))
            except Exception:
                pass
        self.sub_start  = time.monotonic()
        self.sub_clicks = 0

    def _end_sub_session_unlocked(self) -> None:
        if self.sub_start is None:
            return
        dur = time.monotonic() - self.sub_start
        self.clicking_time += dur
        sess = {
            "started":  dt.datetime.fromtimestamp(time.time() - dur).strftime("%H:%M:%S"),
            "clicks":   self.sub_clicks,
            "duration": dur,
        }
        self.sessions.append(sess)
        try:
            append_session(sess)
        except Exception:
            pass
        self.sub_start  = None
        self.sub_clicks = 0

    # ── click loop ────────────────────────────────────────────────────────────

    def _near_corner(self, x: int, y: int) -> bool:
        return x < CORNER_PX and y < CORNER_PX

    def _click_loop(self) -> None:
        if not PYNPUT_OK:
            return
        mc = self.mouse_ctrl
        btn_map = {
            "left":   pmouse.Button.left,
            "right":  pmouse.Button.right,
            "middle": pmouse.Button.middle,
        }
        burst_count    = 0
        in_burst_pause = False
        burst_end      = 0.0

        while not self.stop_flag.is_set():
            if not self.active.is_set():
                self.active.wait(timeout=0.05)
                continue
            if self.paused.is_set():
                time.sleep(0.05)
                continue

            try:
                cps      = self._read_cps()
                pattern  = self._read_pattern()
                button   = self._read_button()
                failsafe = self._read_failsafe()
                pos_mode = self._read_position()
            except Exception:
                time.sleep(0.05)
                continue

            if failsafe:
                try:
                    p = mc.position
                    if self._near_corner(int(p[0]), int(p[1])):
                        self.active.clear()
                        with self.stats_lock:
                            self._end_sub_session_unlocked()
                        try:
                            self.call_from_thread(
                                self._set_status,
                                "[#ff1744 bold]● failsafe — top-left[/]")
                            self.call_from_thread(self._sync_button_states)
                        except Exception:
                            pass
                        continue
                except Exception:
                    pass

            if pattern == "burst" and in_burst_pause:
                if time.monotonic() < burst_end:
                    time.sleep(0.005)
                    continue
                in_burst_pause = False
                burst_count    = 0

            t0 = time.monotonic_ns()
            try:
                if pos_mode == "lock" and self.lock_pos is not None:
                    mc.position = self.lock_pos
                if button == "double":
                    mc.click(pmouse.Button.left, 2)
                else:
                    mc.click(btn_map.get(button, pmouse.Button.left))
            except Exception as _click_err:
                # Log first 3 errors so we can see what's wrong
                if not hasattr(self, '_click_err_count'):
                    self._click_err_count = 0
                if self._click_err_count < 3:
                    self._click_err_count += 1
                    try:
                        APP_DIR.mkdir(parents=True, exist_ok=True)
                        with (APP_DIR / "click_errors.log").open("a") as _f:
                            _f.write(
                                f"{dt.datetime.now().isoformat(timespec='seconds')} "
                                f"click error: {type(_click_err).__name__}: {_click_err}\n"
                            )
                    except Exception:
                        pass
                time.sleep(0.01)
                continue

            now_ns = time.monotonic_ns()
            with self.stats_lock:
                self.total_clicks += 1
                self.sub_clicks   += 1
                self.cps_window.append(now_ns)

            if pattern == "burst":
                burst_count += 1
                if burst_count >= 5:
                    in_burst_pause = True
                    burst_end = time.monotonic() + 1.0
                    continue

            click_s  = (now_ns - t0) / 1e9
            interval = 1.0 / max(cps, 1)
            if pattern == "jitter":
                interval = max(0.001, random.gauss(interval, interval * 0.10))
            sleep_s = interval - click_s
            if sleep_s > 0:
                time.sleep(sleep_s)

    # ── anti-AFK loop ─────────────────────────────────────────────────────────

    def _antiafk_loop(self) -> None:
        if not PYNPUT_OK:
            return
        kb = self.kb_ctrl
        # State: which key is currently held, and when it should be released
        cur_key = None     # 'w' or 's' or None
        switch_at = 0.0
        while not self.stop_flag.is_set():
            if not self.antiafk_active.is_set():
                # Make sure nothing is held
                if cur_key:
                    try: kb.release(cur_key)
                    except Exception: pass
                    cur_key = None
                time.sleep(0.1)
                continue
            now = time.monotonic()
            # If no key held, start with 'w'
            if cur_key is None:
                try:
                    kb.press('w')
                    cur_key = 'w'
                    switch_at = now + ANTIAFK_HOLD_S
                except Exception:
                    time.sleep(0.5)
                    continue
            elif now >= switch_at:
                # Time to switch
                try:
                    kb.release(cur_key)
                except Exception:
                    pass
                next_key = 's' if cur_key == 'w' else 'w'
                try:
                    kb.press(next_key)
                    cur_key = next_key
                    switch_at = now + ANTIAFK_HOLD_S
                except Exception:
                    cur_key = None
                    time.sleep(0.5)
                    continue
            else:
                # Sleep until either switch time or anti-afk turned off
                time.sleep(0.1)

        # Cleanup on shutdown
        if cur_key:
            try: kb.release(cur_key)
            except Exception: pass

    # ── heartbeat ─────────────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        last_fire = time.monotonic()
        while not self.stop_flag.is_set():
            time.sleep(2.0)
            if self.stop_flag.is_set():
                return
            if time.monotonic() - last_fire < HEARTBEAT_S:
                continue
            last_fire = time.monotonic()
            try:
                payload = build_report_embed(
                    self._build_stats(), self.identity,
                    reason="heartbeat_hourly", is_checkpoint=True)
                threading.Thread(target=send_payload_sync,
                                 args=(payload, False),
                                 daemon=True).start()
            except Exception:
                pass

    # ── global keyboard: Opt-alone = on/off, Both Opts = pause, Esc logic ────

    def _install_keyboard_listeners(self) -> None:
        if not PYNPUT_OK:
            return
        try:
            self.esc_listener = pkeyboard.Listener(
                on_press=self._on_global_press,
                on_release=self._on_global_release)
            self.esc_listener.daemon = True
            self.esc_listener.start()
        except Exception:
            self.esc_listener = None

    def _is_opt(self, key) -> bool:
        """Single Option/Alt key (start/stop toggle).
        - macOS: both sides report as Key.alt (vk=58)
        - Windows/Linux: left=Key.alt_l, right=Key.alt_r (also accept Key.alt)
        - Windows/Linux with a non-US layout: the right Alt is AltGr and
          arrives as Key.alt_gr, not Key.alt_r. Without this the right Alt
          does nothing at all on a UK, German, French, Nordic or Polish
          keyboard — which is most keyboards outside the US.
        """
        try:
            keys = [pkeyboard.Key.alt, pkeyboard.Key.alt_l, pkeyboard.Key.alt_r]
            gr = getattr(pkeyboard.Key, "alt_gr", None)
            if gr is not None:
                keys.append(gr)
            return key in keys
        except Exception:
            return False

    def _is_altgr(self, key) -> bool:
        try:
            gr = getattr(pkeyboard.Key, "alt_gr", None)
            return gr is not None and key == gr
        except Exception:
            return False

    def _is_both_opts(self, key) -> bool:
        """Both Option/Alt keys at once (pause/resume).
        - macOS: pressing both fires a distinct raw keycode vk=160
        - Windows/Linux: no combined code, so this returns False there and
          the both-alt case is detected via tracking alt_l + alt_r separately
          in _on_global_press.
        """
        try:
            return hasattr(key, 'vk') and key.vk == 160
        except Exception:
            return False

    def _opt_side(self, key):
        """Return 'l', 'r', or None for which Alt side a key is (Windows/Linux).

        AltGr counts as the right side — it is physically the right Alt.
        """
        try:
            if key == pkeyboard.Key.alt_l:
                return 'l'
            if key == pkeyboard.Key.alt_r:
                return 'r'
            if self._is_altgr(key):
                return 'r'
        except Exception:
            pass
        return None

    def _on_global_press(self, key) -> Optional[bool]:
        try:
            # ── Esc ──────────────────────────────────────────────
            if key == pkeyboard.Key.esc:
                return self._handle_esc_press()

            # ── macOS: both Options together → pause/resume ──────
            if self._is_both_opts(key):
                self._opt_contaminated = True
                try:
                    self.call_from_thread(self._do_pause)
                except Exception:
                    pass
                return None

            # ── F8 / F9: the hotkeys that do not fight Windows ────
            #    Tapping Alt on its own activates the menu bar of whatever
            #    window has focus, so on Windows the Alt hotkeys pop menus
            #    in the app you are clicking on. F8 and F9 are what every
            #    other auto-clicker uses, and nothing else claims them.
            if key == getattr(pkeyboard.Key, "f8", None):
                self._opt_contaminated = True
                try:
                    self.call_from_thread(self._toggle_start_stop)
                except Exception:
                    pass
                return None
            if key == getattr(pkeyboard.Key, "f9", None):
                self._opt_contaminated = True
                try:
                    self.call_from_thread(self._do_pause)
                except Exception:
                    pass
                return None

            # ── AltGr fires a synthetic Ctrl first on Windows ─────
            #    The phantom Ctrl would otherwise count as "another key
            #    while Alt is held" and cancel a legitimate Alt tap.
            if self._is_altgr(key):
                self._opt_contaminated = False

            # ── Windows/Linux: track L and R Alt separately so we can
            #    detect "both Alts down at once" → pause/resume ─────
            side = self._opt_side(key)
            if side == 'l':
                self._alt_l_down = True
            elif side == 'r':
                self._alt_r_down = True
            if self._alt_l_down and self._alt_r_down and not self._both_alt_fired:
                self._both_alt_fired = True
                self._opt_contaminated = True
                try:
                    self.call_from_thread(self._do_pause)
                except Exception:
                    pass
                return None

            # ── Single Option/Alt down (any side) ─────────────────
            if self._is_opt(key):
                if not self._opt_down:
                    self._opt_down = True
                    self._opt_press_time = time.monotonic()
                    self._opt_contaminated = False
                return None

            # ── Any other key while Opt held → contaminate ────────
            if self._opt_down:
                self._opt_contaminated = True

        except Exception:
            pass
        return None

    def _on_global_release(self, key) -> None:
        try:
            # Track Alt side release for Windows/Linux both-alt detection
            side = self._opt_side(key)
            if side == 'l':
                self._alt_l_down = False
            elif side == 'r':
                self._alt_r_down = False
            if not (self._alt_l_down or self._alt_r_down):
                self._both_alt_fired = False

            if self._is_opt(key):
                held = time.monotonic() - self._opt_press_time
                was_clean = (
                    self._opt_down
                    and not self._opt_contaminated
                    and not self._both_alt_fired
                    and held < 0.6
                )
                self._opt_down = False
                self._opt_contaminated = False
                # While PAUSED, a single Option does nothing — only both
                # Options together can resume. This prevents an accidental
                # single tap from starting/stopping while paused.
                if was_clean and not self.paused.is_set():
                    try:
                        self.call_from_thread(self._toggle_start_stop)
                    except Exception:
                        pass
        except Exception:
            pass

    def _handle_esc_press(self) -> Optional[bool]:
        """Esc only has ONE job: 3 taps within ESC_WINDOW_S = start quit countdown.
        Taps 1 and 2 are silently counted — no bar, no pause, nothing visible."""
        try:
            now = time.monotonic()
            if self.esc_count == 0 or (now - self.esc_first_at) > ESC_WINDOW_S:
                # Start a fresh window
                self.esc_count    = 1
                self.esc_first_at = now
            else:
                self.esc_count += 1
                if self.esc_count >= 3:
                    try:
                        self.call_from_thread(self._start_quit_countdown)
                    except Exception:
                        pass
                    return False   # stop listener
        except Exception:
            pass
        return None

    def _start_quit_countdown(self) -> None:
        """Show the 2-second quit countdown bar, then actually quit."""
        self._quitting   = True
        self._quit_start = time.monotonic()
        # Use a daemon thread rather than set_timer — more reliable when
        # the event loop is stressed by rapid Esc presses
        threading.Thread(
            target=self._quit_after_delay,
            daemon=True
        ).start()

    def _quit_after_delay(self) -> None:
        time.sleep(2.1)
        if self._quitting:
            self._sync_shutdown("esc_triple_tap")

    def _dispatch_hotkey(self, name: str) -> None:
        """Kept for any internal callers."""
        if name == "start_stop":
            self._toggle_start_stop()
        elif name == "mini":
            self.action_mini()
        elif name == "quit":
            self._sync_shutdown("ui_quit")

    def _esc_pause_action(self) -> None:
        """Esc×2 within window — toggle pause if clicking, else just clear bar."""
        if self.active.is_set():
            self._do_pause()
        # Either way, clear the warning since the user already took action
        self.esc_count = 0
        self.esc_first_at = 0.0
        self._tick_esc()

    def _maybe_spawn_esc_overlay(self) -> None:
        """If esc style = fullscreen, spawn the overlay subprocess."""
        if self._read_esc_style() != ESC_STYLE_FULL:
            return
        if self._esc_overlay_proc is not None and self._esc_overlay_proc.poll() is None:
            return  # Already running
        overlay_path = ensure_esc_overlay_script()
        if overlay_path is None:
            return
        emoji = self._read_emoji()
        kwargs = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        if IS_WIN:
            kwargs["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
        try:
            self._esc_overlay_proc = subprocess.Popen(
                [gui_python(), str(overlay_path), emoji, str(ESC_WINDOW_S)],
                **kwargs)
        except Exception:
            self._esc_overlay_proc = None


# ─── Esc fullscreen overlay (self-generating, cross-platform) ────────────────

_ESC_OVERLAY_VERSION = 2
_ESC_OVERLAY_SRC = '''# esc_overlay.py — generated by autoclicker, safe to delete
# Fullscreen "about to quit" takeover. tkinter only, no third-party imports.
import sys, tkinter as tk

emoji = sys.argv[1] if len(sys.argv) > 1 else "!"
try:
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
except ValueError:
    seconds = 3.0

root = tk.Tk()
root.title("autoclicker")
root.configure(bg="#c0001a")
try:
    root.attributes("-fullscreen", True)
except Exception:
    root.geometry("%dx%d+0+0" % (root.winfo_screenwidth(),
                                 root.winfo_screenheight()))
try:
    root.attributes("-topmost", True)
except Exception:
    pass
root.overrideredirect(True)

wrap = tk.Frame(root, bg="#c0001a")
wrap.place(relx=0.5, rely=0.5, anchor="center")
tk.Label(wrap, text=emoji, font=("Helvetica", 96), bg="#c0001a",
         fg="#ffffff").pack()
tk.Label(wrap, text="RELEASE ESC TO CANCEL", font=("Helvetica", 34, "bold"),
         bg="#c0001a", fg="#ffffff").pack(pady=(18, 4))
count = tk.Label(wrap, text="", font=("Helvetica", 22), bg="#c0001a",
                 fg="#ffd0d0")
count.pack()

remaining = [seconds]

def tick():
    remaining[0] -= 0.1
    if remaining[0] <= 0:
        root.destroy()
        return
    count.config(text="%.1fs" % remaining[0])
    root.after(100, tick)

root.bind("<Escape>", lambda e: root.destroy())
root.bind("<Button-1>", lambda e: root.destroy())
root.after(100, tick)
try:
    root.focus_force()
except Exception:
    pass
root.mainloop()
'''


def gui_python() -> str:
    """Interpreter to use for GUI subprocesses.

    On Windows, pythonw.exe runs tkinter without flashing a console window;
    everywhere else the current interpreter is right.
    """
    if IS_WIN:
        cand = Path(sys.executable).with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return sys.executable


def ensure_esc_overlay_script() -> Optional[Path]:
    """Return a path to a working esc_overlay.py, writing one if needed.

    A copy sitting next to this script wins, so anyone who wants to customise
    the overlay still can. Otherwise it is generated into APP_DIR.
    """
    try:
        sibling = Path(__file__).resolve().parent / "esc_overlay.py"
        if sibling.exists():
            return sibling
    except Exception:
        pass
    try:
        import tkinter  # noqa: F401  — no point generating it without tk
    except Exception:
        return None
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        path = APP_DIR / "esc_overlay.py"
        stamp = APP_DIR / "esc_overlay.version"
        current = stamp.read_text().strip() if stamp.exists() else ""
        if not path.exists() or current != str(_ESC_OVERLAY_VERSION):
            path.write_text(_ESC_OVERLAY_SRC, encoding="utf-8")
            stamp.write_text(str(_ESC_OVERLAY_VERSION))
        return path
    except Exception:
        return None


# ─── Retry daemon spawn ──────────────────────────────────────────────────────

def pid_alive(pid: int) -> bool:
    """Is this PID still running?  Cross-platform.

    NOTE: os.kill(pid, 0) is a liveness probe on POSIX but NOT on Windows —
    there os.kill routes any non-CTRL signal to TerminateProcess(), so the
    'check' would actually kill the process it was asking about. Windows gets
    OpenProcess + GetExitCodeProcess instead.
    """
    if pid <= 0:
        return False
    if IS_WIN:
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k = ctypes.windll.kernel32
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, 0)       # POSIX: signal 0 only probes, never delivers
        return True
    except OSError:
        return False


def spawn_retry_daemon_if_needed() -> bool:
    """If there's a non-empty pending queue, spawn the retry daemon detached.
    Returns True if spawned."""
    if not PENDING_FILE.exists() or PENDING_FILE.stat().st_size == 0:
        return False
    # Check if a daemon is already running
    pid_file = APP_DIR / "retry_daemon.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if pid_alive(pid):
                return False   # already running, don't spawn another
            pid_file.unlink()
        except (OSError, ValueError):
            try: pid_file.unlink()
            except Exception: pass
    # Find retry_daemon.py next to this script
    script_dir = Path(__file__).resolve().parent
    daemon_path = script_dir / "retry_daemon.py"
    if not daemon_path.exists():
        return False
    try:
        # Detached background process — close fds, no shell
        kwargs = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            cwd=str(script_dir),
        )
        if IS_WIN:
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, str(daemon_path)], **kwargs)
        return True
    except Exception:
        return False


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    app = AutoClickerApp()
    try:
        app.run()
    finally:
        try:
            app._sync_shutdown("normal")
        except Exception:
            pass
        # Spawn background retry daemon if there are pending reports
        spawned = False
        try:
            spawned = spawn_retry_daemon_if_needed()
        except Exception:
            pass
        try:
            queued = 0
            if PENDING_FILE.exists() and PENDING_FILE.stat().st_size > 0:
                queued = sum(1 for _ in PENDING_FILE.open() if _.strip())
            if queued:
                msg = f"\n[!]  {queued} report(s) couldn't reach Discord."
                if spawned:
                    msg += "\n     retry daemon spawned in background — will retry hourly."
                else:
                    msg += "\n     they'll auto-send on next launch."
                if LAST_WEBHOOK_ERROR:
                    msg += f"\n     last error: {LAST_WEBHOOK_ERROR}"
                msg += f"\n     queue file: {PENDING_FILE}\n"
                print(msg)
            elif LAST_WEBHOOK_OK_AT:
                ago = max(0, int(time.time() - LAST_WEBHOOK_OK_AT))
                print(f"\n[OK]  Last Discord delivery: {ago}s ago.\n")
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        try:
            ident = load_identity()
            _post_webhook(build_crash_embed(tb, ident))
        except Exception:
            pass
        print(tb, file=sys.stderr)
        sys.exit(1)