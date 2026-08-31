#!/usr/bin/env python3
"""AutoClicker v040 — ProjectBergmannstrasse — + About button"""
import tkinter as tk
import threading, time
try:
    from pynput.mouse import Controller, Button
    _m = Controller()
except Exception:
    _m = None

clicking = False
count = 0
cps = 10.0
t0 = 0

def loop():
    global count
    while clicking:
        if _m: _m.click(Button.left)
        count += 1
        try: count_lbl.config(text=f"Clicks: {count}")
        except Exception: pass
        try:
            el = time.time() - t0
            elapsed_lbl.config(text=f"Time: {el:.0f}s")
        except Exception: pass
        try:
            el = time.time() - t0
            eff = count / el if el > 0 else 0
            eff_lbl.config(text=f"Eff CPS: {eff:.1f}")
        except Exception: pass
        time.sleep(1.0 / max(cps, 0.1))

def do_start():
    global clicking, cps, count, t0
    if clicking: return
    try: cps = float(cps_box.get())
    except Exception: cps = 10.0
    count = 0
    t0 = time.time()
    clicking = True
    status_lbl.config(text="CLICKING", fg="#00e676")
    threading.Thread(target=loop, daemon=True).start()

def do_stop():
    global clicking
    clicking = False
    status_lbl.config(text="idle", fg="gray")

def do_reset():
    global count
    count = 0
    count_lbl.config(text="Clicks: 0")

def show_about():
    w = tk.Toplevel(root)
    w.title("About")
    w.geometry("280x160")
    w.configure(bg="#0c0c18")
    tk.Label(w, text="AutoClicker v040", bg="#0c0c18", fg="#00c8f0",
             font=("Arial", 12, "bold")).pack(pady=10)
    tk.Label(w, text="ProjectBergmannstrasse", bg="#0c0c18", fg="#eeeeff").pack()
    tk.Label(w, text="github.com/ProjectBergmannstrasse", bg="#0c0c18",
             fg="#5a5a7a").pack(pady=8)

root = tk.Tk()
root.title("AutoClicker")
root.geometry("380x660")
root.configure(bg="#0c0c18")

def L(text, **kw):
    kw.setdefault("bg", "#0c0c18"); kw.setdefault("fg", "#eeeeff")
    return tk.Label(root, text=text, **kw)
L("ProjectBergmannstrasse", font=("Arial", 11, "bold"), fg="#00c8f0").pack(pady=6)
L("(auto clicker)", font=("Arial", 9)).pack()
L("Clicks per second:").pack()
cps_box = tk.Entry(root); cps_box.insert(0, "10"); cps_box.pack(pady=2)
L("Click limit (0 = none):").pack()
limit_box = tk.Entry(root); limit_box.insert(0, "0"); limit_box.pack(pady=2)
btn_var = tk.StringVar(value="left"); L("Button:").pack()
tk.OptionMenu(root, btn_var, "left", "right", "middle", "double").pack()
pat_var = tk.StringVar(value="constant"); L("Pattern:").pack()
tk.OptionMenu(root, pat_var, "constant", "jitter", "burst").pack()
tk.Button(root, text="Start", command=do_start, fg="#00e676").pack(pady=3)
tk.Button(root, text="Stop", command=do_stop, fg="#ff1744").pack(pady=3)
tk.Button(root, text="Reset", command=do_reset).pack(pady=3)
tk.Button(root, text="About", command=show_about).pack(pady=3)
from tkinter import ttk
pb = ttk.Progressbar(root, length=200, maximum=100); pb.pack(pady=4)
status_lbl = L("idle", fg="gray"); status_lbl.pack(pady=3)
count_lbl = L("Clicks: 0"); count_lbl.pack()
elapsed_lbl = L("Time: 0s", fg="#5a5a7a"); elapsed_lbl.pack()
eff_lbl = L("Eff CPS: 0.0", fg="#5a5a7a"); eff_lbl.pack()
sess_lbl = L("Sessions: 0", fg="#5a5a7a"); sess_lbl.pack()

root.mainloop()
