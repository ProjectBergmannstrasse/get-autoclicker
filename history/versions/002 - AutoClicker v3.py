#!/usr/bin/env python3
"""AutoClicker v003 — ProjectBergmannstrasse — + subtitle"""
import tkinter as tk
import threading, time, random
try:
    from pynput.mouse import Controller, Button
    from pynput import keyboard
    _m = Controller()
except Exception:
    _m = None
    keyboard = None

clicking = False
count = 0
cps = 10.0

def loop():
    global count
    while clicking:
        if _m:
            _m.click(Button.left)
        count += 1
        try: count_lbl.config(text=f"Clicks: {count}")
        except Exception: pass
        time.sleep(1.0 / max(cps, 0.1))

def do_start():
    global clicking, cps, count
    pass

def do_stop():
    global clicking
    clicking = False
    print("stopped")

root = tk.Tk()
root.title("AutoClicker v003")
root.geometry("300x146")
root.configure(bg="#f0f0f0")
def L(text, **kw):
    kw.setdefault("bg", "#f0f0f0"); kw.setdefault("fg", "#000000")
    return tk.Label(root, text=text, **kw)
L("ProjectBergmannstrasse", font=("Arial", 11, "bold"), fg="#000080").pack(pady=6)
L("(auto clicker)", font=("Arial", 9)).pack()

root.mainloop()
