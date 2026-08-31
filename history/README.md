# AutoClicker — build history

60 of the most distinct builds of **AutoClicker**, from a blank window to the
shipped terminal app (**v5.6**). Every build is a real, runnable `.py` file.

- `index.html` — the history site (Home, Builds, Slideshow, Credits).
- `versions/` — the 60 build files (`001 … 060`).
- `shots/` — a screenshot per build (used by the slideshow).
- `sheets/` — 4 contact-sheet overview grids.
- `data.js` — the build list.

tkinter builds need `pip install pynput`; Textual builds need
`pip install textual pynput requests`; the shipped build auto-installs its deps.
