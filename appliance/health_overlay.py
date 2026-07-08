#!/usr/bin/env python3
"""health_overlay.py — small always-on-top health light for the appliance.

Reads the JSON written by health_check.sh and shows a colored dot + a couple of
status lines in the top-right corner, floating above the Chromium kiosk. Green =
OK, amber = warning, red = failure, grey = no data yet.

Launched from the kiosk X session (see appliance/xinitrc). Depends on
python3-tk. Reads HEALTH_FILE from the environment (same default as
health_check.sh).
"""

import json
import os
import tkinter as tk

HEALTH_FILE = os.environ.get("HEALTH_FILE", "/var/lib/marksys/health.json")
REFRESH_MS = 5000

COLORS = {
    "OK": "#2e7d32",
    "WARN": "#f9a825",
    "FAIL": "#c62828",
    "UNKNOWN": "#616161",
}


def _age_text(age_min):
    if age_min is None or age_min < 0:
        return "未有"
    if age_min < 90:
        return f"{age_min}分鐘前"
    return f"{age_min // 60}小時前"


class Overlay:
    def __init__(self, root):
        self.root = root
        root.overrideredirect(True)          # no title bar; float freely
        root.attributes("-topmost", True)
        root.configure(bg="#111111")

        w, h = 250, 96
        sw = root.winfo_screenwidth()
        root.geometry(f"{w}x{h}+{sw - w - 16}+16")

        self.canvas = tk.Canvas(root, width=24, height=24, bg="#111111",
                                highlightthickness=0)
        self.canvas.place(x=14, y=14)
        self.dot = self.canvas.create_oval(2, 2, 22, 22,
                                           fill=COLORS["UNKNOWN"], outline="")

        self.title_lbl = tk.Label(root, text="Health Check", fg="white", bg="#111111",
                                  font=("Sans", 11, "bold"))
        self.title_lbl.place(x=48, y=12)

        self.lines = tk.Label(root, text="讀取中…", fg="#dddddd", bg="#111111",
                              justify="left", anchor="w", font=("Sans", 9))
        self.lines.place(x=14, y=46)

        # Drag-to-move: overrideredirect() removed the title bar, so let the
        # operator reposition the light by dragging anywhere on it. Must bind on
        # every child too — clicks land on them and never reach the root window.
        self._drag_off = (0, 0)
        for widget in (root, self.canvas, self.title_lbl, self.lines):
            widget.configure(cursor="fleur")
            widget.bind("<Button-1>", self._start_move)
            widget.bind("<B1-Motion>", self._on_move)

        self.refresh()

    def _start_move(self, event):
        self._drag_off = (event.x_root - self.root.winfo_x(),
                          event.y_root - self.root.winfo_y())

    def _on_move(self, event):
        x = event.x_root - self._drag_off[0]
        y = event.y_root - self._drag_off[1]
        self.root.geometry(f"+{x}+{y}")

    def refresh(self):
        data = None
        try:
            with open(HEALTH_FILE) as f:
                data = json.load(f)
        except Exception:
            pass

        if data:
            overall = data.get("overall", "UNKNOWN")
            self.canvas.itemconfig(self.dot,
                                   fill=COLORS.get(overall, COLORS["UNKNOWN"]))
            app = data.get("app", {})
            backup = data.get("backup", {})
            disk = data.get("disk", {})
            self.lines.config(text=(
                f"App: {app.get('status', '?')}    Disk: {disk.get('free_pct', '?')}% Left\n"
                f"Backup: {backup.get('status', '?')} ({_age_text(backup.get('age_min'))})"
            ))
        else:
            self.canvas.itemconfig(self.dot, fill=COLORS["UNKNOWN"])
            self.lines.config(text="等健康資料…")

        # keep above the kiosk window
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(REFRESH_MS, self.refresh)


def main():
    root = tk.Tk()
    root.title("marksys-health")
    Overlay(root)
    root.mainloop()


if __name__ == "__main__":
    main()
