import tkinter as tk
from tkinter import scrolledtext
import time
from tkinter import *
import numpy

class FusionGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("GUI User Interface")
        self.root.geometry("900x600")

        self.power_on = False    #Power State of the GUI

        # =================Top Bar=================
        self.top_frame = tk.Frame(root, bg="#000000", height=60)
        self.top_frame.pack(fill="both")

        self.title_label = tk.Label(
            self.top_frame,
            text="FIR–NIR Fusion Control Panel",
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

        #==============Middle Section=====================
        self.middle_frame = tk.Frame(root)
        self.middle_frame.pack(fill="both", expand=True)
        self.listbox = Listbox(self.middle_frame, height=1)
        self.listbox.insert(END, "Fusion View")
        self.listbox.pack()

        #Left control panel
        self.control_frame = tk.Frame(self.middle_frame, width=250, bg="#ffffff")
        self.control_frame.pack(side="left", fill="y")

        #Fake black screen
        self.screen = tk.Frame(self.middle_frame, bg="black", width=500, height=400)
        self.screen.pack(expand=True)

        self.screen_label = tk.Label(self.screen, text="Camera View", fg="lime", bg="black", font=("Arial", 20, "bold"))

        self.screen_label.pack(expand=True)

        #==========Left Bar Buttons======================
        tk.Button(self.control_frame, text="Power ON", width=20, command=self.cmd_power_on).pack(pady=0)
        tk.Button(self.control_frame, text="Power OFF", width=20, command=self.cmd_power_off).pack(pady=0)
        tk.Button(self.control_frame, text="Capture Photo", width=20, command=self.capture_photo).pack(pady=0)
        tk.Button(self.control_frame, text="Run Fusion", width=20).pack(pady=0)
        tk.Button(self.control_frame, text="Select View", width=20, command=self.select_view).pack(pady=30)


        #===========Error Console========================
        self.bottom_frame = tk.Frame(root, height=100)
        self.bottom_frame.pack(fill="x")

        self.log_console = scrolledtext.ScrolledText(
            self.bottom_frame,
            height=5,
            state="disabled"
        )
        self.log_console.pack(fill="both", padx=10, pady=5)
    

    def _update_power_ui(self):
        if self.power_on:
            self.power_status.config(text="Power: ON", fg="lime")
        else:
            self.power_status.config(text="Power: OFF", fg="red") 
         

    # Button Functions
    def cmd_power_on(self):
        if self.power_on:
            return
        self.power_on = True
        self._update_power_ui()

    def cmd_power_off(self):
        if not self.power_on:
            return
        self.power_on = False
        self._update_power_ui()

    def capture_photo(self):
        if not self.power_on:
            return
        self.power_on = False
        self._update_power_ui()

    def open_FIR_view(self):
        self.listbox.delete(0, END)
        self.listbox.insert(END, "FIR View")

    def open_NIR_view(self):
        self.listbox.delete(0, END)
        self.listbox.insert(END, "NIR View")

    def open_Fusion_view(self):
        self.listbox.delete(0, END)
        self.listbox.insert(END, "Fusion View")      

    def select_view(self):
        new_window = tk.Toplevel(self.root)
        new_window.title("New Window")
        new_window.geometry("250x150") # Set the window size
        #new_window.eval(f'tk::PlaceWindow {str(new_window)} center')
        tk.Button(new_window, text="FIR View", width=15, command=self.open_FIR_view).pack(pady=0)
        tk.Button(new_window, text="NIR View", width=15, command=self.open_NIR_view).pack(pady=0)
        tk.Button(new_window, text="Fusion View", width=15, command=self.open_Fusion_view).pack(pady=0)

        tk.Button(new_window, text="Close", command=new_window.destroy).pack(pady=15) # Add a close button

     

if __name__ == "__main__":
    root = tk.Tk()
    app = FusionGUI(root)
    root.mainloop()