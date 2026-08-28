#!/usr/bin/env python3
"""
Camoufox Control Center (CCC)
==============================
A graphical control panel for managing multiple Camoufox browser profiles,
each with its own fingerprint configuration.

Main features:
  - Multi-profile management (create, duplicate, delete, rename)
  - Per-profile fingerprint configuration (OS, locale, timezone, geolocation,
    screen, window size, user-agent, proxy, etc.)
  - Automatic launch script generation for each profile
  - Concurrent profile launching (each profile runs in its own process)
  - Setup wizard for venv, camoufox package and browser binary
  - Dark-themed custom file explorer for directory selection
  - Live log viewer with color-coded tags

Architecture:
  - Single-file Tkinter application (no external UI frameworks)
  - Profiles are stored as directories under `./profiles/<name>/`
  - Each profile contains:
      * camoufox-config.py   — generated launch script
      * fingerprint.json     — fingerprint settings (source of truth)
      * browser data files   — persistent Firefox profile data
  - Virtual environment is created under `~/venvs/<name>/`
  - Camoufox binary is cached under `~/.cache/camoufox/`
  
"""

# ============================================================
# Standard library imports
# ============================================================
import ast
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
import json
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont
from urllib.parse import urlparse

# ============================================================
# Application constants
# ============================================================
APP_NAME = "Camoufox Control Center"
APP_VERSION = "0.3.0"

# Base directory is the folder containing this script
BASE_DIR = Path(__file__).resolve().parent

# Default virtual environment location (user can rename via the UI)
DEFAULT_VENV = Path.home() / "venvs" / "camoufox-latest"

# All profiles live here, one subfolder per profile
DEFAULT_PROFILES = BASE_DIR / "profiles"

# Camoufox stores its downloaded browser binary here
CACHE_DIR = Path.home() / ".cache" / "camoufox"

# ============================================================
# Dark color palette
# ============================================================
# Centralized palette so the whole UI stays visually consistent.
# Tweak these values to re-skin the entire application.
COLORS = {
    # Main window
    "bg": "#080b12",
    "bg2": "#0c111a",
    # Panels / cards
    "panel": "#101722",
    "panel2": "#151e2b",
    "panel3": "#1b2737",
    "border": "#263449",
    # Text
    "text": "#e8f0f7",
    "muted": "#8190a5",
    # Accents
    "accent": "#32e6ff",
    "accent2": "#1677ff",
    # Semantic colors
    "green": "#39f5a0",
    "yellow": "#ffd166",
    "red": "#ff5577",
    "purple": "#b56cff",
    "magenta": "#ff4fd8",
    # Terminal / log viewer
    "terminal": "#05070b",
    "terminal_text": "#cfe5f2",
    # Scrollbars
    "scrollbar": "#111a27",
    "scrollbar_hover": "#263449",
    "scrollbar_pressed": "#1677ff",
    # Custom file explorer
    "explorer_bg": "#0a0e17",
    "explorer_panel": "#131b2a",
    "explorer_panel2": "#1a2438",
    "explorer_selected": "#005577",
    "explorer_hover": "#1a2a3a",
}

# ============================================================
# Typography
# ============================================================
FONT = ("TkDefaultFont", 10)
MONO_FONT = ("TkFixedFont", 10)
HEADER_FONT = ("TkDefaultFont", 11, "bold")


# ============================================================
# Custom Dark File Explorer
# ============================================================
# A Toplevel window that replaces the native file dialog.
# Native dialogs on Linux don't respect dark themes, so we roll our own
# to keep the whole app visually consistent.
class DarkFileExplorer(tk.Toplevel):
    """
    Dark-themed file explorer dialog.

    Features:
      - Directory navigation with back/forward/up/home/refresh
      - Show/hide hidden files
      - Single- or multi-select (Shift/Ctrl)
      - Double-click to enter directories
      - Right-click context menu (open, open in file manager, copy path)
      - Address bar with manual path entry
      - Callback-based selection (works for both files and directories)
    """

    def __init__(self, parent, title="Select Directory", initialdir=None,
                 select_callback=None, show_hidden=False, select_files=False):
        super().__init__(parent)
        self.parent = parent
        self.title(title)
        self.geometry("850x580")
        self.minsize(600, 400)
        self.configure(bg=COLORS["explorer_bg"])

        # Navigation state
        self.current_path = Path(initialdir or os.path.expanduser("~")).resolve()
        self.select_callback = select_callback
        self.show_hidden = show_hidden
        self.select_files = select_files
        self.selected_items = []
        self.last_click_index = -1
        self.history = []
        self.history_index = -1

        self._build_ui()
        self._load_directory()

        # Close on Escape / window manager close
        self.bind("<Escape>", lambda e: self._on_close())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.focus_force()
        self.transient(parent)
        # Delay grab_set so the window is fully drawn first
        self.after(50, self._set_grab)

    def _set_grab(self):
        """Set modal grab after the window is fully drawn."""
        try:
            if self.winfo_exists():
                self.grab_set()
        except tk.TclError:
            self.after(50, lambda: self.grab_set() if self.winfo_exists() else None)

    def _build_ui(self):
        """Assemble all widgets of the explorer."""
        # --- Header: location bar + nav buttons ---
        header = tk.Frame(self, bg=COLORS["explorer_panel"])
        header.pack(fill="x", padx=10, pady=(10, 5))

        path_frame = tk.Frame(header, bg=COLORS["explorer_panel"])
        path_frame.pack(fill="x", pady=(0, 5))
        tk.Label(path_frame, text="📍 Location:", bg=COLORS["explorer_panel"],
                 fg=COLORS["muted"], font=HEADER_FONT).pack(side="left", padx=(5, 10))
        self.path_entry = tk.Entry(path_frame, bg=COLORS["explorer_panel2"],
                                   fg=COLORS["text"], insertbackground=COLORS["accent"],
                                   relief="flat", borderwidth=1, highlightcolor=COLORS["accent"],
                                   highlightbackground=COLORS["border"], highlightthickness=1, font=FONT)
        self.path_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.path_entry.bind("<Return>", lambda e: self._navigate_to_path())

        btn_frame = tk.Frame(header, bg=COLORS["explorer_panel"])
        btn_frame.pack(fill="x", pady=(0, 0))

        nav_frame = tk.Frame(btn_frame, bg=COLORS["explorer_panel"])
        nav_frame.pack(side="left")
        self._create_button(nav_frame, "◀ Back", self._go_back)
        self._create_button(nav_frame, "▶ Forward", self._go_forward)
        self._create_button(nav_frame, "⬆ Up", self._go_up)
        self._create_button(nav_frame, "↻ Refresh", self._refresh)
        self._create_button(nav_frame, "🏠 Home", self._go_home)

        opt_frame = tk.Frame(btn_frame, bg=COLORS["explorer_panel"])
        opt_frame.pack(side="right")
        self.hidden_var = tk.BooleanVar(value=self.show_hidden)
        self.hidden_cb = tk.Checkbutton(opt_frame, text="Show Hidden", variable=self.hidden_var,
                                        bg=COLORS["explorer_panel"], fg=COLORS["muted"],
                                        selectcolor=COLORS["explorer_panel"],
                                        activebackground=COLORS["explorer_panel2"],
                                        activeforeground=COLORS["text"], relief="flat",
                                        cursor="hand2", command=self._toggle_hidden)
        self.hidden_cb.pack(side="left", padx=5)
        self._create_button(opt_frame, "Select", self._select_current, variant="accent")
        self._create_button(opt_frame, "Cancel", self._on_close, variant="danger")

        # --- Main area: file list ---
        main_frame = tk.Frame(self, bg=COLORS["explorer_bg"])
        main_frame.pack(fill="both", expand=True, padx=10, pady=(5, 10))

        header_frame = tk.Frame(main_frame, bg=COLORS["explorer_panel2"])
        header_frame.pack(fill="x")
        headers = [("Name", 4), ("Size", 1), ("Modified", 2)]
        for i, (text, weight) in enumerate(headers):
            label = tk.Label(header_frame, text=text, bg=COLORS["explorer_panel2"],
                             fg=COLORS["muted"], font=HEADER_FONT)
            if i == 0:
                label.pack(side="left", fill="x", expand=True, padx=(10, 0), pady=5)
            else:
                label.pack(side="left", padx=10, pady=5)

        list_frame = tk.Frame(main_frame, bg=COLORS["explorer_bg"])
        list_frame.pack(fill="both", expand=True)
        scrollbar = tk.Scrollbar(list_frame, orient="vertical", bg=COLORS["scrollbar"],
                                 troughcolor=COLORS["explorer_bg"], activebackground=COLORS["scrollbar_hover"],
                                 relief="flat", borderwidth=0)
        self.file_listbox = tk.Listbox(list_frame, bg=COLORS["explorer_panel"], fg=COLORS["text"],
                                       selectbackground=COLORS["explorer_selected"],
                                       selectforeground=COLORS["text"], selectmode="extended",
                                       relief="flat", borderwidth=0, highlightthickness=0,
                                       font=FONT, yscrollcommand=scrollbar.set)
        scrollbar.configure(command=self.file_listbox.yview)
        self.file_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Event bindings for the listbox
        self.file_listbox.bind("<Double-Button-1>", self._on_double_click)
        self.file_listbox.bind("<Button-1>", self._on_click)
        self.file_listbox.bind("<Control-Button-1>", self._on_ctrl_click)
        self.file_listbox.bind("<Shift-Button-1>", self._on_shift_click)
        self.file_listbox.bind("<Key-Up>", self._on_key)
        self.file_listbox.bind("<Key-Down>", self._on_key)
        self.file_listbox.bind("<Return>", self._on_double_click)

        # Right-click context menu
        self.context_menu = tk.Menu(self, tearoff=0, bg=COLORS["explorer_panel"],
                                    fg=COLORS["text"], activebackground=COLORS["explorer_panel2"])
        self.context_menu.add_command(label="Open", command=self._open_selected)
        self.context_menu.add_command(label="Open in File Manager", command=self._open_in_file_manager)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy Path", command=self._copy_path)
        if self.select_files:
            self.context_menu.add_command(label="Select File", command=self._select_current)
        else:
            self.context_menu.add_command(label="Select Directory", command=self._select_current)
        self.file_listbox.bind("<Button-3>", self._show_context_menu)

        # --- Status bar ---
        status = tk.Frame(self, bg=COLORS["explorer_panel"])
        status.pack(fill="x", padx=10, pady=(0, 5))
        self.status_label = tk.Label(status, text="Ready", bg=COLORS["explorer_panel"],
                                     fg=COLORS["muted"], font=FONT)
        self.status_label.pack(side="left")
        self.item_count_label = tk.Label(status, text="0 items", bg=COLORS["explorer_panel"],
                                         fg=COLORS["muted"], font=FONT)
        self.item_count_label.pack(side="right")

    def _create_button(self, parent, text, command, variant="secondary"):
        """Create a themed button with hover effect."""
        colors = {
            "secondary": {"bg": COLORS["explorer_panel2"], "hover": COLORS["explorer_panel2"], "fg": COLORS["text"]},
            "accent": {"bg": COLORS["accent2"], "hover": COLORS["accent"], "fg": "white"},
            "danger": {"bg": "#440011", "hover": COLORS["red"], "fg": "white"},
        }
        btn = tk.Button(parent, text=text, bg=colors[variant]["bg"], fg=colors[variant]["fg"],
                        activebackground=colors[variant]["hover"], activeforeground=colors[variant]["fg"],
                        relief="flat", borderwidth=0, padx=12, pady=4, cursor="hand2", font=FONT, command=command)
        btn.pack(side="left", padx=2)
        def on_enter(e): btn.config(bg=colors[variant]["hover"])
        def on_leave(e): btn.config(bg=colors[variant]["bg"])
        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        return btn

    def _load_directory(self, path=None):
        """Reload the file list from the given (or current) directory."""
        if path:
            self.current_path = Path(path).resolve()
            if not self.current_path.exists():
                messagebox.showerror("Error", f"Directory does not exist:\n{self.current_path}")
                return
            if not self.current_path.is_dir():
                messagebox.showerror("Error", f"Not a directory:\n{self.current_path}")
                return

        self.path_entry.delete(0, tk.END)
        self.path_entry.insert(0, str(self.current_path))
        self.file_listbox.delete(0, tk.END)
        self.selected_items = []
        self.last_click_index = -1

        try:
            items = []
            for item in self.current_path.iterdir():
                if not self.show_hidden and item.name.startswith('.'):
                    continue
                items.append(item)
            # Directories first, then alphabetical
            items.sort(key=lambda x: (not x.is_dir(), x.name.lower()))

            for item in items:
                if item.is_dir(): name = f"📁 {item.name}"
                elif item.is_file(): name = f"📄 {item.name}"
                else: name = item.name
                self.file_listbox.insert(tk.END, name)

            total = len(items)
            self.item_count_label.config(text=f"{total} item{'s' if total != 1 else ''}")
        except PermissionError:
            messagebox.showerror("Permission Denied", f"Cannot access:\n{self.current_path}")
        except Exception as e:
            messagebox.showerror("Error", f"Error loading directory:\n{str(e)}")

    def _get_item_at_index(self, index):
        """Return the Path object for the item at the given listbox index."""
        if index < 0 or index >= self.file_listbox.size(): return None
        display_name = self.file_listbox.get(index)
        # Strip the emoji prefix we added for visual cues
        if display_name.startswith("📁 ") or display_name.startswith("📄 "):
            name = display_name[2:]
        else:
            name = display_name
        return self.current_path / name

    def _on_click(self, event):
        """Single click: select one item."""
        index = self.file_listbox.nearest(event.y)
        if index >= 0:
            self.last_click_index = index
            self.file_listbox.selection_clear(0, tk.END)
            self.file_listbox.selection_set(index)
            self.file_listbox.activate(index)
            item = self._get_item_at_index(index)
            if item: self.status_label.config(text=f"Selected: {item.name}")

    def _on_ctrl_click(self, event):
        """Ctrl+click: toggle selection of a single item."""
        index = self.file_listbox.nearest(event.y)
        if index >= 0:
            self.last_click_index = index
            if self.file_listbox.selection_includes(index):
                self.file_listbox.selection_clear(index)
            else:
                self.file_listbox.selection_set(index)

    def _on_shift_click(self, event):
        """Shift+click: range-select from last clicked to current."""
        index = self.file_listbox.nearest(event.y)
        if index >= 0 and self.last_click_index >= 0:
            self.file_listbox.selection_clear(0, tk.END)
            start = min(self.last_click_index, index)
            end = max(self.last_click_index, index)
            self.file_listbox.selection_set(start, end)

    def _on_key(self, event):
        """Keyboard navigation updates the status bar."""
        current = self.file_listbox.curselection()
        if current:
            index = current[0]
            item = self._get_item_at_index(index)
            if item: self.status_label.config(text=f"Selected: {item.name}")

    def _on_double_click(self, event):
        """Double-click: enter directory, or select file."""
        current = self.file_listbox.curselection()
        if not current: return
        index = current[0]
        item = self._get_item_at_index(index)
        if not item: return
        if item.is_dir():
            self._add_to_history()
            self._load_directory(item)
        elif self.select_files:
            if self.select_callback: self.select_callback(str(item))
            self._safe_close()
        else:
            try:
                size = item.stat().st_size
                self.status_label.config(text=f"File: {item.name} ({self._format_size(size)})")
            except:
                self.status_label.config(text=f"File: {item.name}")

    def _add_to_history(self):
        """Push current path onto the navigation history stack."""
        if self.history and self.history[-1] == str(self.current_path): return
        if self.history_index < len(self.history) - 1:
            self.history = self.history[:self.history_index + 1]
        self.history.append(str(self.current_path))
        self.history_index = len(self.history) - 1

    def _go_back(self):
        if self.history_index > 0:
            self.history_index -= 1
            self._load_directory(self.history[self.history_index])

    def _go_forward(self):
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self._load_directory(self.history[self.history_index])

    def _go_up(self):
        parent = self.current_path.parent
        if parent != self.current_path:
            self._add_to_history()
            self._load_directory(parent)

    def _refresh(self): self._load_directory(self.current_path)

    def _go_home(self):
        self._add_to_history()
        self._load_directory(Path.home())

    def _toggle_hidden(self):
        self.show_hidden = self.hidden_var.get()
        self._refresh()

    def _format_size(self, size):
        """Format a byte count into a human-readable string."""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}" if unit != 'B' else f"{int(size)} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"

    def _safe_close(self):
        """Release the modal grab and destroy the window."""
        try: self.grab_release()
        except: pass
        self.destroy()

    def _select_current(self):
        """Invoke the selection callback and close the dialog."""
        if self.select_files:
            current = self.file_listbox.curselection()
            if current:
                index = current[0]
                item = self._get_item_at_index(index)
                if item and item.is_file():
                    if self.select_callback: self.select_callback(str(item))
                    self._safe_close()
                    return
                elif item and item.is_dir():
                    self._add_to_history()
                    self._load_directory(item)
                    return
            path = str(self.current_path)
        else:
            path = str(self.current_path)

        if self.select_callback: self.select_callback(path)
        self._safe_close()

    def _open_selected(self):
        """Open the selected item with the system default handler."""
        current = self.file_listbox.curselection()
        if current:
            index = current[0]
            item = self._get_item_at_index(index)
            if item:
                if item.is_dir():
                    self._add_to_history()
                    self._load_directory(item)
                else:
                    try: subprocess.Popen(["xdg-open", str(item)])
                    except: pass

    def _open_in_file_manager(self):
        """Open the current directory in the system file manager."""
        try:
            for cmd in ["xdg-open", "nautilus", "dolphin", "thunar", "nemo"]:
                try:
                    subprocess.Popen([cmd, str(self.current_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
                except: continue
        except:
            messagebox.showerror("Error", "Cannot open file manager")

    def _copy_path(self):
        """Copy the current directory path to the clipboard."""
        self.clipboard_clear()
        self.clipboard_append(str(self.current_path))
        self.status_label.config(text=f"📋 Path copied: {self.current_path}")

    def _show_context_menu(self, event):
        self.context_menu.post(event.x_root, event.y_root)

    def _navigate_to_path(self):
        """Navigate to the path typed into the address bar."""
        path = Path(self.path_entry.get()).expanduser().resolve()
        if path.exists() and path.is_dir():
            self._add_to_history()
            self._load_directory(path)
        else:
            messagebox.showerror("Error", "Invalid directory path")

    def _on_close(self): self._safe_close()


# ============================================================
# Directory Chooser (wrapper around DarkFileExplorer)
# ============================================================
class DirectoryChooser:
    """
    Static helper that opens DarkFileExplorer and blocks until the user
    picks a file or directory. Returns the chosen path (or None).
    """
    @staticmethod
    def ask_directory(parent, title="Select Directory", initialdir=None):
        result = None
        def callback(path):
            nonlocal result
            result = path
        explorer = DarkFileExplorer(parent, title=title, initialdir=initialdir,
                                    select_callback=callback, show_hidden=False, select_files=False)
        parent.wait_window(explorer)
        return result

    @staticmethod
    def ask_open_file(parent, title="Select File", initialdir=None):
        result = None
        def callback(path):
            nonlocal result
            result = path
        explorer = DarkFileExplorer(parent, title=title, initialdir=initialdir,
                                    select_callback=callback, show_hidden=False, select_files=True)
        parent.wait_window(explorer)
        return result


# ============================================================
# Rounded Button (Canvas-drawn, theme-aware)
# ============================================================
class RoundedButton(tk.Canvas):
    """
    A Canvas-based button with rounded corners and a purple border.

    We use Canvas instead of ttk.Button because:
      - Native buttons don't support rounded corners
      - We want full control over the hover color
      - We want the button to stretch with `pack(fill="x")` while keeping
        the rounded rectangle in sync (see _on_configure)
    """
    _VARIANTS = {
        None: {"fill": COLORS["panel2"], "hover": COLORS["panel3"], "fg": COLORS["text"]},
        "Accent.TButton": {"fill": COLORS["accent2"], "hover": "#2a8eff", "fg": "#ffffff"},
        "Danger.TButton": {"fill": "#351725", "hover": "#c93a55", "fg": "#ffffff"},
    }

    def __init__(self, parent, text="", command=None, style=None,
                 width=None, padx=16, pady=8, radius=10, **kwargs):
        try:
            bg = parent.cget("background")
        except Exception:
            bg = COLORS["bg"]

        variant = self._VARIANTS.get(style, self._VARIANTS[None])
        self._fill = variant["fill"]
        self._hover_fill = variant["hover"]
        self._fg = variant["fg"]
        self._border = COLORS["accent"]
        self._command = command
        self._radius = radius
        self._font = tkfont.Font(font=FONT)
        self._text = text  # saved so we can redraw on resize
        self._padx = padx
        self._pady = pady

        # Compute initial size based on text metrics
        text_w = self._font.measure(text)
        text_h = self._font.metrics("linespace")
        min_w = width * self._font.measure("0") if width else 0
        canvas_w = max(text_w + padx * 2, min_w + padx * 2, 2 * radius + 8)
        canvas_h = text_h + pady * 2

        super().__init__(parent, width=canvas_w, height=canvas_h,
                         bg=bg, highlightthickness=0, bd=0)
        self._rect = self._draw_round_rect(1, 1, canvas_w - 1, canvas_h - 1,
                                           radius, fill=self._fill,
                                           outline=self._border, width=1.4)
        self._label = self.create_text(canvas_w / 2, canvas_h / 2,
                                       text=text, fill=self._fg, font=self._font)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        # Redraw the rounded rectangle whenever the Canvas is resized
        self.bind("<Configure>", self._on_configure)
        self.configure(cursor="hand2")

    def _draw_round_rect(self, x1, y1, x2, y2, r, **kwargs):
        """Draw a rounded rectangle using a smoothed polygon."""
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kwargs)

    def _on_configure(self, event):
        """Redraw the rounded rectangle and label after a resize."""
        w = event.width
        h = event.height
        if w < 2 * self._radius + 8 or h < 2 * self._radius + 8:
            return
        self.delete("all")
        self._rect = self._draw_round_rect(1, 1, w - 1, h - 1, self._radius,
                                           fill=self._fill, outline=self._border, width=1.4)
        self._label = self.create_text(w / 2, h / 2, text=self._text,
                                       fill=self._fg, font=self._font)

    def _on_enter(self, _event):
        if self._rect:
            self.itemconfig(self._rect, fill=self._hover_fill)

    def _on_leave(self, _event):
        if self._rect:
            self.itemconfig(self._rect, fill=self._fill)

    def _on_click(self, _event):
        if self._command:
            self._command()


# ============================================================
# Sidebar Button
# ============================================================
class SidebarButton(tk.Canvas):
    """Navigation button for the left sidebar, with a selected state."""

    def __init__(self, parent, text="", command=None, **kwargs):
        try: bg = parent.cget("background")
        except Exception: bg = COLORS["panel"]

        self._command = command
        self._selected = False
        self._radius = 10
        self._font = tkfont.Font(font=FONT)
        text_h = self._font.metrics("linespace")
        canvas_h = text_h + 22

        super().__init__(parent, height=canvas_h, bg=bg, highlightthickness=0, bd=0)
        self._fill = COLORS["panel"]
        self._hover_fill = COLORS["panel2"]
        self._selected_fill = COLORS["panel2"]
        self._fg = COLORS["muted"]
        self._selected_fg = COLORS["text"]
        self._border = COLORS["purple"]
        self._rect = None
        self._label = None
        self._text = text

        self.bind("<Configure>", self._redraw)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.configure(cursor="hand2")

    def _draw_round_rect(self, x1, y1, x2, y2, r, **kwargs):
        points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                  x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.create_polygon(points, smooth=True, **kwargs)

    def _redraw(self, event=None):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 10: return

        fill = self._selected_fill if self._selected else self._fill
        fg = self._selected_fg if self._selected else self._fg
        outline = self._border if self._selected else COLORS["border"]

        self._rect = self._draw_round_rect(2, 2, w - 2, h - 2, self._radius,
                                           fill=fill, outline=outline, width=1.4)
        self._label = self.create_text(18, h / 2, text=self._text, fill=fg, font=self._font, anchor="w")

    def set_selected(self, selected: bool):
        self._selected = selected
        self._redraw()

    def _on_enter(self, _event):
        if not self._selected and self._rect: self.itemconfig(self._rect, fill=self._hover_fill)

    def _on_leave(self, _event):
        if not self._selected and self._rect: self.itemconfig(self._rect, fill=self._fill)

    def _on_click(self, _event):
        if self._command: self._command()


# ============================================================
# Fingerprint presets
# ============================================================
# Dropdown options for the fingerprint editor.
# Adding a new option here automatically makes it available in the UI.
OS_OPTIONS = ["windows", "macos", "linux"]

LOCALE_OPTIONS = [
    "en-US", "en-GB", "de-DE", "fr-FR", "es-ES", "it-IT", "pt-BR", "pt-PT",
    "nl-NL", "pl-PL", "tr-TR", "ru-RU", "uk-UA", "ja-JP", "ko-KR", "zh-CN"
]

TIMEZONE_OPTIONS = [
    "UTC",
    "Europe/Istanbul", "Europe/London", "Europe/Berlin", "Europe/Paris",
    "Europe/Moscow", "Europe/Warsaw", "Europe/Kyiv", "Europe/Athens",
    "Europe/Vienna", "Europe/Bucharest", "Europe/Madrid", "Europe/Rome",
    "Europe/Amsterdam", "Europe/Stockholm", "Europe/Helsinki", "Europe/Prague",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Toronto", "America/Vancouver", "America/Sao_Paulo",
    "Asia/Dubai", "Asia/Tbilisi", "Asia/Tokyo", "Asia/Seoul",
    "Asia/Singapore", "Asia/Hong_Kong", "Asia/Bangkok", "Asia/Kolkata",
    "Africa/Cairo", "Australia/Sydney", "Pacific/Auckland",
]

# Locale -> (primary language, list of fallback languages)
LOCALE_TO_LANGUAGE = {
    "en-US": ("en-US", ["en-US", "en"]), "en-GB": ("en-GB", ["en-GB", "en"]),
    "de-DE": ("de-DE", ["de-DE", "de", "en"]), "fr-FR": ("fr-FR", ["fr-FR", "fr", "en"]),
    "es-ES": ("es-ES", ["es-ES", "es", "en"]), "it-IT": ("it-IT", ["it-IT", "it", "en"]),
    "pt-BR": ("pt-BR", ["pt-BR", "pt", "en"]), "pt-PT": ("pt-PT", ["pt-PT", "pt", "en"]),
    "nl-NL": ("nl-NL", ["nl-NL", "nl", "en"]), "pl-PL": ("pl-PL", ["pl-PL", "pl", "en"]),
    "tr-TR": ("tr-TR", ["tr-TR", "tr", "en"]), "ru-RU": ("ru-RU", ["ru-RU", "ru", "en"]),
    "uk-UA": ("uk-UA", ["uk-UA", "uk", "en"]), "ja-JP": ("ja-JP", ["ja-JP", "ja", "en"]),
    "ko-KR": ("ko-KR", ["ko-KR", "ko", "en"]), "zh-CN": ("zh-CN", ["zh-CN", "zh", "en"]),
}

# Timezone -> (latitude, longitude) for the "Detect coordinates" button
TIMEZONE_COORDINATES = {
    "Europe/Istanbul": (41.0082, 28.9784),
    "Europe/London": (51.5074, -0.1278),
    "Europe/Berlin": (52.5200, 13.4050),
    "Europe/Paris": (48.8566, 2.3522),
    "Europe/Moscow": (55.7558, 37.6173),
    "Europe/Warsaw": (52.2297, 21.0122),
    "Europe/Kyiv": (50.4501, 30.5234),
    "Europe/Athens": (37.9838, 23.7275),
    "Europe/Vienna": (48.2082, 16.3738),
    "Europe/Bucharest": (44.4268, 26.1025),
    "Europe/Madrid": (40.4168, -3.7038),
    "Europe/Rome": (41.9028, 12.4964),
    "Europe/Amsterdam": (52.3676, 4.9041),
    "Europe/Stockholm": (59.3293, 18.0686),
    "Europe/Helsinki": (60.1699, 24.9384),
    "Europe/Prague": (50.0755, 14.4378),
    "America/New_York": (40.7128, -74.0060),
    "America/Chicago": (41.8781, -87.6298),
    "America/Denver": (39.7392, -104.9903),
    "America/Los_Angeles": (34.0522, -118.2437),
    "America/Toronto": (43.6532, -79.3832),
    "America/Vancouver": (49.2827, -123.1207),
    "America/Sao_Paulo": (-23.5505, -46.6333),
    "Asia/Dubai": (25.2048, 55.2708),
    "Asia/Tbilisi": (41.7151, 44.8271),
    "Asia/Tokyo": (35.6762, 139.6503),
    "Asia/Seoul": (37.5665, 126.9780),
    "Asia/Singapore": (1.3521, 103.8198),
    "Asia/Hong_Kong": (22.3193, 114.1694),
    "Asia/Bangkok": (13.7563, 100.5018),
    "Asia/Kolkata": (22.5726, 88.3639),
    "Africa/Cairo": (30.0444, 31.2357),
    "Australia/Sydney": (-33.8688, 151.2093),
    "Pacific/Auckland": (-36.8485, 174.7633),
}


# ============================================================
# Utility functions
# ============================================================
def expand_path(value: str) -> Path:
    """Expand ~ and env vars, then resolve to an absolute path."""
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def read_text(path: Path) -> str:
    """Read a text file, returning empty string on any error."""
    try: return path.read_text(encoding="utf-8")
    except Exception: return ""


def write_text(path: Path, text: str):
    """Write text to a file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def find_binary():
    """
    Locate the camoufox-bin executable inside the user cache directory.
    Returns a Path or None.
    """
    if not CACHE_DIR.exists(): return None
    try:
        for root, dirs, files in os.walk(CACHE_DIR):
            if "camoufox-bin" in files:
                return Path(root) / "camoufox-bin"
    except Exception: pass
    return None


def version_from_cache(channel="stable"):
    """Return the newest Camoufox version string found in the cache."""
    if not CACHE_DIR.exists(): return None
    channel_dir = CACHE_DIR / "browsers" / ("official" if channel == "stable" else "prerelease")
    if not channel_dir.exists(): return None
    pattern = re.compile(r"(\d+\.\d+\.\d+(?:-[a-zA-Z0-9._-]+)?)")
    versions = []
    try:
        for item in channel_dir.iterdir():
            if not item.is_dir(): continue
            match = pattern.search(item.name)
            if match: versions.append(match.group(1))
    except Exception: return None
    if versions: return sorted(versions)[-1]
    return None


# ============================================================
# Process Manager
# ============================================================
# Wraps subprocess.Popen so we can stream stdout into the log viewer
# and terminate the whole process group (including grandchildren) cleanly.
class ProcessManager:
    def __init__(self, log_callback):
        self.process = None
        self.log_callback = log_callback
        self.thread = None

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, command, cwd=None, env=None):
        """Spawn a new process in its own session and start the log reader."""
        if self.running: raise RuntimeError("A process is already running.")
        self.log_callback("[EXEC] " + " ".join(str(x) for x in command))
        merged_env = os.environ.copy()
        if env: merged_env.update(env)

        self.process = subprocess.Popen(
            [str(x) for x in command], cwd=str(cwd) if cwd else None, env=merged_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, bufsize=1, universal_newlines=True, start_new_session=True,
        )
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()
        return self.process

    def _reader(self):
        """Stream stdout line-by-line into the log callback."""
        process = self.process
        if not process or not process.stdout: return
        try:
            for line in iter(process.stdout.readline, ""):
                if line: self.log_callback(line.rstrip("\n"))
        except Exception as exc:
            self.log_callback(f"[ERROR] {exc}")
        finally:
            try: process.stdout.close()
            except Exception: pass
            code = process.wait()
            self.log_callback(f"[PROCESS] Process exited with code {code}")

    def stop(self):
        """Terminate the process group, escalating to SIGKILL if needed."""
        if not self.running: return
        process = self.process
        try: os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except Exception:
            try: process.terminate()
            except Exception: pass

        for _ in range(30):
            if process.poll() is not None: break
            time.sleep(0.1)

        if process.poll() is None:
            try: os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                try: process.kill()
                except Exception: pass


# ============================================================
# Fingerprint Model
# ============================================================
# Holds all the fingerprint-related settings for a single profile.
# Can be loaded from an existing camoufox-config.py (AST-parsed) or
# from a fingerprint.json (the source of truth on disk).
class FingerprintConfig:
    DEFAULTS = {
        "os": "windows", "locale": "en-US", "timezone": "Europe/Istanbul",
        "latitude": 41.0082, "longitude": 28.9784,
        "navigator.language": "en-US", "navigator.languages": ["en-US", "en"],
        "headers.Accept-Language": "en-US,en;q=0.9",
        "headers.User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "startup_url": "https://duckduckgo.com",
        "screen.width": 1700, "screen.height": 970,
        "screen.availWidth": 1700, "screen.availHeight": 940,
        "screen.colorDepth": 24, "screen.pixelDepth": 24,
        "window.width": 1696, "window.height": 1026,
        "navigator.hardwareConcurrency": 8,
        "proxy.server": "", "proxy.username": "", "proxy.password": "",
    }

    def __init__(self):
        self.values = dict(self.DEFAULTS)
        self.profile_dir = ""

    def load(self, path: Path):
        """Parse an existing camoufox-config.py and extract fingerprint values."""
        source = read_text(path)
        if not source: return
        try: tree = ast.parse(source)
        except SyntaxError: return

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "PROFILE_DIR":
                        value = self._literal(node.value)
                        if isinstance(value, str): self.profile_dir = value

            if isinstance(node, ast.Call):
                if not self._is_name(node.func, "launch_options"): continue
                for keyword in node.keywords:
                    if keyword.arg == "os":
                        value = self._literal(keyword.value)
                        if value is not None: self.values["os"] = value
                    elif keyword.arg == "locale":
                        value = self._literal(keyword.value)
                        if value is not None: self.values["locale"] = value
                    elif keyword.arg == "window":
                        value = self._literal(keyword.value)
                        if isinstance(value, (list, tuple)) and len(value) >= 2:
                            self.values["window.width"] = value[0]
                            self.values["window.height"] = value[1]
                    elif keyword.arg == "config":
                        config = self._literal(keyword.value)
                        if isinstance(config, dict):
                            for key, value in config.items():
                                if key in self.values: self.values[key] = value
                                elif key == "proxy":
                                    if isinstance(value, dict):
                                        self.values["proxy.server"] = value.get("server", "")
                                        self.values["proxy.username"] = value.get("username", "")
                                        self.values["proxy.password"] = value.get("password", "")

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if self._is_name(node.func, "goto") and node.args:
                    value = self._literal(node.args[0])
                    if isinstance(value, str): self.values["startup_url"] = value

    @staticmethod
    def _is_name(node, name):
        return isinstance(node, ast.Name) and node.id == name

    @staticmethod
    def _literal(node):
        try: return ast.literal_eval(node)
        except Exception: return None


# ============================================================
# Profile Model
# ============================================================
# Represents a single browser profile on disk.
# Each profile owns a directory containing:
#   - fingerprint.json   (source of truth for fingerprint settings)
#   - camoufox-config.py (generated launch script)
#   - browser data       (persistent Firefox profile)
class Profile:
    def __init__(self, name, base_dir):
        self.name = name
        self.dir = base_dir / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.dir / "camoufox-config.py"
        self.fp_path = self.dir / "fingerprint.json"
        self.fingerprint = FingerprintConfig()
        self.fingerprint.profile_dir = str(self.dir)
        self._load_fp()

    def _load_fp(self):
        """Load fingerprint from JSON if present, else fall back to config.py."""
        if self.fp_path.exists():
            try:
                data = json.loads(self.fp_path.read_text(encoding="utf-8"))
                if "values" in data:
                    self.fingerprint.values.update(data["values"])
                if "profile_dir" in data:
                    self.fingerprint.profile_dir = data["profile_dir"]
            except Exception:
                pass
        elif self.config_path.exists():
            self.fingerprint.load(self.config_path)

    def save_fp(self):
        """Persist the fingerprint to JSON and regenerate the launch script."""
        data = {
            "values": self.fingerprint.values,
            "profile_dir": self.fingerprint.profile_dir
        }
        self.fp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        binary = find_binary()
        binary_path = str(binary) if binary else "camoufox-bin"
        generate_config(self.fingerprint, self.config_path, binary_path)


# ============================================================
# Config Generator
# ============================================================
# Produces a self-contained camoufox-config.py from a FingerprintConfig.
# The generated script can be run directly with the venv Python.
def generate_config(fingerprint, output_path, binary_path):
    values = fingerprint.values
    profile_dir = fingerprint.profile_dir or str(Path.home() / "camoufox-profile")
    os_name = str(values["os"])
    locale = str(values["locale"])
    timezone = str(values["timezone"])
    latitude = float(values["latitude"])
    longitude = float(values["longitude"])
    language = str(values["navigator.language"])
    languages = values["navigator.languages"]
    if isinstance(languages, str):
        languages = [x.strip() for x in languages.split(",") if x.strip()]
    accept_language = str(values["headers.Accept-Language"])
    user_agent = str(values.get("headers.User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"))
    startup_url = str(values.get("startup_url", "https://duckduckgo.com"))
    parsed = urlparse(startup_url)
    if parsed.scheme not in ("http", "https"): startup_url = "https://duckduckgo.com"

    screen_width = int(values["screen.width"])
    screen_height = int(values["screen.height"])
    avail_width = int(values["screen.availWidth"])
    avail_height = int(values["screen.availHeight"])
    color_depth = int(values["screen.colorDepth"])
    pixel_depth = int(values["screen.pixelDepth"])
    window_width = int(values["window.width"])
    window_height = int(values["window.height"])
    hw_concurrency = int(values.get("navigator.hardwareConcurrency", 8))

    proxy_server = str(values.get("proxy.server", "")).strip()
    proxy_username = str(values.get("proxy.username", "")).strip()
    proxy_password = str(values.get("proxy.password", "")).strip()

    if proxy_server:
        proxy_lines = [f'            "server": {proxy_server!r}']
        if proxy_username and proxy_password:
            proxy_lines.append(f'            "username": {proxy_username!r}')
            proxy_lines.append(f'            "password": {proxy_password!r}')
        proxy_dict_str = "{\n" + ",\n".join(proxy_lines) + "\n        }"
        proxy_kwarg = f",\n        proxy={proxy_dict_str}"
        geoip_kwarg = ",\n        geoip=True"
    else:
        proxy_kwarg = ""
        geoip_kwarg = ""

    config = f'''#!/usr/bin/env python3
"""
Camoufox Launch Configuration
Generated by Camoufox Control Center {APP_VERSION}.
"""
import os
import time
from camoufox.utils import launch_options
from playwright.sync_api import sync_playwright

PROFILE_DIR = {profile_dir!r}

def get_launch_config():
    options = launch_options(
        headless=False,
        os={os_name!r},
        locale={locale!r},
        window=({window_width}, {window_height}),
        config={{
            "timezone": {timezone!r},
            "geolocation:latitude": {latitude!r},
            "geolocation:longitude": {longitude!r},
            "navigator.language": {language!r},
            "navigator.languages": {languages!r},
            "headers.Accept-Language": {accept_language!r},
            "headers.User-Agent": {user_agent!r},
            "screen.width": {screen_width},
            "screen.height": {screen_height},
            "screen.availWidth": {avail_width},
            "screen.availHeight": {avail_height},
            "screen.colorDepth": {color_depth},
            "screen.pixelDepth": {pixel_depth},
            "navigator.hardwareConcurrency": {hw_concurrency},
        }},
        i_know_what_im_doing=True{geoip_kwarg}{proxy_kwarg}
    )
    options.pop("executable_path", None)
    options.pop("headless", None)
    options.pop("viewport", None)
    custom_args = ["-new-instance", "-no-remote"]
    if "args" in options:
        custom_args = options.pop("args") + custom_args
    return options, custom_args

def run_with_persistent_profile(p, options, custom_args):
    print(f"Using persistent profile: {{PROFILE_DIR}}")
    context_options = {{"viewport": {{"width": {screen_width}, "height": {screen_height}}}}}
    context = p.firefox.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        executable_path={binary_path!r},
        headless=False,
        args=custom_args,
        **options,
        **context_options,
    )
    return context

def run_ephemeral(p, options, custom_args):
    print("Profile not found. Launching in ephemeral mode...")
    browser = p.firefox.launch(
        executable_path={binary_path!r},
        headless=False,
        args=custom_args,
        **options,
    )
    context = browser.new_context(
        viewport={{ "width": {screen_width}, "height": {screen_height} }},
        locale={locale!r},
        timezone_id={timezone!r},
        geolocation={{ "latitude": {latitude!r}, "longitude": {longitude!r} }},
        permissions=["geolocation"],
    )
    return context

def main():
    options, custom_args = get_launch_config()
    with sync_playwright() as p:
        if os.path.exists(PROFILE_DIR):
            context = run_with_persistent_profile(p, options, custom_args)
        else:
            context = run_ephemeral(p, options, custom_args)

        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto({startup_url!r}, wait_until="commit", timeout=60000)
            print(f"Page loaded: {{page.title()}}")
        except Exception as exc:
            print(f"Failed to load page: {{exc}}")

        try:
            while True:
                pages = context.pages
                if not pages: break
                pages[0].evaluate("1")
                time.sleep(1)
        except Exception:
            pass

        try: context.close()
        except Exception: pass

if __name__ == "__main__":
    main()
'''
    write_text(output_path, config)
    try: output_path.chmod(0o755)
    except Exception: pass


# ============================================================
# Main GUI
# ============================================================
class CamoufoxGUI(tk.Tk):
    """
    Root window of the application.
    Owns all state (profiles, running processes, UI variables) and
    dispatches between the three main pages: Dashboard, Setup, Profiles.
    """

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1280x820")
        self.minsize(1080, 700)
        self.configure(bg=COLORS["bg"])

        # Log plumbing: worker threads push into log_queue,
        # the main loop drains it into the Text widget every 100 ms.
        self.log_queue = queue.Queue()
        self.log_buffer = []
        self.current_page = None

        # Multi-profile state
        self.profiles = {}                  # name -> Profile
        self.active_profile_name = None
        self.process_managers = {}          # name -> ProcessManager (one per running profile)

        # Tk variables bound to UI widgets
        self.profile_var = tk.StringVar(value="Default")
        self.channel_var = tk.StringVar(value="stable")
        self.venv_name_var = tk.StringVar(value=DEFAULT_VENV.name)
        self.venv_name_var.trace_add("write", lambda *_: self.apply_venv_name())
        self.status_var = tk.StringVar(value="Ready")
        self.version_var = tk.StringVar(value="Not detected")
        self.fp_vars = {}                   # fingerprint key -> StringVar (rebuilt per profile)

        # Derived paths
        self.venv_dir = DEFAULT_VENV

        self.build_styles()
        self.build_layout()

        # Periodic tasks
        self.after(100, self.process_logs)
        self.after(1000, self.refresh_status)

        self.refresh_profiles()
        self.show_page("dashboard")

    # --------------------------------------------------------
    # Styles
    # --------------------------------------------------------
    def build_styles(self):
        """Configure ttk styles to match the dark palette."""
        style = ttk.Style(self)
        try: style.theme_use("clam")
        except Exception: pass

        style.configure(".", background=COLORS["bg"], foreground=COLORS["text"], font=FONT)
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TButton", background=COLORS["panel2"], foreground=COLORS["text"],
                        borderwidth=0, padding=(12, 8))
        style.map("TButton", background=[("active", COLORS["border"]), ("pressed", COLORS["accent2"])])
        style.configure("Accent.TButton", background=COLORS["accent2"], foreground="white", padding=(14, 9))
        style.map("Accent.TButton", background=[("active", COLORS["accent"]), ("pressed", COLORS["accent2"])])
        style.configure("Danger.TButton", background=COLORS["red"], foreground="white")

        style.configure("TEntry", fieldbackground=COLORS["panel2"], foreground=COLORS["text"],
                        insertcolor=COLORS["text"], borderwidth=1, padding=7,
                        bordercolor=COLORS["border"], lightcolor=COLORS["border"], darkcolor=COLORS["border"])
        style.map("TEntry", bordercolor=[("focus", COLORS["accent"]), ("!focus", COLORS["border"])],
                  lightcolor=[("focus", COLORS["accent"]), ("!focus", COLORS["border"])],
                  darkcolor=[("focus", COLORS["accent"]), ("!focus", COLORS["border"])])

        style.configure("TCombobox", fieldbackground=COLORS["panel2"], background=COLORS["panel2"],
                        foreground=COLORS["text"], arrowcolor=COLORS["text"], borderwidth=1,
                        padding=6, bordercolor=COLORS["border"], lightcolor=COLORS["border"],
                        darkcolor=COLORS["border"], arrowsize=14)
        style.map("TCombobox", fieldbackground=[("readonly", COLORS["panel2"]), ("focus", COLORS["panel2"]),
                                                ("active", COLORS["panel3"]), ("pressed", COLORS["panel3"])],
                  foreground=[("readonly", COLORS["text"])],
                  background=[("active", COLORS["panel3"]), ("pressed", COLORS["panel3"])],
                  bordercolor=[("focus", COLORS["accent"]), ("!focus", COLORS["border"])],
                  arrowcolor=[("active", COLORS["accent"]), ("pressed", COLORS["accent"]),
                              ("focus", COLORS["accent"]), ("!focus", COLORS["muted"])])
        self.option_add("*TCombobox*Listbox*Background", COLORS["panel2"])
        self.option_add("*TCombobox*Listbox*Foreground", COLORS["text"])
        self.option_add("*TCombobox*Listbox*selectBackground", COLORS["accent2"])
        self.option_add("*TCombobox*Listbox*selectForeground", "white")

        style.configure("TCheckbutton", background=COLORS["panel"], foreground=COLORS["text"])
        style.map("TCheckbutton", background=[("active", COLORS["panel2"]), ("selected", COLORS["panel"])],
                  foreground=[("active", COLORS["text"])])

        style.configure("TNotebook", background=COLORS["bg"], borderwidth=1,
                        bordercolor=COLORS["border"], lightcolor=COLORS["border"], darkcolor=COLORS["border"])
        style.configure("TNotebook.Tab", background=COLORS["panel"], foreground=COLORS["muted"],
                        padding=(14, 8), borderwidth=1, bordercolor=COLORS["border"],
                        lightcolor=COLORS["border"], darkcolor=COLORS["border"])
        style.map("TNotebook.Tab", background=[("selected", COLORS["panel2"]), ("active", COLORS["panel3"])],
                  foreground=[("selected", COLORS["text"]), ("active", COLORS["text"])],
                  bordercolor=[("selected", COLORS["accent"]), ("!selected", COLORS["border"])])

        for name in ("Vertical", "Horizontal"):
            # Базовый стиль (используется Combobox dropdown и другими ttk-скроллбарами)
            style.configure(f"{name}.TScrollbar",
                            background=COLORS["scrollbar"],
                            troughcolor=COLORS["terminal"],
                            bordercolor=COLORS["terminal"],
                            arrowcolor=COLORS["bg"],
                            darkcolor=COLORS["scrollbar"],
                            lightcolor=COLORS["scrollbar"],
                            relief="flat",
                            borderwidth=0,
                            arrowsize=12)
            style.map(f"{name}.TScrollbar",
                      background=[("active", COLORS["scrollbar_hover"]),
                                  ("pressed", COLORS["scrollbar_pressed"])],
                      arrowcolor=[("active", COLORS["text"]),
                                  ("pressed", COLORS["text"])])

            # Явный Dark-стиль (для логов, fingerprint canvas и т.д.)
            style.configure(f"Dark.{name}.TScrollbar",
                            background=COLORS["scrollbar"],
                            troughcolor=COLORS["terminal"],
                            bordercolor=COLORS["terminal"],
                            arrowcolor=COLORS["bg"],
                            darkcolor=COLORS["scrollbar"],
                            lightcolor=COLORS["scrollbar"],
                            relief="flat",
                            borderwidth=0,
                            arrowsize=12)
            style.map(f"Dark.{name}.TScrollbar",
                      background=[("active", COLORS["scrollbar_hover"]),
                                  ("pressed", COLORS["scrollbar_pressed"])],
                      arrowcolor=[("active", COLORS["text"]),
                                  ("pressed", COLORS["text"])])

    # --------------------------------------------------------
    # Layout
    # --------------------------------------------------------
    def build_layout(self):
        """Build the sidebar + content area skeleton."""
        self.sidebar = tk.Frame(self, bg=COLORS["panel"], width=220)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self.content = tk.Frame(self, bg=COLORS["bg"])
        self.content.pack(side="left", fill="both", expand=True)

        tk.Label(self.sidebar, text="CAMOUFOX", bg=COLORS["panel"],
                 fg=COLORS["accent"], font=("TkDefaultFont", 17, "bold")).pack(anchor="w", padx=22, pady=(28, 2))
        tk.Label(self.sidebar, text="CONTROL CENTER", bg=COLORS["panel"],
                 fg=COLORS["accent"], font=("TkDefaultFont", 8, "bold")).pack(anchor="w", padx=23, pady=(0, 25))

        self.nav_buttons = {}
        navigation = [
            ("dashboard", "⌂   Dashboard"),
            ("profiles", "◉   Profiles"),
            ("setup", "⚙   Setup & Logs"),
        ]
        for page_id, text in navigation:
            button = SidebarButton(self.sidebar, text=text, command=lambda p=page_id: self.show_page(p))
            button.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[page_id] = button

        bottom = tk.Frame(self.sidebar, bg=COLORS["panel"])
        bottom.pack(side="bottom", fill="x", padx=18, pady=20)
        tk.Label(bottom, text="STATUS", bg=COLORS["panel"], fg=COLORS["muted"],
                 font=("TkDefaultFont", 8, "bold")).pack(anchor="w")
        self.sidebar_status = tk.Label(bottom, textvariable=self.status_var,
                                       bg=COLORS["panel"], fg=COLORS["green"])
        self.sidebar_status.pack(anchor="w", pady=(4, 0))

    def clear_content(self):
        for child in self.content.winfo_children(): child.destroy()

    def show_page(self, page):
        """Switch to one of: 'dashboard', 'setup', 'profiles'."""
        self.current_page = page
        self.clear_content()
        for page_id, button in self.nav_buttons.items():
            button.set_selected(page_id == page)

        if page == "dashboard": self.page_dashboard()
        elif page == "setup": self.page_setup()
        elif page == "profiles": self.page_profiles()

    def page_header(self, title, subtitle=""):
        """Reusable page header with title, subtitle and accent underline."""
        header = tk.Frame(self.content, bg=COLORS["bg"])
        header.pack(fill="x", padx=32, pady=(28, 20))
        tk.Label(header, text=title, bg=COLORS["bg"], fg=COLORS["text"],
                 font=("TkDefaultFont", 21, "bold")).pack(anchor="w")
        if subtitle:
            tk.Label(header, text=subtitle, bg=COLORS["bg"], fg=COLORS["muted"]).pack(anchor="w", pady=(5, 0))
        tk.Frame(header, bg=COLORS["accent"], height=2).pack(fill="x", pady=(14, 0))

    # --------------------------------------------------------
    # Dashboard
    # --------------------------------------------------------
    def page_dashboard(self):
        self.page_header("Dashboard", "Camoufox environment at a glance")

        cards = tk.Frame(self.content, bg=COLORS["bg"])
        cards.pack(fill="x", padx=32)
        self.dashboard_card(cards, "CAMOUFOX", self.version_var, 0)
        self.dashboard_card(cards, "CHANNEL", self.channel_var, 1)
        self.dashboard_card(cards, "ACTIVE PROFILE", self.profile_var, 2)
        self.dashboard_card(cards, "BINARY", tk.StringVar(value="Found" if find_binary() else "Not found"), 3)

        actions = tk.Frame(self.content, bg=COLORS["bg"])
        actions.pack(fill="x", padx=32, pady=28)
        self.rbutton(actions, text="▶  Launch Active Profile", style="Accent.TButton",
                     command=self.launch_active_profile).pack(side="left", padx=(0, 8))
        self.rbutton(actions, text="⚙  Setup & Logs",
                     command=lambda: self.show_page("setup")).pack(side="left", padx=8)
        self.rbutton(actions, text="◉  Manage Profiles",
                     command=lambda: self.show_page("profiles")).pack(side="left", padx=8)

        # Running sessions panel
        sessions_panel = tk.Frame(self.content, bg=COLORS["panel"])
        sessions_panel.pack(fill="x", padx=32, pady=(0, 20))
        tk.Label(sessions_panel, text="Running Sessions", bg=COLORS["panel"], fg=COLORS["text"],
                 font=("TkDefaultFont", 12, "bold")).pack(anchor="w", padx=20, pady=(20, 12))

        self.sessions_frame = tk.Frame(sessions_panel, bg=COLORS["panel"])
        self.sessions_frame.pack(fill="x", padx=20, pady=(0, 20))
        self.refresh_sessions_ui()

        env_panel = tk.Frame(self.content, bg=COLORS["panel"])
        env_panel.pack(fill="x", padx=32, pady=(0, 32))
        tk.Label(env_panel, text="Environment", bg=COLORS["panel"], fg=COLORS["text"],
                 font=("TkDefaultFont", 12, "bold")).pack(anchor="w", padx=20, pady=(20, 12))
        self.environment_row(env_panel, "Python", sys.executable)
        self.environment_row(env_panel, "Virtual environment", str(self.venv_dir))
        binary = find_binary()
        self.environment_row(env_panel, "Camoufox binary", str(binary) if binary else "Not found")

    def dashboard_card(self, parent, title, variable, column):
        card = tk.Frame(parent, bg=COLORS["panel"], height=105)
        card.grid(row=0, column=column, sticky="nsew", padx=5)
        parent.grid_columnconfigure(column, weight=1)
        tk.Label(card, text=title, bg=COLORS["panel"], fg=COLORS["muted"],
                 font=("TkDefaultFont", 8, "bold")).pack(anchor="w", padx=16, pady=(16, 4))
        tk.Label(card, textvariable=variable, bg=COLORS["panel"], fg=COLORS["text"],
                 font=("TkDefaultFont", 14, "bold")).pack(anchor="w", padx=16)

    def environment_row(self, parent, name, value):
        row = tk.Frame(parent, bg=COLORS["panel"])
        row.pack(fill="x", padx=20, pady=4)
        tk.Label(row, text=name, width=23, anchor="w", bg=COLORS["panel"], fg=COLORS["muted"]).pack(side="left")
        tk.Label(row, text=value, anchor="w", bg=COLORS["panel"], fg=COLORS["text"]).pack(side="left", fill="x", expand=True)

    def refresh_sessions_ui(self):
        """Rebuild the 'Running Sessions' list from self.process_managers."""
        if not hasattr(self, "sessions_frame"): return
        try:
            if not self.sessions_frame.winfo_exists():
                return
        except tk.TclError:
            return
        for child in self.sessions_frame.winfo_children(): child.destroy()

        running = {k: v for k, v in self.process_managers.items() if v.running}
        if not running:
            tk.Label(self.sessions_frame, text="No active sessions.", bg=COLORS["panel"],
                     fg=COLORS["muted"]).pack(anchor="w", pady=10)
            return

        for name, pm in running.items():
            row = tk.Frame(self.sessions_frame, bg=COLORS["panel2"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=f"● {name}", bg=COLORS["panel2"], fg=COLORS["green"],
                     font=("TkDefaultFont", 10, "bold")).pack(side="left", padx=10, pady=8)
            self.rbutton(row, text="Stop", style="Danger.TButton",
                         command=lambda n=name: self.stop_profile(n)).pack(side="right", padx=10, pady=4)

    # --------------------------------------------------------
    # Setup
    # --------------------------------------------------------
    def page_setup(self):
        self.page_header("Setup | Update", "Prepare or update the Camoufox environment")
        container = tk.Frame(self.content, bg=COLORS["bg"])
        container.pack(fill="x", padx=32)

        steps = [
            (1, "Python environment", "Create the virtual environment"),
            (2, "Camoufox package", "Install camoufox[geoip]"),
            (3, "Browser binary", "Fetch/update Camoufox"),
            (4, "Configurations", "Generate launch scripts for all profiles"),
        ]
        for number, title, description in steps:
            row = tk.Frame(container, bg=COLORS["panel"])
            row.pack(fill="x", pady=5)
            tk.Label(row, text=str(number), width=3, bg=COLORS["panel2"],
                     fg=COLORS["accent"], font=("TkDefaultFont", 11, "bold")).pack(side="left", padx=14, pady=14)
            text = tk.Frame(row, bg=COLORS["panel"])
            text.pack(side="left", fill="x", expand=True, pady=12)
            tk.Label(text, text=title, bg=COLORS["panel"], fg=COLORS["text"],
                     font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
            tk.Label(text, text=description, bg=COLORS["panel"], fg=COLORS["muted"]).pack(anchor="w")
            self.rbutton(row, text="Run", command=lambda n=number: self.run_setup_step(n)).pack(side="right", padx=14)

        # Bottom row: Channel | Venv name | Run Full Setup
        fields_row = tk.Frame(self.content, bg=COLORS["bg"])
        fields_row.pack(fill="x", padx=32, pady=(14, 20))

        ch_frame = tk.Frame(fields_row, bg=COLORS["bg"])
        ch_frame.pack(side="left", padx=(0, 12))
        tk.Label(ch_frame, text="Channel", bg=COLORS["bg"], fg=COLORS["muted"]).pack(anchor="w")
        ttk.Combobox(ch_frame, textvariable=self.channel_var, values=("stable", "prerelease"),
                     state="readonly", width=14).pack(pady=(4, 0))

        venv_frame = tk.Frame(fields_row, bg=COLORS["bg"])
        venv_frame.pack(side="left", padx=(0, 12))
        tk.Label(venv_frame, text="Venv name", bg=COLORS["bg"], fg=COLORS["muted"]).pack(anchor="w")
        ttk.Entry(venv_frame, textvariable=self.venv_name_var, width=30).pack(pady=(4, 0))

        self.rbutton(fields_row, text="Run Full Setup", style="Accent.TButton",
                     command=self.run_full_setup).pack(side="right", anchor="s", padx=(12, 0))

        self.build_log_panel(self.content)

    def build_log_panel(self, parent):
        """Build the log viewer (terminal-style Text widget with tags)."""
        logs_header = tk.Frame(parent, bg=COLORS["bg"])
        logs_header.pack(fill="x", padx=32, pady=(4, 8))
        tk.Label(logs_header, text="Logs", bg=COLORS["bg"], fg=COLORS["text"],
                 font=("TkDefaultFont", 12, "bold")).pack(side="left")

        toolbar = tk.Frame(logs_header, bg=COLORS["bg"])
        toolbar.pack(side="right")
        self.rbutton(toolbar, text="Clear", command=self.clear_logs).pack(side="left")
        self.rbutton(toolbar, text="Save Log", command=self.save_logs).pack(side="left", padx=8)
        self.rbutton(toolbar, text="Stop All", style="Danger.TButton",
                     command=self.stop_all_processes).pack(side="left")

        terminal = tk.Frame(parent, bg=COLORS["terminal"])
        terminal.pack(fill="both", expand=True, padx=32, pady=(0, 32))
        scrollbar = ttk.Scrollbar(terminal, orient="vertical", style="Dark.Vertical.TScrollbar")
        horizontal = ttk.Scrollbar(terminal, orient="horizontal", style="Dark.Horizontal.TScrollbar")

        self.log_text = tk.Text(terminal, bg=COLORS["terminal"], fg=COLORS["terminal_text"],
                                insertbackground=COLORS["text"], selectbackground=COLORS["accent2"],
                                font=MONO_FONT, relief="flat", borderwidth=0, wrap="none",
                                yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        scrollbar.configure(command=self.log_text.yview)
        horizontal.configure(command=self.log_text.xview)
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 0))
        scrollbar.grid(row=0, column=1, sticky="ns", pady=(12, 0))
        horizontal.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        terminal.grid_rowconfigure(0, weight=1)
        terminal.grid_columnconfigure(0, weight=1)

        # Color tags for different log categories
        self.log_text.tag_configure("exec", foreground=COLORS["accent"])
        self.log_text.tag_configure("setup", foreground=COLORS["purple"])
        self.log_text.tag_configure("config", foreground=COLORS["green"])
        self.log_text.tag_configure("profile", foreground="#79c0ff")
        self.log_text.tag_configure("fingerprint", foreground="#ffa657")
        self.log_text.tag_configure("launch", foreground=COLORS["green"])
        self.log_text.tag_configure("process", foreground=COLORS["muted"])
        self.log_text.tag_configure("error", foreground=COLORS["red"])
        self.log_text.tag_configure("stop", foreground=COLORS["yellow"])
        self.log_text.tag_configure("info", foreground=COLORS["text"])
        self.log_text.tag_configure("progress", foreground="#d2a8ff")

        if not self.log_buffer:
            self.log_text.insert("1.0", "Camoufox Control Center\n========================\n", "info")
        else:
            for message, tag in self.log_buffer:
                self.log_text.insert(tk.END, message + "\n", tag)
        self.log_text.see(tk.END)

    def apply_venv_name(self):
        """Recompute self.venv_dir whenever the venv name entry changes."""
        name = self.venv_name_var.get().strip()
        self.venv_dir = DEFAULT_VENV.parent / name if name else DEFAULT_VENV

    def get_venv_python(self):
        if os.name == "nt": return self.venv_dir / "Scripts" / "python.exe"
        return self.venv_dir / "bin" / "python3"

    def get_venv_pip(self):
        if os.name == "nt": return self.venv_dir / "Scripts" / "pip.exe"
        return self.venv_dir / "bin" / "pip"

    def run_setup_step(self, number):
        """Run a single setup step in a background thread."""
        if any(pm.running for pm in self.process_managers.values()):
            messagebox.showwarning("Busy", "Stop running profiles first.")
            return

        def worker(cmd, msg):
            self.log(f"[SETUP] {msg}")
            pm = ProcessManager(self.enqueue_log)
            pm.start(cmd)
            while pm.running: time.sleep(0.2)
            if number == 4:
                for p in self.profiles.values():
                    p.save_fp()
                self.log("[CONFIG] Generated configurations for all profiles.")

        if number == 1:
            threading.Thread(target=worker, args=([sys.executable, "-m", "venv", str(self.venv_dir)], "Creating virtual environment..."), daemon=True).start()
        elif number == 2:
            threading.Thread(target=worker, args=([str(self.get_venv_pip()), "install", "camoufox[geoip]"], "Installing camoufox..."), daemon=True).start()
        elif number == 3:
            python = self.get_venv_python()
            channel = self.channel_var.get()
            cmd1 = [str(python), "-m", "camoufox", "set", f"official/{channel}"]
            cmd2 = [str(python), "-m", "camoufox", "fetch"]
            def chain():
                self.log(f"[SETUP] Setting channel {channel}...")
                pm1 = ProcessManager(self.enqueue_log)
                pm1.start(cmd1)
                while pm1.running: time.sleep(0.2)
                self.log(f"[SETUP] Fetching browser binary...")
                pm2 = ProcessManager(self.enqueue_log)
                pm2.start(cmd2)
                while pm2.running: time.sleep(0.2)
            threading.Thread(target=chain, daemon=True).start()
        elif number == 4:
            def gen():
                for p in self.profiles.values():
                    p.save_fp()
                self.log("[CONFIG] Generated configurations for all profiles.")
            threading.Thread(target=gen, daemon=True).start()

    def run_full_setup(self):
        """Run all setup steps sequentially in a background thread."""
        if any(pm.running for pm in self.process_managers.values()):
            messagebox.showwarning("Busy", "Stop running profiles first.")
            return

        def setup_worker():
            try:
                cmds = [
                    ([sys.executable, "-m", "venv", str(self.venv_dir)], "Creating virtual environment..."),
                    ([str(self.get_venv_pip()), "install", "camoufox[geoip]"], "Installing camoufox..."),
                    ([str(self.get_venv_python()), "-m", "camoufox", "set", f"official/{self.channel_var.get()}"], "Setting channel..."),
                    ([str(self.get_venv_python()), "-m", "camoufox", "fetch"], "Fetching browser binary..."),
                ]
                for cmd, msg in cmds:
                    self.log(f"[SETUP] {msg}")
                    pm = ProcessManager(self.enqueue_log)
                    pm.start(cmd)
                    while pm.running: time.sleep(0.2)

                self.log("[SETUP] Generating configurations...")
                for profile in self.profiles.values():
                    profile.save_fp()
                self.log("[SETUP] Full setup completed.")
            except Exception as e:
                self.log(f"[ERROR] Setup failed: {e}")

        threading.Thread(target=setup_worker, daemon=True).start()

    # --------------------------------------------------------
    # Profiles
    # --------------------------------------------------------
    def page_profiles(self):
        self.page_header("Profiles", "Manage browser profiles and their fingerprints")
        outer = tk.Frame(self.content, bg=COLORS["bg"])
        outer.pack(fill="both", expand=True, padx=32, pady=(0, 30))

        # --- Left panel: profile list + 3 management buttons ---
        left = tk.Frame(outer, bg=COLORS["panel"], width=320)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        tk.Label(left, text="PROFILE LIBRARY", bg=COLORS["panel"], fg=COLORS["muted"],
                 font=("TkDefaultFont", 8, "bold")).pack(anchor="w", padx=18, pady=(18, 8))

        self.profile_list = tk.Listbox(left, bg=COLORS["panel"], fg=COLORS["text"],
                                       selectbackground=COLORS["accent2"], selectforeground="white",
                                       borderwidth=0, highlightthickness=0, activestyle="none", font=FONT)
        self.profile_list.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        buttons = tk.Frame(left, bg=COLORS["panel"])
        buttons.pack(fill="x", padx=12, pady=12)
        btn_row = tk.Frame(buttons, bg=COLORS["panel"])
        btn_row.pack()
        self.rbutton(btn_row, text="+ New", command=self.new_profile).pack(side="left", padx=(0, 4))
        self.rbutton(btn_row, text="Duplicate", command=self.duplicate_profile).pack(side="left", padx=4)
        self.rbutton(btn_row, text="Delete", style="Danger.TButton", command=self.delete_profile).pack(side="left", padx=(4, 0))

        # --- Right panel: form + 5 action buttons in a column ---
        right = tk.Frame(outer, bg=COLORS["panel"])
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        tk.Label(right, text="Profile Details", bg=COLORS["panel"], fg=COLORS["text"],
                 font=("TkDefaultFont", 15, "bold")).pack(anchor="w", padx=22, pady=(22, 3))
        tk.Label(right, text="Each profile has its own browser data and fingerprint settings.",
                 bg=COLORS["panel"], fg=COLORS["muted"]).pack(anchor="w", padx=22)

        form = tk.Frame(right, bg=COLORS["panel"])
        form.pack(fill="x", padx=22, pady=28)

        self.profile_name_entry = self.form_entry(form, "Name", "", panel=True)
        self.profile_path_entry = self.form_entry(form, "Browser data", "", panel=True)

        # All 5 buttons share one container so they stretch to the same width
        btn_column = tk.Frame(form, bg=COLORS["panel"])
        btn_column.pack(fill="x", pady=(8, 0))
        self.rbutton(btn_column, text="Select directory",
                     command=self.select_profile_directory).pack(fill="x", pady=15)
        self.rbutton(btn_column, text="Save Profile", style="Accent.TButton",
                     command=self.save_profile).pack(fill="x", pady=15)
        self.rbutton(btn_column, text="Edit Fingerprint",
                     command=self.edit_fingerprint).pack(fill="x", pady=15)
        self.rbutton(btn_column, text="▶ Launch Profile", style="Accent.TButton",
                     command=self.launch_active_profile).pack(fill="x", pady=15)
        self.rbutton(btn_column, text="■ Stop", style="Danger.TButton",
                     command=self.stop_active_profile).pack(fill="x", pady=15)

        self.profile_list.bind("<<ListboxSelect>>", self.on_profile_select)
        self.refresh_profiles_list()

    def refresh_profiles(self):
        """Scan DEFAULT_PROFILES on disk and rebuild self.profiles."""
        DEFAULT_PROFILES.mkdir(parents=True, exist_ok=True)
        self.profiles = {}
        for p in DEFAULT_PROFILES.iterdir():
            if p.is_dir():
                self.profiles[p.name] = Profile(p.name, DEFAULT_PROFILES)

        if not self.profiles:
            self.profiles["Default"] = Profile("Default", DEFAULT_PROFILES)

        if self.active_profile_name not in self.profiles:
            self.active_profile_name = next(iter(self.profiles))

        self.profile_var.set(self.active_profile_name)
        if hasattr(self, "profile_list"):
            self.refresh_profiles_list()

    def refresh_profiles_list(self):
        """Repopulate the listbox from self.profiles."""
        if not hasattr(self, "profile_list"): return
        self.profile_list.delete(0, tk.END)
        sorted_names = sorted(self.profiles.keys())
        for name in sorted_names:
            self.profile_list.insert(tk.END, name)

        if self.active_profile_name in sorted_names:
            idx = sorted_names.index(self.active_profile_name)
            self.profile_list.selection_set(idx)
            self.profile_list.see(idx)
            self.on_profile_select()

    def on_profile_select(self, event=None):
        """Update the form fields when the user clicks a profile in the list."""
        if not hasattr(self, "profile_list"): return
        selection = self.profile_list.curselection()
        if not selection: return
        name = self.profile_list.get(selection[0])
        self.active_profile_name = name
        self.profile_var.set(name)

        profile = self.profiles[name]
        self.profile_name_entry.delete(0, tk.END)
        self.profile_name_entry.insert(0, name)
        self.profile_path_entry.delete(0, tk.END)
        self.profile_path_entry.insert(0, profile.fingerprint.profile_dir)

    def new_profile(self):
        """Create a brand-new profile with a unique name and select it."""
        base = "New Profile"
        name = base
        counter = 1
        while name in self.profiles or (DEFAULT_PROFILES / name).exists():
            counter += 1
            name = f"{base} {counter}"
        profile = Profile(name, DEFAULT_PROFILES)
        self.profiles[name] = profile
        self.active_profile_name = name
        self.profile_var.set(name)
        self.refresh_profiles_list()
        self.log(f"[PROFILE] Created new profile: {name}")

    def duplicate_profile(self):
        source_name = self.active_profile_name
        if not source_name: return
        source = self.profiles[source_name]
        target_name = f"{source_name} Copy"
        target_dir = DEFAULT_PROFILES / target_name
        index = 2
        while target_dir.exists():
            target_name = f"{source_name} Copy {index}"
            target_dir = DEFAULT_PROFILES / target_name
            index += 1
        try:
            shutil.copytree(source.dir, target_dir)
            self.refresh_profiles()
            self.log(f"[PROFILE] Duplicated: {source_name} -> {target_name}")
        except Exception as exc:
            messagebox.showerror("Profile error", str(exc))

    def delete_profile(self):
        name = self.active_profile_name
        if not name: return
        if name == "Default":
            messagebox.showwarning("Protected", "Default profile cannot be deleted.")
            return
        if not messagebox.askyesno("Delete profile", f"Delete profile '{name}'?"): return
        try:
            shutil.rmtree(DEFAULT_PROFILES / name)
            self.refresh_profiles()
            self.log(f"[PROFILE] Deleted: {name}")
        except Exception as exc:
            messagebox.showerror("Profile error", str(exc))

    def select_profile_directory(self):
        directory = DirectoryChooser.ask_directory(
            self, "Select Profile Directory",
            initialdir=self.profile_path_entry.get() or None
        )
        if directory:
            self.profile_path_entry.delete(0, tk.END)
            self.profile_path_entry.insert(0, directory)

    def save_profile(self):
        """
        Persist the current profile.
        - If the name changed, rename the profile folder on disk.
        - Regenerate fingerprint.json and camoufox-config.py.
        """
        old_name = self.active_profile_name
        new_name = self.profile_name_entry.get().strip()
        path_str = self.profile_path_entry.get().strip()

        if not new_name:
            messagebox.showwarning("Invalid profile", "Enter a profile name.")
            return
        if not path_str:
            path_str = str(DEFAULT_PROFILES / new_name)

        # Conflict check
        if new_name != old_name and (new_name in self.profiles or (DEFAULT_PROFILES / new_name).exists()):
            messagebox.showwarning("Name conflict", f"Profile '{new_name}' already exists.")
            return

        old_dir = DEFAULT_PROFILES / old_name
        new_dir = DEFAULT_PROFILES / new_name

        # Capture old fingerprint path before we mutate anything
        old_profile = self.profiles.get(old_name)
        old_fp_dir = None
        if old_profile:
            try:
                old_fp_dir = expand_path(old_profile.fingerprint.profile_dir)
            except Exception:
                old_fp_dir = None

        user_path = expand_path(path_str)

        # Rename the profile folder if the name changed
        if old_name != new_name:
            if old_dir.exists():
                try:
                    shutil.move(str(old_dir), str(new_dir))
                except Exception as exc:
                    messagebox.showerror("Rename error", str(exc))
                    return
            else:
                new_dir.mkdir(parents=True, exist_ok=True)
        else:
            new_dir.mkdir(parents=True, exist_ok=True)

        # If the browser-data path was the old CCC folder itself, it has moved → update it
        if (old_name != new_name
                and old_fp_dir is not None
                and old_fp_dir.resolve() == old_dir.resolve()
                and user_path.resolve() == old_dir.resolve()):
            final_path = new_dir
        else:
            final_path = user_path
        final_path.mkdir(parents=True, exist_ok=True)

        # Rebuild profiles from disk
        self.refresh_profiles()
        if new_name not in self.profiles:
            self.profiles[new_name] = Profile(new_name, DEFAULT_PROFILES)
        self.active_profile_name = new_name
        self.profile_var.set(new_name)

        profile = self.profiles[new_name]
        profile.fingerprint.profile_dir = str(final_path)
        profile.save_fp()

        # Keep the form in sync
        self.profile_path_entry.delete(0, tk.END)
        self.profile_path_entry.insert(0, str(final_path))
        self.refresh_profiles_list()
        self.log(f"[PROFILE] Saved: {new_name}")

    def edit_fingerprint(self):
        if not self.active_profile_name:
            messagebox.showwarning("No profile", "Select a profile first.")
            return
        self.page_fingerprint()

    # --------------------------------------------------------
    # Fingerprint editor (inline page)
    # --------------------------------------------------------
    def page_fingerprint(self):
        """Open the fingerprint editor for the active profile."""
        self.clear_content()
        for page_id, button in self.nav_buttons.items():
            button.set_selected(False)

        self.page_header(f"Fingerprint — {self.active_profile_name}",
                         "Edit launch configuration for this profile")

        tabs_bar = tk.Frame(self.content, bg=COLORS["bg"])
        tabs_bar.pack(fill="x", padx=32, pady=(0, 12))
        self.fp_tab_buttons = {}

        def switch_tab(name):
            for key, btn in self.fp_tab_buttons.items():
                if key == name:
                    btn.itemconfig(btn._rect, fill=COLORS["panel2"])
                    btn.itemconfig(btn._label, fill=COLORS["text"])
                else:
                    btn.itemconfig(btn._rect, fill=COLORS["panel"])
                    btn.itemconfig(btn._label, fill=COLORS["muted"])
            if name == "simple":
                self.fp_advanced_frame.pack_forget()
                self.fp_simple_frame.pack(fill="both", expand=True)
                self._sync_simple_from_fingerprint()
            else:
                self.fp_simple_frame.pack_forget()
                self.fp_advanced_frame.pack(fill="both", expand=True)
                self.load_raw_config()

        btn_simple = self.rbutton(tabs_bar, text="Simple", command=lambda: switch_tab("simple"))
        btn_simple.pack(side="left", padx=(24, 8))
        self.fp_tab_buttons["simple"] = btn_simple
        btn_advanced = self.rbutton(tabs_bar, text="Advanced", command=lambda: switch_tab("advanced"))
        btn_advanced.pack(side="left", padx=(8, 0))
        self.fp_tab_buttons["advanced"] = btn_advanced
        btn_simple.itemconfig(btn_simple._rect, fill=COLORS["panel2"])
        btn_simple.itemconfig(btn_simple._label, fill=COLORS["text"])

        content_wrap = tk.Frame(self.content, bg=COLORS["bg"])
        content_wrap.pack(fill="both", expand=True, padx=32, pady=(0, 25))
        self.fp_simple_frame = tk.Frame(content_wrap, bg=COLORS["bg"])
        self.fp_advanced_frame = tk.Frame(content_wrap, bg=COLORS["bg"])

        self.build_fingerprint_simple(self.fp_simple_frame)
        self.build_fingerprint_advanced(self.fp_advanced_frame)
        self.fp_simple_frame.pack(fill="both", expand=True)

        # "Back to Profiles" button, aligned with the Reset button above
        bottom_bar = tk.Frame(self.content, bg=COLORS["bg"])
        bottom_bar.pack(fill="x", padx=(48, 32), pady=(0, 20))
        self.rbutton(bottom_bar, text="← Back to Profiles",
                     command=lambda: self.show_page("profiles")).pack(side="left")

    def _sync_simple_from_fingerprint(self):
        """Push the current fingerprint values into the Simple-mode widgets."""
        profile = self.profiles[self.active_profile_name]
        for key, variable in self.fp_vars.items():
            if key in profile.fingerprint.values:
                value = profile.fingerprint.values[key]
                if isinstance(value, list):
                    variable.set(", ".join(str(x) for x in value))
                else:
                    variable.set(str(value))
        self.fp_profile_var.set(profile.fingerprint.profile_dir)
        self.config_file_var.set(str(profile.config_path))

    def build_fingerprint_simple(self, parent):
        """Build the 'Simple' fingerprint editor (scrollable card grid)."""
        canvas = tk.Canvas(parent, bg=COLORS["bg"], highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview, style="Dark.Vertical.TScrollbar")
        inner = tk.Frame(canvas, bg=COLORS["bg"])
        canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def update_scrollregion(_event=None): canvas.configure(scrollregion=canvas.bbox("all"))
        def resize_inner(event): canvas.itemconfigure(canvas_window, width=event.width)
        inner.bind("<Configure>", update_scrollregion)
        canvas.bind("<Configure>", resize_inner)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_mousewheel(event):
            if getattr(event, "delta", 0): canvas.yview_scroll(int(-event.delta / 120), "units")
            elif getattr(event, "num", None) == 4: canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5: canvas.yview_scroll(1, "units")
            return "break"

        def bind_mousewheel_recursive(widget):
            widget.bind("<MouseWheel>", on_mousewheel)
            widget.bind("<Button-4>", on_mousewheel)
            widget.bind("<Button-5>", on_mousewheel)
            for child in widget.winfo_children(): bind_mousewheel_recursive(child)

        self.fp_vars = {}
        profile = self.profiles[self.active_profile_name]

        def card(parent_frame, title):
            frame = tk.Frame(parent_frame, bg=COLORS["panel"], highlightbackground=COLORS["border"], highlightthickness=1, bd=0)
            head = tk.Frame(frame, bg=COLORS["panel"])
            head.pack(fill="x", padx=10, pady=(6, 2))
            tk.Label(head, text=title.upper(), bg=COLORS["panel"], fg=COLORS["accent"],
                     font=("TkDefaultFont", 8, "bold")).pack(anchor="w")
            body = tk.Frame(frame, bg=COLORS["panel"])
            body.pack(fill="both", expand=True, padx=10, pady=(0, 6))
            return frame, body

        def row(body, label, key, default, width=None, combo_values=None):
            value = profile.fingerprint.values.get(key, default)
            if isinstance(value, list): value = ", ".join(str(x) for x in value)
            variable = tk.StringVar(value=str(value))
            self.fp_vars[key] = variable
            line = tk.Frame(body, bg=COLORS["panel"])
            line.pack(fill="x", pady=1)
            tk.Label(line, text=label, bg=COLORS["panel"], fg=COLORS["muted"],
                     font=("TkDefaultFont", 8), width=16, anchor="w").pack(side="left")
            if combo_values is not None:
                cb = ttk.Combobox(line, textvariable=variable, values=combo_values, state="readonly", width=width or 14)
                cb.pack(side="left", fill="x", expand=True)
            else:
                ent = ttk.Entry(line, textvariable=variable, width=width or 0)
                ent.pack(side="left", fill="x", expand=True)
            return variable

        def pair_row(body, items):
            line = tk.Frame(body, bg=COLORS["panel"])
            line.pack(fill="x", pady=1)
            for i, (label, key, default, width) in enumerate(items):
                value = profile.fingerprint.values.get(key, default)
                if isinstance(value, list): value = ", ".join(str(x) for x in value)
                variable = tk.StringVar(value=str(value))
                self.fp_vars[key] = variable
                cell = tk.Frame(line, bg=COLORS["panel"])
                cell.pack(side="left", fill="x", expand=True, padx=(0 if i == 0 else 6, 0))
                tk.Label(cell, text=label, bg=COLORS["panel"], fg=COLORS["muted"],
                         font=("TkDefaultFont", 8), anchor="w").pack(anchor="w")
                ttk.Entry(cell, textvariable=variable, width=width or 10).pack(fill="x", pady=(1, 0))

        def combo_pair(body, items):
            line = tk.Frame(body, bg=COLORS["panel"])
            line.pack(fill="x", pady=1)
            for i, (label, key, values) in enumerate(items):
                value = str(profile.fingerprint.values.get(key, values[0]))
                variable = tk.StringVar(value=value)
                self.fp_vars[key] = variable
                cell = tk.Frame(line, bg=COLORS["panel"])
                cell.pack(side="left", fill="x", expand=True, padx=(0 if i == 0 else 6, 0))
                tk.Label(cell, text=label, bg=COLORS["panel"], fg=COLORS["muted"],
                         font=("TkDefaultFont", 8), anchor="w").pack(anchor="w")
                ttk.Combobox(cell, textvariable=variable, values=values, state="readonly", width=12).pack(fill="x", pady=(1, 0))

        grid = tk.Frame(inner, bg=COLORS["bg"])
        grid.pack(fill="x", padx=16, pady=(10, 0))
        grid.grid_columnconfigure(0, weight=1, uniform="fp")
        grid.grid_columnconfigure(1, weight=1, uniform="fp")

        startup, body = card(grid, "Startup")
        startup.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        row(body, "Startup URL", "startup_url", "https://duckduckgo.com")

        proxy, body = card(grid, "Proxy")
        proxy.grid(row=1, column=0, columnspan=2, sticky="ew", pady=5)
        row(body, "Server", "proxy.server", "")
        pair_row(body, [("Username", "proxy.username", "", 14), ("Password", "proxy.password", "", 14)])

        env, body = card(grid, "Environment")
        env.grid(row=2, column=0, sticky="nsew", padx=(0, 4), pady=5)
        combo_pair(body, [("OS", "os", OS_OPTIONS), ("Locale", "locale", LOCALE_OPTIONS)])
        combo_pair(body, [("Timezone", "timezone", TIMEZONE_OPTIONS),
                          ("CPU cores", "navigator.hardwareConcurrency", ["2", "4", "8", "16", "32"])])
        self.rbutton(body, text="Detect coordinates from timezone", command=self.detect_coordinates).pack(anchor="w", pady=(4, 0))

        geo, body = card(grid, "Geolocation")
        geo.grid(row=2, column=1, sticky="nsew", padx=(4, 0), pady=5)
        pair_row(body, [("Latitude", "latitude", "41.0082", 12), ("Longitude", "longitude", "28.9784", 12)])

        lang, body = card(grid, "Language")
        lang.grid(row=3, column=0, sticky="nsew", padx=(0, 4), pady=5)
        row(body, "Language", "navigator.language", "en-US", width=18)
        row(body, "Languages", "navigator.languages", "en-US, en", width=18)
        row(body, "Accept-Language", "headers.Accept-Language", "en-US,en;q=0.9", width=18)

        window, body = card(grid, "Browser Window")
        window.grid(row=3, column=1, sticky="nsew", padx=(4, 0), pady=5)
        pair_row(body, [("Width", "window.width", "1696", 10), ("Height", "window.height", "1026", 10)])

        screen, body = card(grid, "Screen")
        screen.grid(row=4, column=0, columnspan=2, sticky="ew", pady=5)
        def screen_row(items):
            line = tk.Frame(body, bg=COLORS["panel"])
            line.pack(fill="x", pady=1)
            for i, (label, key, default) in enumerate(items):
                value = profile.fingerprint.values.get(key, default)
                variable = tk.StringVar(value=str(value))
                self.fp_vars[key] = variable
                cell = tk.Frame(line, bg=COLORS["panel"])
                cell.pack(side="left", fill="x", expand=True, padx=(0 if i == 0 else 8, 0))
                tk.Label(cell, text=label, bg=COLORS["panel"], fg=COLORS["muted"],
                         font=("TkDefaultFont", 8), width=12, anchor="w").pack(anchor="w")
                ttk.Entry(cell, textvariable=variable, width=12).pack(fill="x", pady=(1, 0))
        screen_row([("Width", "screen.width", "1700"), ("Height", "screen.height", "970"), ("Avail width", "screen.availWidth", "1700")])
        screen_row([("Avail height", "screen.availHeight", "940"), ("Color depth", "screen.colorDepth", "24"), ("Pixel depth", "screen.pixelDepth", "24")])

        identity, body = card(grid, "Browser Identity")
        identity.grid(row=5, column=0, columnspan=2, sticky="ew", pady=5)
        row(body, "User-Agent", "headers.User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0")

        paths, body = card(grid, "Paths")
        paths.grid(row=6, column=0, columnspan=2, sticky="ew", pady=5)
        cfg_line = tk.Frame(body, bg=COLORS["panel"])
        cfg_line.pack(fill="x", pady=1)
        tk.Label(cfg_line, text="Config file", bg=COLORS["panel"], fg=COLORS["muted"],
                 font=("TkDefaultFont", 8), width=16, anchor="w").pack(side="left")
        self.config_file_var = tk.StringVar(value=str(profile.config_path))
        ttk.Entry(cfg_line, textvariable=self.config_file_var).pack(side="left", fill="x", expand=True)

        self.fp_profile_var = tk.StringVar(value=profile.fingerprint.profile_dir)
        prof_line = tk.Frame(body, bg=COLORS["panel"])
        prof_line.pack(fill="x", pady=1)
        tk.Label(prof_line, text="Profile data", bg=COLORS["panel"], fg=COLORS["muted"],
                 font=("TkDefaultFont", 8), width=16, anchor="w").pack(side="left")
        ttk.Entry(prof_line, textvariable=self.fp_profile_var).pack(side="left", fill="x", expand=True)

        actions = tk.Frame(inner, bg=COLORS["bg"])
        actions.pack(fill="x", padx=16, pady=(6, 14))
        self.rbutton(actions, text="Reset", command=self.reset_fingerprint).pack(side="left")
        self.rbutton(actions, text="Save Configuration", style="Accent.TButton",
                     command=self.save_fingerprint).pack(side="right")

        bind_mousewheel_recursive(canvas)
        bind_mousewheel_recursive(inner)
        inner.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))

    def build_fingerprint_advanced(self, parent):
        """Build the 'Advanced' fingerprint editor (raw Python source)."""
        profile = self.profiles[self.active_profile_name]
        tk.Label(parent, text=f"Raw {profile.config_path.name}. Use this when you need full control.",
                 bg=COLORS["bg"], fg=COLORS["muted"]).pack(anchor="w", padx=12, pady=(10, 8))

        editor_frame = tk.Frame(parent, bg=COLORS["terminal"])
        editor_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        vertical = ttk.Scrollbar(editor_frame, orient="vertical", style="Dark.Vertical.TScrollbar")
        horizontal = ttk.Scrollbar(editor_frame, orient="horizontal", style="Dark.Horizontal.TScrollbar")

        self.raw_config_text = tk.Text(editor_frame, bg=COLORS["terminal"], fg=COLORS["terminal_text"],
                                       insertbackground=COLORS["text"], selectbackground=COLORS["accent2"],
                                       font=MONO_FONT, relief="flat", borderwidth=0, undo=True, wrap="none",
                                       yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.configure(command=self.raw_config_text.yview)
        horizontal.configure(command=self.raw_config_text.xview)
        self.raw_config_text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        editor_frame.grid_rowconfigure(0, weight=1)
        editor_frame.grid_columnconfigure(0, weight=1)

        self.raw_config_text.insert("1.0", read_text(profile.config_path))

        buttons = tk.Frame(parent, bg=COLORS["bg"])
        buttons.pack(fill="x", padx=12, pady=(0, 12))
        self.rbutton(buttons, text="Reload", command=self.load_raw_config).pack(side="left")
        self.rbutton(buttons, text="Save Raw Config", style="Accent.TButton",
                     command=self.save_raw_config).pack(side="right")

    def load_raw_config(self):
        profile = self.profiles[self.active_profile_name]
        self.raw_config_text.delete("1.0", tk.END)
        self.raw_config_text.insert("1.0", read_text(profile.config_path))

    def save_raw_config(self):
        """Save the raw editor text after validating Python syntax."""
        profile = self.profiles[self.active_profile_name]
        text = self.raw_config_text.get("1.0", tk.END)
        try: ast.parse(text)
        except SyntaxError as exc:
            messagebox.showerror("Syntax error", str(exc))
            return
        write_text(profile.config_path, text.rstrip() + "\n")
        self.log(f"[CONFIG] Raw configuration saved for {self.active_profile_name}.")

    def detect_coordinates(self):
        """Fill latitude/longitude from the selected timezone's preset."""
        timezone = self.fp_vars["timezone"].get()
        coordinates = TIMEZONE_COORDINATES.get(timezone)
        if not coordinates:
            messagebox.showinfo("Timezone", "No coordinate preset is available for this timezone.")
            return
        latitude, longitude = coordinates
        self.fp_vars["latitude"].set(str(latitude))
        self.fp_vars["longitude"].set(str(longitude))

        locale = self.fp_vars["locale"].get()
        language_data = LOCALE_TO_LANGUAGE.get(locale)
        if language_data:
            language, languages = language_data
            self.fp_vars["navigator.language"].set(language)
            self.fp_vars["navigator.languages"].set(", ".join(languages))
            self.fp_vars["headers.Accept-Language"].set(
                ",".join([f"{languages[0]};q=1.0"] + [f"{lang};q=0.9" for lang in languages[1:]]))
        self.log(f"[FINGERPRINT] Coordinates detected for {timezone}: {latitude}, {longitude}")

    def reset_fingerprint(self):
        profile = self.profiles[self.active_profile_name]
        profile.fingerprint = FingerprintConfig()
        profile.fingerprint.profile_dir = str(profile.dir)
        self._sync_simple_from_fingerprint()

    def save_fingerprint(self):
        """Read all Simple-mode widgets and persist them to the profile."""
        try:
            profile = self.profiles[self.active_profile_name]
            for key, variable in self.fp_vars.items():
                value = variable.get()
                if isinstance(value, str):
                    value = value.strip()
                    if key in ("latitude", "longitude"): value = float(value)
                    elif key in ("screen.width", "screen.height", "screen.availWidth", "screen.availHeight",
                                 "screen.colorDepth", "screen.pixelDepth", "window.width", "window.height",
                                 "navigator.hardwareConcurrency"): value = int(value)
                    elif key == "navigator.languages": value = [x.strip() for x in value.split(",") if x.strip()]
                profile.fingerprint.values[key] = value

            profile.fingerprint.profile_dir = self.fp_profile_var.get().strip()
            profile.save_fp()
            self.log(f"[CONFIG] Fingerprint saved for {self.active_profile_name}.")
            messagebox.showinfo("Saved", "Fingerprint configuration saved.")
        except Exception as exc:
            messagebox.showerror("Configuration error", str(exc))

    # --------------------------------------------------------
    # Launch & Process Management
    # --------------------------------------------------------
    def launch_active_profile(self):
        if not self.active_profile_name:
            messagebox.showwarning("No profile", "Select a profile first.")
            return
        self.launch_profile(self.active_profile_name)

    def launch_profile(self, name):
        """Spawn the profile's launch script in its own process."""
        if name in self.process_managers and self.process_managers[name].running:
            messagebox.showinfo("Running", f"{name} is already running.")
            return

        profile = self.profiles[name]
        python = self.get_venv_python()
        if not python.exists():
            messagebox.showwarning("Not installed", "Camoufox virtual environment does not exist yet.\nRun Setup first.")
            self.show_page("setup")
            return

        if not profile.config_path.exists():
            profile.save_fp()

        # Tag log lines with the profile name so concurrent logs stay readable
        def log_cb(msg):
            if msg.startswith("[") and "] " in msg:
                tag_end = msg.index("] ") + 2
                new_msg = f"{msg[:tag_end]}[{name}] {msg[tag_end:]}"
            else:
                new_msg = f"[{name}] {msg}"
            self.enqueue_log(new_msg)

        pm = ProcessManager(log_cb)
        self.process_managers[name] = pm
        self.log(f"[LAUNCH] Starting {name}...")
        pm.start([str(python), str(profile.config_path)])

        if hasattr(self, "sessions_frame"):
            self.refresh_sessions_ui()

    def stop_active_profile(self):
        if not self.active_profile_name: return
        self.stop_profile(self.active_profile_name)

    def stop_profile(self, name):
        if name in self.process_managers:
            self.log(f"[STOP] Stopping {name}...")
            self.process_managers[name].stop()
            del self.process_managers[name]
            if hasattr(self, "sessions_frame"):
                self.refresh_sessions_ui()

    def stop_all_processes(self):
        for name in list(self.process_managers.keys()):
            self.stop_profile(name)

    # --------------------------------------------------------
    # Logs
    # --------------------------------------------------------
    def log(self, message): self.enqueue_log(message)

    def enqueue_log(self, message):
        self.log_queue.put(str(message))

    def classify_log(self, message):
        """Pick a color tag based on the message prefix."""
        tag = "info"
        if "[EXEC]" in message: tag = "exec"
        elif "[SETUP]" in message: tag = "setup"
        elif "[CONFIG]" in message: tag = "config"
        elif "[PROFILE]" in message: tag = "profile"
        elif "[FINGERPRINT]" in message: tag = "fingerprint"
        elif "[LAUNCH]" in message: tag = "launch"
        elif "[PROCESS]" in message: tag = "process"
        elif "[ERROR]" in message or "error" in message.lower() or "failed" in message.lower(): tag = "error"
        elif "[STOP]" in message: tag = "stop"
        elif any(x in message.lower() for x in ("%", "downloading", "fetch", "progress", "mb", "kb")): tag = "progress"
        return tag

    def process_logs(self):
        """Drain log_queue into the Text widget, called every 100 ms."""
        try:
            while True:
                message = self.log_queue.get_nowait()
                tag = self.classify_log(message)
                self.log_buffer.append((message, tag))
                if hasattr(self, "log_text") and self.log_text.winfo_exists():
                    self.log_text.insert(tk.END, message + "\n", tag)
                    self.log_text.see(tk.END)
        except queue.Empty: pass
        except tk.TclError: pass
        self.after(100, self.process_logs)

    def clear_logs(self):
        self.log_buffer = []
        if hasattr(self, "log_text"): self.log_text.delete("1.0", tk.END)

    def save_logs(self):
        if not hasattr(self, "log_text"): return
        path = filedialog.asksaveasfilename(title="Save log", defaultextension=".log",
                                            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")])
        if path: write_text(Path(path), self.log_text.get("1.0", tk.END))

    # --------------------------------------------------------
    # Status & helpers
    # --------------------------------------------------------
    def refresh_status(self):
        """Called every second: clean up dead processes, update sidebar status."""
        dead = [name for name, pm in self.process_managers.items() if not pm.running]
        for name in dead:
            del self.process_managers[name]
            self.log(f"[PROCESS] {name} has exited.")
            if hasattr(self, "sessions_frame"):
                self.refresh_sessions_ui()

        if any(pm.running for pm in self.process_managers.values()):
            self.status_var.set("● Running")
            self.sidebar_status.configure(fg=COLORS["green"])
        else:
            self.status_var.set("● Ready")
            self.sidebar_status.configure(fg=COLORS["muted"])

        version = version_from_cache(self.channel_var.get())
        self.version_var.set(version or "Not found")
        self.after(1000, self.refresh_status)

    def form_entry(self, parent, label, value="", variable=None, panel=False):
        """Reusable labeled entry widget."""
        frame = tk.Frame(parent, bg=COLORS["panel"] if panel else COLORS["bg"])
        frame.pack(fill="x", pady=5)
        if variable is None: variable = tk.StringVar(value=value)
        tk.Label(frame, text=label, bg=frame["bg"], fg=COLORS["muted"]).pack(anchor="w", pady=(0, 4))
        entry = ttk.Entry(frame, textvariable=variable)
        entry.pack(fill="x")
        return entry

    def rbutton(self, parent, text="", command=None, style=None, width=None, **kwargs):
        """Shortcut for creating a RoundedButton."""
        return RoundedButton(parent, text=text, command=command, style=style, width=width, **kwargs)


# ============================================================
# Main entry point
# ============================================================
def main():
    app = CamoufoxGUI()

    def on_close():
        running = [name for name, pm in app.process_managers.items() if pm.running]
        if running:
            if not messagebox.askyesno("Exit", f"Stop {len(running)} running profile(s) and exit?"):
                return
            for name in running:
                app.process_managers[name].stop()
        app.destroy()

    app.protocol("WM_DELETE_WINDOW", on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
