"""
Jono Queue Tracker v2 — Windows
Always-on-top OCR tracker for two screen-selected queue indicators.

Install:
  py -m pip install pillow numpy pywin32
Uses trained pixel-font templates, no Tesseract required.
"""
import re
import threading
import time
# Set DPI awareness before Tkinter starts (fixes 125% Windows scaling).
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
import tkinter as tk
from tkinter import messagebox
from PIL import ImageGrab
from jono_digits import read as read_jono_digits
try:
    import win32gui, win32ui, win32con
except ImportError:
    raise SystemExit("Install the new dependency: py -m pip install pywin32")
from PIL import Image

def capture_window(hwnd):
    """Best-effort capture of a non-minimized window using PrintWindow."""
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right-left, bottom-top
    if width < 1 or height < 1:
        raise RuntimeError("Game window has no visible size")
    window_dc = win32gui.GetWindowDC(hwnd)
    src_dc = win32ui.CreateDCFromHandle(window_dc)
    mem_dc = src_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(src_dc, width, height)
        mem_dc.SelectObject(bitmap)
        # PW_RENDERFULLCONTENT=2. Some GPU games return blank/stale frames.
        result = ctypes.windll.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), 2)
        if not result:
            raise RuntimeError("PrintWindow failed; game may not support background capture")
        data = bitmap.GetBitmapBits(True)
        image = Image.frombuffer("RGB", (width, height), data, "raw", "BGRX", 0, 1).copy()
        return image, (left, top, right, bottom)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        mem_dc.DeleteDC()
        src_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)

def get_game_windows():
    found = []
    def callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
            title = win32gui.GetWindowText(hwnd).strip()
            if title and "queue tracker" not in title.lower():
                found.append((hwnd, title))
    win32gui.EnumWindows(callback, None)
    return found


REFRESH_SECONDS = 1.5
HIGH_CONFIDENCE_COST = 15
MAX_MATCH_COST = 35
NEAR_TURN_POSITION = 10

class RegionSelector:
    def __init__(self, root, callback, title):
        self.root, self.callback = root, callback
        self.start = None
        self.rect = None
        self.win = tk.Toplevel(root)
        # Fullscreen and override-redirect conflict on Windows Tk.
        self.win.attributes("-fullscreen", True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.30)
        self.win.configure(bg="black")
        self.canvas = tk.Canvas(self.win, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(22, 22, anchor="nw",
            text=title + "\nDrag tightly around the orange numbers and slash. Press Esc to cancel.",
            fill="white", font=("Segoe UI", 16, "bold"))
        self.canvas.bind("<ButtonPress-1>", self.begin)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.finish)
        self.win.bind("<Escape>", lambda e: self.cancel())
        self.win.focus_force()

    def begin(self, e):
        self.start = (e.x_root, e.y_root)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#00ff88", width=3)

    def drag(self, e):
        if self.start and self.rect:
            self.canvas.coords(self.rect, self.start[0] - self.win.winfo_rootx(), self.start[1] - self.win.winfo_rooty(),
                               e.x_root - self.win.winfo_rootx(), e.y_root - self.win.winfo_rooty())

    def finish(self, e):
        if not self.start:
            return
        x1, y1 = self.start
        x2, y2 = e.x_root, e.y_root
        if abs(x2-x1) < 6 or abs(y2-y1) < 6:
            return
        region = (min(x1,x2), min(y1,y2), max(x1,x2), max(y1,y2))
        self.win.destroy()
        self.callback(region)

    def cancel(self):
        self.win.destroy()
        self.root.deiconify()

class App:
    def __init__(self, root):
        self.root = root
        root.title("Jono Queue Tracker")
        root.geometry("385x345+40+40")
        root.minsize(330, 295)
        root.attributes("-topmost", True)
        root.configure(bg="#211b2b")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.game_hwnd = None
        self.window_choices = []
        self.regions = {"Jono": None, "Meat Grinder": None}
        self.data = {n: {"pos": None, "count": None, "diag": "Area not selected"} for n in self.regions}
        self.scanning = False
        self.pending = {n: None for n in self.regions}
        self.pending_position = {n: None for n in self.regions}
        self.scan_generation = 0
        self.running = True
        self.compact = False
        self._ui()
        self._compact_ui()
        self.root.after(800, self.scan_loop)
        self.root.after(200, self.find_windows)

    def _ui(self):
        self.normal_view = tk.Frame(self.root, bg="#211b2b")
        self.normal_view.pack(fill="both", expand=True)
        head = tk.Frame(self.normal_view, bg="#211b2b")
        head.pack(fill="x", padx=10, pady=(8,4))
        tk.Label(head, text="JONO QUEUE TRACKER", bg="#211b2b", fg="white",
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        tk.Button(head, text="Compact", command=self.toggle_compact, bg="#403448",
                  fg="white", relief="flat").pack(side="right")

        picker = tk.Frame(self.normal_view, bg="#211b2b")
        picker.pack(fill="x", padx=8, pady=(0,3))
        self.window_var = tk.StringVar(value="Choose Jono window…")
        from tkinter import ttk
        self.window_menu = ttk.Combobox(picker, textvariable=self.window_var, state="readonly", width=28)
        self.window_menu.pack(side="left", expand=True, fill="x", padx=(0,3))
        self.window_menu.bind("<<ComboboxSelected>>", self.choose_window)
        tk.Button(picker, text="Find windows", command=self.find_windows,
                  bg="#594468", fg="white", relief="flat").pack(side="right")
        self.card_widgets = {}
        for name in self.regions:
            card = tk.Frame(self.normal_view, bg="#302638", padx=9, pady=5)
            card.pack(fill="x", padx=8, pady=3)
            row = tk.Frame(card, bg="#302638")
            row.pack(fill="x")
            tk.Label(row, text=name.upper(), bg="#302638", fg="#d9cbe2",
                     font=("Segoe UI", 8, "bold")).pack(side="left")
            pos = tk.Label(row, text="Position: —", bg="#302638", fg="#ffad32",
                           font=("Segoe UI", 12, "bold"))
            pos.pack(side="right")
            count = tk.Label(card, text="People in queue: —", bg="#302638", fg="#eee5f3",
                             font=("Segoe UI", 9))
            count.pack(anchor="w", pady=(1,0))
            diag = tk.Label(card, text="Area not selected", bg="#302638", fg="#b9a9c4",
                            font=("Segoe UI", 8), anchor="w", justify="left", wraplength=325)
            diag.pack(fill="x", anchor="w")
            self.card_widgets[name] = {"card": card, "pos": pos, "count": count, "diag": diag}

        buttons = tk.Frame(self.normal_view, bg="#211b2b")
        buttons.pack(fill="x", padx=8, pady=(4,2))
        tk.Button(buttons, text="Set Jono area", command=lambda: self.select("Jono"),
                  bg="#594468", fg="white", relief="flat").pack(side="left", expand=True, fill="x", padx=(0,3))
        tk.Button(buttons, text="Set Meat Grinder area", command=lambda: self.select("Meat Grinder"),
                  bg="#594468", fg="white", relief="flat").pack(side="left", expand=True, fill="x", padx=(3,0))
        foot = tk.Frame(self.normal_view, bg="#211b2b")
        foot.pack(fill="x", padx=10, pady=(3,7))
        self.status = tk.Label(foot, text="Ready — select each queue area", bg="#211b2b",
                               fg="#b9a9c4", font=("Segoe UI", 8))
        self.status.pack(side="left")
        tk.Button(foot, text="Refresh now", command=self.refresh, bg="#403448",
                  fg="white", relief="flat").pack(side="right")

    def _compact_ui(self):
        """A separate borderless overlay; the normal settings view is untouched."""
        self.compact_view = tk.Frame(self.root, bg="#211b2b", bd=0)
        header = tk.Frame(self.compact_view, bg="#211b2b", height=34)
        header.pack(fill="x", padx=8, pady=(5, 2))
        title = tk.Label(header, text="QUEUE TRACKER", bg="#211b2b",
                         fg="#eee4f2", font=("Segoe UI", 10, "bold"))
        title.pack(side="left", padx=5)
        restore = tk.Button(header, text="Expand", command=self.toggle_compact,
                            bg="#44354d", fg="white", relief="flat",
                            font=("Segoe UI", 9), cursor="hand2")
        restore.pack(side="right")
        self.compact_labels = {}
        for name in self.regions:
            row = tk.Frame(self.compact_view, bg="#302638", height=52)
            row.pack(fill="x", padx=8, pady=4)
            row.pack_propagate(False)
            label = tk.Label(row, text=name.upper(), bg="#302638",
                             fg="#e8ddea", font=("Segoe UI", 10, "bold"))
            label.pack(side="left", padx=12)
            value = tk.Label(row, text="#— / —", bg="#302638",
                             fg="#ffad32", font=("Segoe UI", 14, "bold"))
            value.pack(side="right", padx=12)
            self.compact_labels[name] = value
            # Drag by the empty row, label, or number, too.
            for widget in (row, label, value):
                widget.bind("<ButtonPress-1>", self._start_drag)
                widget.bind("<B1-Motion>", self._drag)
        for widget in (header, title):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
        self._drag_origin = None

    def _start_drag(self, event):
        self._drag_origin = (event.x_root, event.y_root,
                             self.root.winfo_x(), self.root.winfo_y())

    def _drag(self, event):
        if not self._drag_origin:
            return
        sx, sy, wx, wy = self._drag_origin
        self.root.geometry(f"+{wx + event.x_root - sx}+{wy + event.y_root - sy}")

    def find_windows(self):
        self.window_choices = get_game_windows()
        labels = [f"{title[:38]} (id {hwnd})" for hwnd, title in self.window_choices]
        self.window_menu["values"] = labels
        if not labels:
            self.window_var.set("No windows found")
            return
        matches = [i for i, (_, title) in enumerate(self.window_choices)
                   if "jono" in title.lower() and "tracker" not in title.lower()]
        if matches:
            self.window_menu.current(matches[0])
            self.choose_window()
        else:
            self.window_var.set("Choose Jono window…")

    def choose_window(self, _event=None):
        index = self.window_menu.current()
        if index >= 0:
            self.game_hwnd = self.window_choices[index][0]
            self.status.config(text="Connected to " + self.window_choices[index][1][:22])

    def select(self, name):
        if not self.game_hwnd:
            messagebox.showwarning("Choose game", "Choose Jono from the window dropdown first.")
            return
        self.root.withdraw()
        self.root.after(250, lambda: RegionSelector(self.root,
            lambda region: self.set_region(name, region), f"Select {name} queue text"))

    def set_region(self, name, region):
        self.regions[name] = region
        self.scan_generation += 1
        self.pending[name] = None
        self.pending_position[name] = None
        self.data[name]["pos"] = None
        self.data[name]["count"] = None
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.data[name]["diag"] = f"Area saved: {region}. Testing pixel recognizer…"
        self.update_ui()
        self.refresh()

    def refresh(self):
        if self.scanning:
            return
        self.scanning = True
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        messages = []
        generation = self.scan_generation
        try:
            for name, region in self.regions.items():
                if not region:
                    continue
                try:
                    if not self.game_hwnd or not win32gui.IsWindow(self.game_hwnd):
                        raise RuntimeError("Select the Jono window again")
                    full_image, bounds = capture_window(self.game_hwnd)
                    left, top, right, bottom = bounds
                    # Selected rectangles are in physical desktop pixels.
                    crop = (region[0]-left, region[1]-top, region[2]-left, region[3]-top)
                    if crop[0] < 0 or crop[1] < 0 or crop[2] > full_image.width or crop[3] > full_image.height:
                        raise RuntimeError("Queue area is outside game window. Reselect it.")
                    img = full_image.crop(crop)
                    # Debug image: verify Jono is actually captured when covered.
                    from pathlib import Path
                    debug_dir = Path.home() / "Pictures"
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    img.save(debug_dir / ("jono_debug_" + name.lower().replace(" ", "_") + ".png"))
                    rows = read_jono_digits(img)
                    if generation != self.scan_generation:
                        return
                    candidates = [candidate for row in rows for candidate in row]
                    if candidates:
                        current = self.data[name]
                        previous_pos = current["pos"]
                        previous_count = current["count"]
                        # Use the strongest pixel evidence. Version 6.6 gave a
                        # previous value an artificial advantage over a better
                        # optical match, allowing persistent incorrect readings.
                        # Never let the history change what the image says.
                        cost, position, count, slash_x = min(
                            candidates, key=lambda item: item[0]
                        )
                        if cost > MAX_MATCH_COST:
                            current["diag"] = f"Uncertain pixel match ({cost:.1f}); kept previous values"
                        else:
                            # Initial reads are immediate. Any changed value must
                            # appear in TWO consecutive scans to prevent a single
                            # wrongly recognized digit from flashing on the overlay.
                            if previous_pos is None:
                                current["pos"] = position
                                self.pending_position[name] = None
                                pos_message = f"Position #{position} (initial)"
                            elif position == previous_pos:
                                self.pending_position[name] = None
                                pos_message = f"Position #{position} unchanged"
                            else:
                                # A large sudden drop can be a 7 -> 1 or 8 -> 3
                                # misread. Require one extra identical capture.
                                required = 3 if position < previous_pos - 8 else 2
                                pending = self.pending_position[name]
                                seen = pending[1] + 1 if pending and pending[0] == position else 1
                                if seen >= required:
                                    current["pos"] = position
                                    self.pending_position[name] = None
                                    pos_message = f"Position #{position} confirmed"
                                else:
                                    self.pending_position[name] = (position, seen)
                                    pos_message = f"Checking position #{position} ({seen}/{required}; showing #{previous_pos})"

                            if previous_count is None:
                                current["count"] = count
                                self.pending[name] = None
                                count_message = f"total {count} (initial)"
                            elif count == previous_count:
                                self.pending[name] = None
                                count_message = f"total {count}"
                            elif self.pending[name] == count:
                                current["count"] = count
                                self.pending[name] = None
                                count_message = f"total {count} confirmed"
                            else:
                                self.pending[name] = count
                                count_message = f"checking total {count} (showing {previous_count})"
                            current["diag"] = f"{pos_message}; {count_message} (score {cost:.1f})"
                    else:
                        self.data[name]["diag"] = "No matching Jono pixel-font line found; kept previous values"
                except Exception as e:
                    self.data[name]["diag"] = f"CAPTURE ERROR: {type(e).__name__}: {e}"
        finally:
            self.root.after(0, self.update_ui)
            self.scanning = False

    def update_ui(self):
        for name, w in self.card_widgets.items():
            d = self.data[name]
            if d["pos"] is None:
                w["pos"].config(text="Position: —", fg="#ffad32")
                w["count"].config(text="People in queue: —")
            else:
                color = "#4ade80" if d["pos"] <= NEAR_TURN_POSITION else "#ffad32"
                w["pos"].config(text=f"Position: #{d['pos']}", fg=color)
                w["count"].config(text=f"People in queue: {d['count']}")
            w["diag"].config(text=d["diag"])
            # Same live data drives both views; compact uses the game's #position / total format.
            pos_text = f"#{d['pos']}" if d["pos"] is not None else "#—"
            count_text = str(d["count"]) if d["count"] is not None else "—"
            self.compact_labels[name].config(text=f"{pos_text} / {count_text}")
        self.status.config(text=time.strftime("Updated %H:%M:%S"))

    def toggle_compact(self):
        if not self.compact:
            self.compact = True
            self.normal_view.pack_forget()
            # Save the full-size window's placement before removing its decorations.
            self.normal_geometry = self.root.geometry()
            self.root.minsize(1, 1)
            self.root.overrideredirect(True)
            self.root.geometry("350x180")
            self.compact_view.pack(fill="both", expand=True)
            self.root.attributes("-topmost", True)
            self.root.lift()
        else:
            self.compact = False
            self.compact_view.pack_forget()
            self.root.overrideredirect(False)
            self.root.geometry(self.normal_geometry)
            self.root.minsize(330, 295)
            self.normal_view.pack(fill="both", expand=True)
            self.root.attributes("-topmost", True)
            self.root.lift()

    def scan_loop(self):
        if self.running:
            self.refresh()
            self.root.after(int(REFRESH_SECONDS * 1000), self.scan_loop)

    def close(self):
        self.running = False
        self.root.destroy()

def main():
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    except Exception as exc:
        messagebox.showerror("Jono Queue Tracker", str(exc))

if __name__ == "__main__":
    main()
