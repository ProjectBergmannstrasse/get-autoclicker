#!/usr/bin/env python3
"""AutoClicker v091 — ProjectBergmannstrasse — + FEEDBACK tab (Textual)"""
import threading, time
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.widgets import (Button, Input, ProgressBar, Static, Switch,
                             TabbedContent, TabPane)
try:
    from pynput.mouse import Controller, Button as MB
    _mouse = Controller()
except Exception:
    _mouse = None

ART = [
    "  ▄▀█ █ █ ▀█▀ █▀█    █▀▀ █   █ █▀▀ █▄▀ █▀▀ █▀█",
    "  █▀█ █▄█  █  █▄█    █▄▄ █▄▄ █ █▄▄ █ █ █▄▄ █ █",
]

class AutoClicker(App):
    CSS = """
    Screen { background: #07070d; color: #eeeeff; }
    #esc-frame { height: 0; background: #0d0005; }
    #esc-fill { height: 0; background: #c0001a; color: #ffffff; text-style: bold; content-align: center middle; width: 100%; }
    #brand { color: #5a5a7a; padding: 0 2; height: 1; }
    #discord { color: #00e676; padding: 0 2; height: 1; }
    .art-line { color: #ff1744; text-style: bold; height: 1; }
    .title { color: #00c8f0; text-style: bold; padding: 1 2 0 2; height: 2; }
    .hint { color: #5a5a7a; padding: 0 2; height: 1; }
    Input { width: 14; background: #161625; color: #00c8f0; margin: 0 2; }
    #status { padding: 1 2; text-style: bold; height: 2; }
    #count { padding: 0 2; color: #aaaacc; height: 1; }
    Button { margin: 0 1; }
    #start { color: #00e676; }
    #stop { color: #ff1744; }
    #reset, #antiafk { color: #5a5a7a; }
    #mini { color: #00c8f0; }
    .seg { padding: 0 2; height: 1; color: #6060a0; }
    .seg-on { color: #00c8f0; text-style: bold; }
    Tab { color: #5a5a7a; padding: 0 2; }
    Tab.-active { color: #00c8f0; text-style: bold; }
    TabbedContent { height: 1fr; }
    .cmd { color: #aaaacc; padding: 0 2; height: 1; }
    ProgressBar { margin: 0 2; }
    Switch { background: transparent; }
    .star { width: 4; color: #ff9100; text-style: bold; }
    #emu-record { color: #ff1744; }
    #emu-save, #emu-submit, #fb-submit { color: #00c8f0; }
    #capture { color: #00c8f0; }
    #emu-name, #fb-comment { background: #161625; width: 34; margin: 0 2; }
    .rec-play { color: #00e676; width: 10; }
    .rec-del { color: #ff1744; width: 12; }
    """

    def __init__(self):
        super().__init__()
        self.clicking = False
        self.count = 0
        self.cps = 20.0

    def compose(self) -> ComposeResult:
        with TabbedContent():
            with TabPane("MAIN", id="t-main"):
                with VerticalScroll():
                    with Container():
                        for _a in ART:
                            yield Static(_a, classes="art-line")
                    with Horizontal():
                        yield Static("projectbergmannstrasse", id="brand")
                        yield Static("● discord ok", id="discord")
                    yield Static("CLICKS PER SECOND", classes="title")
                    yield Input(value="20", id="cps")
                    yield Static("CLICK LIMIT", classes="title")
                    yield Input(value="0", id="limit")
                    yield Static("CLICK BUTTON", classes="title")
                    with Horizontal():
                        yield Static("▸ LEFT", classes="seg seg-on")
                        yield Static("RIGHT", classes="seg")
                        yield Static("MIDDLE", classes="seg")
                        yield Static("DOUBLE", classes="seg")
                    yield Static("CLICK PATTERN", classes="title")
                    with Horizontal():
                        yield Static("▸ CONSTANT", classes="seg seg-on")
                        yield Static("JITTER", classes="seg")
                        yield Static("BURST", classes="seg")
                    yield Static("POSITION", classes="title")
                    with Horizontal():
                        yield Static("▸ FOLLOW CURSOR", classes="seg seg-on")
                        yield Static("LOCK POSITION", classes="seg")
                    with Horizontal():
                        yield Static("FAILSAFE", classes="title")
                        yield Switch(value=True)
                    yield ProgressBar(total=100, show_eta=False, show_percentage=False)
                    with Horizontal():
                        yield Button("start", id="start")
                        yield Button("stop", id="stop")
                        yield Button("reset", id="reset")
                        yield Button("anti-afk", id="antiafk")
                        yield Button("mini", id="mini")
                    yield Static("[#5a5a7a bold]● idle[/]", id="status")
                    yield Static("clicks: 0", id="count")
            with TabPane("INFO", id="t-info"):
                with VerticalScroll():
                    yield Static("what each option does:", classes="title")
                    yield Static("  CPS = clicks per second", classes="cmd")
                    yield Static("CREDITS", classes="title")
                    yield Static("  ProjectBergmannstrasse v5.6", classes="cmd")
            with TabPane("HISTORY", id="t-hist"):
                with VerticalScroll():
                    yield Static("3 sessions", classes="title")
                    yield Static("  1  10:00  150 clicks  12.4s", classes="cmd")
            with TabPane("FEEDBACK", id="t-fb"):
                with VerticalScroll():
                    yield Static("how is it working?", classes="cmd")

    def on_button_pressed(self, event) -> None:
        if event.button.id == "start":
            self.start_clicking()
        elif event.button.id == "stop":
            self.stop_clicking()

    def start_clicking(self):
        if self.clicking: return
        try:
            self.cps = float(self.query_one("#cps", Input).value or "20")
        except Exception:
            self.cps = 20.0
        self.count = 0
        self.clicking = True
        try:
            self.query_one("#status", Static).update("[#00e676]● CLICKING[/]")
        except Exception: pass
        threading.Thread(target=self._loop, daemon=True).start()

    def stop_clicking(self):
        self.clicking = False
        try:
            self.query_one("#status", Static).update("[#5a5a7a]● idle[/]")
        except Exception: pass

    def _loop(self):
        while self.clicking:
            if _mouse:
                _mouse.click(MB.left)
            self.count += 1
            try:
                self.call_from_thread(
                    self.query_one("#count", Static).update, f"clicks: {self.count}")
            except Exception: pass
            time.sleep(1.0 / max(self.cps, 0.1))

if __name__ == "__main__":
    AutoClicker().run()
