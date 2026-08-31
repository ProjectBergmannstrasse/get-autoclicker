#!/usr/bin/env python3
"""AutoClicker v009 — ProjectBergmannstrasse — status goes green"""
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
    if clicking: return
    count = 0
    clicking = True
    try: status_lbl.config(text="CLICKING", fg="#00aa00")
    except Exception: pass
    threading.Thread(target=loop, daemon=True).start()

def do_stop():
    global clicking
    clicking = False
    try: status_lbl.config(text="idle", fg="gray")
    except Exception: pass

root = tk.Tk()
root.title("AutoClicker v009")
root.geometry("300x224")
root.configure(bg="#f0f0f0")
def L(text, **kw):
    kw.setdefault("bg", "#f0f0f0"); kw.setdefault("fg", "#000000")
    return tk.Label(root, text=text, **kw)
L("ProjectBergmannstrasse", font=("Arial", 11, "bold"), fg="#000080").pack(pady=6)
L("(auto clicker)", font=("Arial", 9)).pack()
tk.Button(root, text="Start", command=do_start).pack(pady=4)
tk.Button(root, text="Stop", command=do_stop).pack(pady=4)
status_lbl = L("idle", fg="gray"); status_lbl.pack(pady=4)

root.mainloop()
