# GUI4.py — Live camera view (RTSP or webcam) inside Tkinter
# Based on your GUI3 structure, but replaces dummy images with a live stream.

import tkinter as tk
from tkinter import scrolledtext
from PIL import Image, ImageTk
import time
import cv2


class FusionGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("GUI User Interface")
        self.root.geometry("900x600")

        self.power_on = False
        self.current_view = "Fusion"  # FIR / NIR / Fusion

        # ---- Live stream state ----
        self.cap = None
        self.streaming = False
        self._tk_img = None  # keep reference so Tkinter doesn't garbage collect

        # ---- Stream sources ----
        # Replace <PI_IP> with your Raspberry Pi IP when ready.
        # If you're testing on a laptop with a webcam, set NIR/FIR to 0.
        self.stream_urls = {
            "NIR": "rtsp://192.168.137.37:8554/gs",
            "FIR": "rtsp://192.168.137.37:8554/thermal",
            "Fusion": "rtsp://192.168.137.37:8554/fusion",
            # Example webcam testing:
            # "NIR": 0,
            # "FIR": 0,
        }

        # ================= Top Bar =================
        self.top_frame = tk.Frame(root, bg="#000000", height=60)
        self.top_frame.pack(fill="x")

        self.title_label = tk.Label(
            self.top_frame,
            text="FIR-NIR Fusion Control Panel",
            fg="white",
            bg="#000000",
            font=("Arial", 16, "bold")
        )
        self.title_label.pack(side="left", padx=20, pady=15)

        self.power_status = tk.Label(
            self.top_frame,
            text="Power: OFF",
            fg="red",
            bg="#000000",
            font=("Arial", 12, "bold")
        )
        self.power_status.pack(side="right", padx=20)

        # ============== Middle Section ==============
        self.middle_frame = tk.Frame(root)
        self.middle_frame.pack(fill="both", expand=True)

        # Left control panel
        self.control_frame = tk.Frame(self.middle_frame, width=250, bg="#ffffff")
        self.control_frame.pack(side="left", fill="y")

        # Center screen
        self.screen = tk.Frame(self.middle_frame, bg="black")
        self.screen.pack(side="left", fill="both", expand=True)
        self.screen.pack_propagate(False)

        self.view_label = tk.Label(
            self.screen,
            text="View: Fusion",
            fg="lime",
            bg="black",
            font=("Arial", 14, "bold")
        )
        self.view_label.pack(anchor="nw", padx=10, pady=10)

        self.image_label = tk.Label(
            self.screen,
            bg="black",
            fg="white",
            text="(powered off)"
        )
        self.image_label.pack(fill="both", expand=True)

        # ========== Left Bar Buttons ==========
        tk.Button(self.control_frame, text="Power ON", width=20, command=self.cmd_power_on).pack(pady=2)
        tk.Button(self.control_frame, text="Power OFF", width=20, command=self.cmd_power_off).pack(pady=2)
        tk.Button(self.control_frame, text="Capture Photo", width=20, command=self.capture_photo).pack(pady=2)
        tk.Button(self.control_frame, text="Run Fusion", width=20, command=self.run_fusion).pack(pady=2)
        tk.Button(self.control_frame, text="Select View", width=20, command=self.select_view).pack(pady=20)

        # =========== Bottom Log Console ===========
        self.bottom_frame = tk.Frame(root, height=100)
        self.bottom_frame.pack(fill="x")

        self.log_console = scrolledtext.ScrolledText(self.bottom_frame, height=5, state="disabled")
        self.log_console.pack(fill="both", padx=10, pady=5)

        self._refresh_screen_text()

    # ---------- Logging ----------
    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_console.configure(state="normal")
        self.log_console.insert("end", f"[{ts}] {msg}\n")
        self.log_console.see("end")
        self.log_console.configure(state="disabled")

    # ---------- UI helpers ----------
    def _update_power_ui(self):
        if self.power_on:
            self.power_status.config(text="Power: ON", fg="lime")
        else:
            self.power_status.config(text="Power: OFF", fg="red")

    def _refresh_screen_text(self):
        self.view_label.config(text=f"View: {self.current_view}")

    def _ensure_power(self):
        if not self.power_on:
            self.log("Power is OFF — turn ON first.")
            return False
        return True

    # ---------- Streaming ----------
    def _start_stream(self, source):
        self._stop_stream()

        self.cap = cv2.VideoCapture(source)
        if not self.cap.isOpened():
            self.log(f"Failed to open stream: {source}")
            self.cap = None
            self.image_label.config(image="", text="(stream failed)", fg="white", bg="black")
            return

        self.streaming = True
        self.log(f"Streaming started: {source}")
        self.image_label.config(text="")  # clear placeholder text
        self._stream_loop()

    def _stop_stream(self):
        self.streaming = False
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def _stream_loop(self):
        if not self.streaming or self.cap is None:
            return

        ok, frame = self.cap.read()
        if ok and frame is not None:
            # OpenCV frame is BGR -> convert to RGB for PIL/Tk
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            # Resize to fit available space
            w = max(1, self.screen.winfo_width() - 20)
            h = max(1, self.screen.winfo_height() - 60)

            # If window isn't fully laid out yet, avoid tiny resize
            if w > 10 and h > 10:
                frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)

            img = Image.fromarray(frame)
            self._tk_img = ImageTk.PhotoImage(img)
            self.image_label.config(image=self._tk_img, text="")

        # Schedule next frame
        self.root.after(33, self._stream_loop)  # ~30 FPS

    # ---------- Button callbacks ----------
    def cmd_power_on(self):
        if self.power_on:
            return
        self.power_on = True
        self._update_power_ui()
        self.log("Power turned ON.")

        # If current view is FIR/NIR, start stream immediately
        if self.current_view in ("FIR", "NIR"):
            src = self.stream_urls.get(self.current_view, 0)
            self._start_stream(src)
        else:
            self.image_label.config(image="", text="(Fusion view — no live stream yet)", fg="white", bg="black")

    def cmd_power_off(self):
        if not self.power_on:
            return
        self.power_on = False
        self._update_power_ui()
        self._stop_stream()
        self.image_label.config(image="", text="(powered off)", fg="white", bg="black")
        self.log("Power turned OFF.")

    def open_view(self, view):
        self.current_view = view
        self._refresh_screen_text()
        self.log(f"Switched view to {view}.")

        if not self.power_on:
            self.image_label.config(image="", text="(powered off)", fg="white", bg="black")
            return
        
        if view in ("FIR", "NIR", "Fusion"):
            src = self.stream_urls.get(view, 0)
            self._start_stream(src)
        else:
            self._stop_stream()
            self.image_label.config(image="", text="(Fusion view — no live stream yet)", fg="white", bg="black")

    def select_view(self):
        if not self._ensure_power():
            return
        new_window = tk.Toplevel(self.root)
        new_window.title("Select View")
        new_window.geometry("250x150")

        tk.Button(new_window, text="FIR View", width=15,
                  command=lambda: (self.open_view("FIR"), new_window.destroy())).pack(pady=2)
        tk.Button(new_window, text="NIR View", width=15,
                  command=lambda: (self.open_view("NIR"), new_window.destroy())).pack(pady=2)
        tk.Button(new_window, text="Fusion View", width=15,
                  command=lambda: (self.open_view("Fusion"), new_window.destroy())).pack(pady=2)

        tk.Button(new_window, text="Close", command=new_window.destroy).pack(pady=10)

    def capture_photo(self):
        if not self._ensure_power():
            return
        if self.current_view not in ("FIR", "NIR"):
            self.log("Capture Photo is for FIR or NIR views. Select FIR or NIR.")
            return
        if self.cap is None:
            self.log("No live stream active — cannot capture.")
            return

        ok, frame = self.cap.read()
        if not ok or frame is None:
            self.log("Capture failed — no frame received.")
            return

        # Save as PNG in current directory
        filename = f"{self.current_view.lower()}_snapshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
        cv2.imwrite(filename, frame)
        self.log(f"Saved snapshot: {filename}")

    def run_fusion(self):
        if not self._ensure_power():
            return
        # Placeholder: You’ll replace this with your actual fusion pipeline
        self.log("Run Fusion pressed (not wired yet).")
        self.open_view("Fusion")


if __name__ == "__main__":
    root = tk.Tk()
    app = FusionGUI(root)
    root.mainloop()