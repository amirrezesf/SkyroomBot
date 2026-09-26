"""
Skyroom Bot - Tkinter GUI with integrated wake scheduler.

Run:
    python skyroom_gui.py
    python skyroom_gui.py --json users.json --auto-start
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from skyroom_core import (
    RuntimeConfig,
    SkyroomBot,
    TEHRAN_TZ,
    parse_time_string,
    PERSIAN_DAYS,
    next_occurrence,
)


# ============================================================
# Dialogs
# ============================================================
class UserDialog(tk.Toplevel):
    def __init__(self, parent, user=None):
        super().__init__(parent)
        self.title("Edit user" if user else "Add user")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None

        tk.Label(self, text="User name:").grid(row=0, column=0, padx=8, pady=8, sticky="e")
        self.name_var = tk.StringVar(value=user.get("user_name", "") if user else "")
        tk.Entry(self, textvariable=self.name_var, width=32).grid(
            row=0, column=1, padx=8, pady=8, sticky="we"
        )

        btns = tk.Frame(self)
        btns.grid(row=1, column=0, columnspan=2, pady=(0, 8))
        tk.Button(btns, text="OK", width=10, command=self._ok).pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", width=10, command=self.destroy).pack(side="left", padx=4)

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.wait_window(self)

    def _ok(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Error", "User name cannot be empty", parent=self)
            return
        self.result = name
        self.destroy()


class ClassDialog(tk.Toplevel):
    DAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه",
            "چهارشنبه", "پنجشنبه", "جمعه"]

    def __init__(self, parent, cls=None):
        super().__init__(parent)
        self.title("Edit class" if cls else "Add class")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None

        cls = cls or {}
        self.vars = {
            "name":     tk.StringVar(value=cls.get("name", "")),
            "url":      tk.StringVar(value=cls.get("url", "")),
            "day":      tk.StringVar(value=cls.get("day", self.DAYS[0])),
            "time":     tk.StringVar(value=cls.get("time", "10:00am")),
            "end_time": tk.StringVar(value=cls.get("end_time", "12:00pm")),
        }

        rows = [
            ("Name:",       "name",     "entry"),
            ("URL:",        "url",      "entry"),
            ("Day:",        "day",      "combo"),
            ("Start time:", "time",     "entry"),
            ("End time:",   "end_time", "entry"),
        ]
        for i, (label, key, kind) in enumerate(rows):
            tk.Label(self, text=label).grid(row=i, column=0, padx=8, pady=6, sticky="e")
            if kind == "combo":
                ttk.Combobox(self, textvariable=self.vars[key],
                             values=self.DAYS, width=30, state="readonly"
                             ).grid(row=i, column=1, padx=8, pady=6, sticky="we")
            else:
                tk.Entry(self, textvariable=self.vars[key], width=32).grid(
                    row=i, column=1, padx=8, pady=6, sticky="we"
                )

        tk.Label(self, text="Time format: 10:00am / 2:30pm / 22:30",
                 fg="gray").grid(row=len(rows), column=0, columnspan=2, pady=(0, 4))
        tk.Label(self, text="End time must be after start time (same day).",
                 fg="gray").grid(row=len(rows) + 1, column=0, columnspan=2, pady=(0, 4))

        btns = tk.Frame(self)
        btns.grid(row=len(rows) + 2, column=0, columnspan=2, pady=(0, 8))
        tk.Button(btns, text="OK", width=10, command=self._ok).pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", width=10, command=self.destroy).pack(side="left", padx=4)

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.wait_window(self)

    def _ok(self):
        name = self.vars["name"].get().strip()
        url = self.vars["url"].get().strip()
        day = self.vars["day"].get().strip()
        time_str = self.vars["time"].get().strip()
        end_str = self.vars["end_time"].get().strip()

        if not name:
            messagebox.showerror("Error", "Class name cannot be empty", parent=self)
            return
        if not url:
            messagebox.showerror("Error", "URL cannot be empty", parent=self)
            return
        if day not in PERSIAN_DAYS:
            messagebox.showerror("Error", f"Unknown day: {day}", parent=self)
            return
        try:
            start_t = parse_time_string(time_str)
        except ValueError as e:
            messagebox.showerror("Error", f"Start time: {e}", parent=self)
            return
        try:
            end_t = parse_time_string(end_str)
        except ValueError as e:
            messagebox.showerror("Error", f"End time: {e}", parent=self)
            return
        if end_t <= start_t:
            messagebox.showerror("Error",
                                 "End time must be after start time.",
                                 parent=self)
            return

        self.result = {
            "name": name,
            "url": url,
            "day": day,
            "time": time_str,
            "end_time": end_str,
        }
        self.destroy()


# ============================================================
# Main window
# ============================================================
class SkyroomGUI:
    SLEEP_MODES = [
        ("Suspend (S3) - fast resume, small battery drain", "mem"),
        ("Hibernate (S4) - no battery drain, slower resume", "disk"),
        ("Power off (S5) - needs BIOS RTC; auto-start won't work", "off"),
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Skyroom Bot Manager")
        self.root.geometry("1300x820")
        self.root.minsize(1050, 680)

        self.users: list = []
        self.json_path: Path | None = None
        self.bot: SkyroomBot | None = None
        self.bot_thread: threading.Thread | None = None
        self.log_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()

        self.cfg = RuntimeConfig()
        self._init_vars()

        self._build_menu()
        self._build_layout()
        self._refresh_tree()

        self._on_debug_toggle()
        self._on_send_message_toggle()
        self.refresh_next_wake()

        self.root.after(100, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------
    def _init_vars(self):
        c = self.cfg
        self.v_chromedriver    = tk.StringVar(value=c.chromedriver_path)
        self.v_send_message    = tk.BooleanVar(value=c.send_message)
        self.v_chat_message    = tk.StringVar(value=c.chat_message)
        self.v_page_timeout    = tk.IntVar(value=c.page_load_timeout)
        self.v_elem_timeout    = tk.IntVar(value=c.element_timeout)
        self.v_ws_interval     = tk.IntVar(value=c.ws_check_interval)
        self.v_ws_grace        = tk.IntVar(value=c.ws_dead_grace)
        self.v_max_restarts    = tk.IntVar(value=c.max_restarts)
        self.v_min_delay       = tk.IntVar(value=c.min_delay_min)
        self.v_max_delay       = tk.IntVar(value=c.max_delay_min)

        self.v_debug_mode      = tk.BooleanVar(value=c.debug_mode)
        self.v_debug_run_now   = tk.BooleanVar(value=c.debug_run_now)
        self.v_debug_no_delay  = tk.BooleanVar(value=c.debug_disable_random_delay)
        self.v_debug_no_ws     = tk.BooleanVar(value=c.debug_disable_ws_check)

        self.v_status          = tk.StringVar(value="Idle")
        self.v_json_path       = tk.StringVar(value="(no file loaded)")

        # ---- wake tab ----
        self.v_sleep_mode      = tk.StringVar(value="disk")
        self.v_wake_lead       = tk.IntVar(value=3)
        self.v_next_wake       = tk.StringVar(value="(no schedule loaded)")
        self.v_auto_after_wake = tk.BooleanVar(value=True)

        self.v_debug_mode.trace_add("write", lambda *_: self._on_debug_toggle())
        self.v_wake_lead.trace_add("write", lambda *_: self.refresh_next_wake())
        self.v_send_message.trace_add("write",
                                      lambda *_: self._on_send_message_toggle())

    def _on_debug_toggle(self):
        state = "normal" if self.v_debug_mode.get() else "disabled"
        for w in (self.cb_run_now, self.cb_no_delay, self.cb_no_ws):
            w.configure(state=state)

    def _on_send_message_toggle(self):
        entry = getattr(self, "entry_chat_message", None)
        if entry is not None:
            entry.configure(
                state="normal" if self.v_send_message.get() else "disabled"
            )

    # ---------------------------------------------------------
    def _build_menu(self):
        m = tk.Menu(self.root)

        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="Load users JSON…", command=self.load_json)
        fm.add_command(label="Save users JSON",   command=self.save_json)
        fm.add_command(label="Save users JSON as…", command=self.save_json_as)
        fm.add_separator()
        fm.add_command(label="Exit", command=self._on_close)
        m.add_cascade(label="File", menu=fm)

        hm = tk.Menu(m, tearoff=0)
        hm.add_command(label="About", command=self._about)
        m.add_cascade(label="Help", menu=hm)

        self.root.config(menu=m)

    # ---------------------------------------------------------
    def _build_layout(self):
        top = tk.Frame(self.root, bd=1, relief="raised")
        top.pack(side="top", fill="x")
        tk.Button(top, text="Load JSON", command=self.load_json).pack(side="left", padx=4, pady=4)
        tk.Button(top, text="Save JSON", command=self.save_json).pack(side="left", padx=4, pady=4)
        tk.Label(top, textvariable=self.v_json_path, fg="gray").pack(
            side="left", padx=12, pady=4
        )

        action = tk.Frame(self.root, bd=1, relief="raised")
        action.pack(side="bottom", fill="x")
        self.btn_start = tk.Button(action, text="▶ Start", width=10,
                                   command=self.start_bot, bg="#d9ead3")
        self.btn_start.pack(side="left", padx=6, pady=6)
        self.btn_stop = tk.Button(action, text="■ Stop", width=10,
                                  command=self.stop_bot, bg="#f4cccc",
                                  state="disabled")
        self.btn_stop.pack(side="left", padx=6, pady=6)
        tk.Label(action, textvariable=self.v_status, anchor="w").pack(
            side="left", padx=16, pady=6
        )

        paned = tk.PanedWindow(self.root, sashrelief="raised", sashwidth=6)
        paned.pack(side="top", fill="both", expand=True)

        # ----- left: tree -----
        left = tk.Frame(paned, bd=1, relief="sunken")
        paned.add(left, minsize=500, width=650)

        tk.Label(left, text="Users & Classes", font=("", 10, "bold")).pack(
            anchor="w", padx=6, pady=(4, 0)
        )

        tree_frame = tk.Frame(left)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=6)

        cols = ("name", "day", "start", "end", "url")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="tree headings")
        self.tree.heading("#0",    text="User / Class")
        self.tree.heading("name",  text="Class name")
        self.tree.heading("day",   text="Day")
        self.tree.heading("start", text="Start (Tehran)")
        self.tree.heading("end",   text="End (Tehran)")
        self.tree.heading("url",   text="URL")
        self.tree.column("#0",    width=150, anchor="w")
        self.tree.column("name",  width=220, anchor="w")
        self.tree.column("day",   width=80,  anchor="w")
        self.tree.column("start", width=100, anchor="w")
        self.tree.column("end",   width=100, anchor="w")
        self.tree.column("url",   width=280, anchor="w")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<Delete>",   lambda e: self.delete_selected())
        self.tree.bind("<Button-3>", self._on_tree_rightclick)

        self.ctx = tk.Menu(self.root, tearoff=0)
        self.ctx.add_command(label="Add class to this user", command=self.add_class)
        self.ctx.add_command(label="Edit",   command=self.edit_selected)
        self.ctx.add_command(label="Delete", command=self.delete_selected)

        lb = tk.Frame(left)
        lb.pack(fill="x", padx=6, pady=(0, 6))
        tk.Button(lb, text="Add user",  command=self.add_user).pack(side="left", padx=2)
        tk.Button(lb, text="Add class", command=self.add_class).pack(side="left", padx=2)
        tk.Button(lb, text="Edit",      command=self.edit_selected).pack(side="left", padx=2)
        tk.Button(lb, text="Delete",    command=self.delete_selected).pack(side="left", padx=2)

        # ----- right: notebook -----
        right = tk.Frame(paned, bd=1, relief="sunken")
        paned.add(right, minsize=420)

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True, padx=4, pady=4)

        self._build_general_tab(nb)
        self._build_wake_tab(nb)
        self._build_debug_tab(nb)
        self._build_log_tab(nb)

    # ---------------------------------------------------------
    def _build_general_tab(self, nb):
        f = tk.Frame(nb, padx=12, pady=12)
        nb.add(f, text="General")

        row = 0

        tk.Label(f, text="Chromedriver path:").grid(row=row, column=0, sticky="e", padx=6, pady=4)
        tk.Entry(f, textvariable=self.v_chromedriver, width=42).grid(
            row=row, column=1, sticky="we", padx=6, pady=4)
        row += 1

        tk.Label(f, text="Chat message:").grid(row=row, column=0, sticky="e", padx=6, pady=4)
        tk.Checkbutton(f, text="Send chat message on join",
                       variable=self.v_send_message).grid(
            row=row, column=1, sticky="w", padx=6, pady=4)
        row += 1

        tk.Label(f, text="  Message text:").grid(row=row, column=0, sticky="e", padx=6, pady=4)
        self.entry_chat_message = tk.Entry(f, textvariable=self.v_chat_message, width=42)
        self.entry_chat_message.grid(row=row, column=1, sticky="we", padx=6, pady=4)
        row += 1

        numeric_rows = [
            ("Page load timeout (s):", self.v_page_timeout),
            ("Element timeout (s):",   self.v_elem_timeout),
            ("WS check interval (s):", self.v_ws_interval),
            ("WS dead grace (s):",     self.v_ws_grace),
            ("Max restarts:",          self.v_max_restarts),
            ("Random delay min (min):", self.v_min_delay),
            ("Random delay max (min):", self.v_max_delay),
        ]
        for label, var in numeric_rows:
            tk.Label(f, text=label).grid(row=row, column=0, sticky="e", padx=6, pady=4)
            tk.Entry(f, textvariable=var, width=42).grid(
                row=row, column=1, sticky="we", padx=6, pady=4)
            row += 1

        f.columnconfigure(1, weight=1)

        tk.Label(
            f,
            text=("All schedule times are interpreted in Asia/Tehran (Iran) "
                  "timezone. If Start is pressed after the class has begun, "
                  "the bot joins immediately (as long as end_time hasn't "
                  "passed). Sessions close automatically at 'end_time'."),
            fg="gray", justify="left", wraplength=420,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 0))

    # ---------------------------------------------------------
    def _build_wake_tab(self, nb):
        f = tk.Frame(nb, padx=12, pady=12)
        nb.add(f, text="Wake")

        tk.Label(f, text="Wake & Sleep Automation",
                 font=("", 11, "bold")).pack(anchor="w")

        tk.Label(
            f, fg="gray", justify="left", wraplength=420,
            text=("Arm an RTC wake and put the laptop to sleep. When it wakes "
                  "up, the same GUI resumes and (if enabled) starts the bot "
                  "automatically. Works with Suspend and Hibernate — not with "
                  "Power off."),
        ).pack(anchor="w", pady=(0, 10))

        tk.Label(f, text="Sleep mode:", font=("", 9, "bold")).pack(anchor="w")
        for text, val in self.SLEEP_MODES:
            tk.Radiobutton(f, text=text, variable=self.v_sleep_mode,
                           value=val, anchor="w", justify="left"
                           ).pack(anchor="w", padx=14)

        row = tk.Frame(f)
        row.pack(anchor="w", fill="x", pady=(10, 0))
        tk.Label(row, text="Wake lead (min before class):").pack(side="left")
        tk.Spinbox(row, from_=0, to=60, textvariable=self.v_wake_lead,
                   width=5).pack(side="left", padx=6)
        tk.Label(row, text="(3 min is plenty on an SSD)",
                 fg="gray").pack(side="left")

        tk.Label(f, text="Next wake time:",
                 font=("", 9, "bold")).pack(anchor="w", pady=(12, 0))
        tk.Label(f, textvariable=self.v_next_wake,
                 fg="#2a7f42", font=("", 12, "bold")
                 ).pack(anchor="w")

        tk.Button(f, text="Refresh",
                  command=self.refresh_next_wake
                  ).pack(anchor="w", pady=(4, 0))

        tk.Frame(f, height=1, bd=1, relief="sunken").pack(fill="x", pady=12)

        self.cb_auto_after_wake = tk.Checkbutton(
            f, text="Auto-start the bot after the laptop wakes up",
            variable=self.v_auto_after_wake,
        )
        self.cb_auto_after_wake.pack(anchor="w")

        self.btn_arm_sleep = tk.Button(
            f, text="💤  Arm Wake & Sleep Now",
            command=self.arm_wake_and_sleep,
            bg="#ffe599", activebackground="#ffd966",
            font=("", 11, "bold"), height=2,
        )
        self.btn_arm_sleep.pack(fill="x", pady=(14, 4))

        tk.Button(
            f, text="Setup wake permission (one-time)",
            command=self.setup_wake_permission,
        ).pack(anchor="w", pady=(4, 0))

        tk.Label(
            f, fg="gray", justify="left", wraplength=420,
            text=("If you get a 'sudo required' error, click the setup button "
                  "above and run the shown command once in a terminal."),
        ).pack(anchor="w", pady=(8, 0))

    # ---------------------------------------------------------
    def _build_debug_tab(self, nb):
        f = tk.Frame(nb, padx=12, pady=12)
        nb.add(f, text="Debug")

        tk.Label(f, text="Debug Master Switch",
                 font=("", 10, "bold")).pack(anchor="w")

        tk.Checkbutton(
            f, text="Enable Debug Mode (master)",
            variable=self.v_debug_mode,
            font=("", 10, "bold"),
        ).pack(anchor="w", pady=(4, 2))

        tk.Label(
            f, fg="gray", justify="left", wraplength=400,
            text=("When Debug Mode is OFF, all sub-options below are ignored "
                  "and the bot behaves normally. When ON, the sub-options "
                  "take effect."),
        ).pack(anchor="w", pady=(0, 12))

        self.cb_run_now = tk.Checkbutton(
            f, text="Run now (ignore schedule entirely)",
            variable=self.v_debug_run_now,
        )
        self.cb_run_now.pack(anchor="w")

        self.cb_no_delay = tk.Checkbutton(
            f, text="Disable +min..+max random delay",
            variable=self.v_debug_no_delay,
        )
        self.cb_no_delay.pack(anchor="w")

        self.cb_no_ws = tk.Checkbutton(
            f, text="Disable WebSocket watchdog (never restart on WS drop)",
            variable=self.v_debug_no_ws,
        )
        self.cb_no_ws.pack(anchor="w")

    def _build_log_tab(self, nb):
        f = tk.Frame(nb)
        nb.add(f, text="Log")

        bar = tk.Frame(f)
        bar.pack(side="top", fill="x")
        tk.Button(bar, text="Clear", command=self.clear_log).pack(side="left", padx=4, pady=4)
        self.v_autoscroll = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="Autoscroll", variable=self.v_autoscroll).pack(
            side="left", padx=4
        )

        self.log_text = tk.Text(f, wrap="word", height=20, state="disabled",
                                bg="#111", fg="#eee", insertbackground="#eee",
                                font=("monospace", 9))
        sb = tk.Scrollbar(f, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.log_text.tag_configure("INFO",    foreground="#d0e8ff")
        self.log_text.tag_configure("WARNING", foreground="#ffcc66")
        self.log_text.tag_configure("ERROR",   foreground="#ff8080")
        self.log_text.tag_configure("DEBUG",   foreground="#a0a0a0")

    # =========================================================
    # JSON I/O
    # =========================================================
    def load_json(self):
        p = filedialog.askopenfilename(
            title="Select users JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not p:
            return
        self._load_json_from_path(Path(p))

    def _load_json_from_path(self, path: Path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            messagebox.showerror("Error", f"Could not read JSON:\n{e}")
            return
        if not isinstance(data, list):
            messagebox.showerror("Error", "Top-level JSON must be a list of users.")
            return
        for u in data:
            if not isinstance(u, dict) or "user_name" not in u:
                messagebox.showerror("Error", "Each user needs a 'user_name' key.")
                return
            u.setdefault("classes", [])
        self.users = data
        self.json_path = path
        self.v_json_path.set(str(self.json_path))
        self._refresh_tree()
        self.refresh_next_wake()
        self._log("INFO", f"Loaded {len(self.users)} user(s) from {self.json_path}")

    def save_json(self):
        if self.json_path is None:
            return self.save_json_as()
        try:
            self.json_path.write_text(
                json.dumps(self.users, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )
            self._log("INFO", f"Saved {self.json_path}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save:\n{e}")

    def save_json_as(self):
        p = filedialog.asksaveasfilename(
            title="Save users JSON",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not p:
            return
        self.json_path = Path(p)
        self.v_json_path.set(str(self.json_path))
        self.save_json()

    # =========================================================
    # Tree operations
    # =========================================================
    def _refresh_tree(self):
        for iid in self.tree.get_children(""):
            self.tree.delete(iid)

        self._tree_map = {}

        for ui, user in enumerate(self.users):
            uid = f"u{ui}"
            self.tree.insert(
                "", "end", iid=uid, text=user.get("user_name", "?"),
                values=("", "", "", "", ""), open=True,
            )
            self._tree_map[uid] = ("user", user, None)
            for ci, cls in enumerate(user.get("classes", [])):
                cid = f"{uid}-c{ci}"
                self.tree.insert(
                    uid, "end", iid=cid, text="",
                    values=(
                        cls.get("name", ""),
                        cls.get("day", ""),
                        cls.get("time", ""),
                        cls.get("end_time", ""),
                        cls.get("url", ""),
                    ),
                )
                self._tree_map[cid] = ("class", user, cls)

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return sel[0], self._tree_map.get(sel[0])

    def add_user(self):
        dlg = UserDialog(self.root)
        if dlg.result is None:
            return
        self.users.append({"user_name": dlg.result, "classes": []})
        self._refresh_tree()
        self._log("INFO", f"Added user '{dlg.result}'")

    def add_class(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("Info", "Select a user first.")
            return
        _kind, user, _cls = sel[1]
        dlg = ClassDialog(self.root)
        if dlg.result is None:
            return
        user.setdefault("classes", []).append(dlg.result)
        self._refresh_tree()
        self.refresh_next_wake()
        self._log("INFO", f"Added class '{dlg.result['name']}' to {user['user_name']}")

    def edit_selected(self):
        sel = self._selected()
        if not sel:
            return
        _iid, (kind, user, cls) = sel

        if kind == "user":
            dlg = UserDialog(self.root, user=user)
            if dlg.result is not None:
                old = user["user_name"]
                user["user_name"] = dlg.result
                self._refresh_tree()
                self._log("INFO", f"Renamed user '{old}' → '{dlg.result}'")
        else:
            dlg = ClassDialog(self.root, cls=cls)
            if dlg.result is not None:
                cls.update(dlg.result)
                self._refresh_tree()
                self.refresh_next_wake()
                self._log("INFO", f"Edited class '{cls['name']}'")

    def delete_selected(self):
        sel = self._selected()
        if not sel:
            return
        _iid, (kind, user, cls) = sel
        if kind == "user":
            if not messagebox.askyesno("Confirm",
                                       f"Delete user '{user['user_name']}' "
                                       f"and all their classes?"):
                return
            self.users.remove(user)
            self._log("INFO", f"Deleted user '{user['user_name']}'")
        else:
            if not messagebox.askyesno("Confirm",
                                       f"Delete class '{cls['name']}'?"):
                return
            user["classes"].remove(cls)
            self._log("INFO", f"Deleted class '{cls['name']}'")
        self._refresh_tree()
        self.refresh_next_wake()

    def _on_tree_double(self, _event):
        self.edit_selected()

    def _on_tree_rightclick(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            kind, _u, _c = self._tree_map[iid]
            state = "normal" if kind == "user" else "disabled"
            self.ctx.entryconfigure(0, state=state)
            self.ctx.tk_popup(event.x_root, event.y_root)

    # =========================================================
    # Bot control
    # =========================================================
    def _apply_gui_to_config(self) -> RuntimeConfig:
        return RuntimeConfig(
            chromedriver_path=self.v_chromedriver.get().strip(),
            send_message=bool(self.v_send_message.get()),
            chat_message=self.v_chat_message.get().strip() or "سلام",
            page_load_timeout=int(self.v_page_timeout.get()),
            element_timeout=int(self.v_elem_timeout.get()),
            ws_check_interval=int(self.v_ws_interval.get()),
            ws_dead_grace=int(self.v_ws_grace.get()),
            max_restarts=int(self.v_max_restarts.get()),
            min_delay_min=int(self.v_min_delay.get()),
            max_delay_min=int(self.v_max_delay.get()),
            debug_mode=bool(self.v_debug_mode.get()),
            debug_run_now=bool(self.v_debug_run_now.get()),
            debug_disable_random_delay=bool(self.v_debug_no_delay.get()),
            debug_disable_ws_check=bool(self.v_debug_no_ws.get()),
        )

    def start_bot(self):
        if self.bot is not None:
            messagebox.showinfo("Info", "Bot is already running.")
            return
        if not self.users:
            messagebox.showinfo("Info", "Load a users JSON file first.")
            return
        cfg = self._apply_gui_to_config().effective()

        if not Path(cfg.chromedriver_path).is_file():
            messagebox.showerror(
                "Error",
                f"chromedriver not found at:\n{cfg.chromedriver_path}\n\n"
                f"Fix the path in the General tab.",
            )
            return

        self.bot = SkyroomBot(cfg, self.users, self._bot_log)
        try:
            tasks = self.bot.start()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start bot:\n{e}")
            self.bot = None
            return

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.v_status.set(f"Running: {len(tasks)} task(s)")

        self._log("INFO", f"=== Bot started with {len(tasks)} task(s) ===")
        self._log("INFO",
                  f"    chat message: "
                  f"{'enabled' if cfg.send_message else 'DISABLED'}")
        for t in tasks:
            # `actual_time` is finalised inside the worker; we just show the
            # class window here.
            self._log(
                "INFO",
                f"  • {t.user_name}@{t.class_name}  "
                f"class {t.scheduled_time:%a %Y-%m-%d %H:%M}"
                f"-{t.end_time:%H:%M} Tehran",
            )

        self.bot_thread = threading.Thread(target=self._watch_bot, daemon=True)
        self.bot_thread.start()

    def _watch_bot(self):
        if self.bot is None:
            return
        for t in list(self.bot.threads):
            t.join()
        self.root.after(0, self._bot_finished)

    def _bot_finished(self):
        self.bot = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.v_status.set("Idle")
        self._log("INFO", "=== Bot finished all tasks ===")

    def stop_bot(self):
        if self.bot is None:
            return
        self.v_status.set("Stopping…")
        self._log("WARNING", "Stop requested - closing browsers and threads…")
        bot = self.bot
        threading.Thread(target=self._stop_bot_bg, args=(bot,), daemon=True).start()

    def _stop_bot_bg(self, bot: SkyroomBot):
        try:
            bot.stop()
        finally:
            self.root.after(0, self._bot_finished)
            self._log("INFO", "Bot stopped by user")

    # =========================================================
    # Wake scheduling
    # =========================================================
    def _get_lead_minutes(self) -> int:
        try:
            v = int(self.v_wake_lead.get())
        except Exception:
            return 3
        return max(0, min(60, v))

    def _compute_next_wake(self):
        if not self.users:
            return None, None
        soonest = None
        for u in self.users:
            for cls in u.get("classes", []):
                try:
                    dt = next_occurrence(cls["day"], cls["time"])
                except Exception:
                    continue
                if soonest is None or dt < soonest:
                    soonest = dt
        if soonest is None:
            return None, None
        wake_at = soonest - timedelta(minutes=self._get_lead_minutes())
        return soonest, wake_at

    def refresh_next_wake(self):
        class_start, wake_at = self._compute_next_wake()
        if class_start is None:
            self.v_next_wake.set("(no valid classes in schedule)")
            return
        now = datetime.now(TEHRAN_TZ)
        if wake_at <= now:
            self.v_next_wake.set(
                f"{wake_at:%a %Y-%m-%d %H:%M} — already passed "
                f"(next class {class_start:%H:%M})"
            )
            return
        delta = wake_at - now
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        self.v_next_wake.set(
            f"{wake_at:%a %Y-%m-%d %H:%M:%S}  (in {h}h {m}m)"
        )

    def arm_wake_and_sleep(self):
        if not self.users:
            messagebox.showinfo("Info", "Load a users JSON first.")
            return

        class_start, wake_at = self._compute_next_wake()
        if wake_at is None:
            messagebox.showerror("Error", "No valid classes in schedule.")
            return
        now = datetime.now(TEHRAN_TZ)
        if wake_at <= now:
            messagebox.showerror(
                "Error",
                f"Computed wake time is already in the past:\n"
                f"{wake_at:%Y-%m-%d %H:%M:%S}\n\n"
                f"Adjust the schedule or the lead time.",
            )
            return

        if self.bot is not None:
            if not messagebox.askyesno(
                "Bot is running",
                "The bot is currently running. Stop it, arm wake, and sleep?"
            ):
                return
            self.stop_bot()
            for _ in range(40):
                if self.bot is None:
                    break
                time.sleep(0.25)

        mode = self.v_sleep_mode.get()
        mode_name = {"mem": "suspend", "disk": "hibernate", "off": "power off"}[mode]
        warn_off = ("\n\nNote: 'Power off' shuts the machine down entirely — "
                    "the GUI won't auto-restart. You'll need to boot manually."
                    if mode == "off" else "")

        if not messagebox.askyesno(
            "Confirm",
            f"About to {mode_name} now.\n\n"
            f"Class starts: {class_start:%a %Y-%m-%d %H:%M:%S} Tehran\n"
            f"Wake at:      {wake_at:%a %Y-%m-%d %H:%M:%S} Tehran\n\n"
            f"Continue?{warn_off}"
        ):
            return

        epoch = int(wake_at.timestamp())
        cmd = ["sudo", "-n", "rtcwake", "-m", mode, "-t", str(epoch)]
        self._log("INFO", f"Arming wake: {' '.join(cmd)}")
        self._log("INFO",
                  f"Class {class_start:%a %H:%M} -> waking at {wake_at:%H:%M:%S}")

        self.btn_arm_sleep.configure(state="disabled")
        threading.Thread(target=self._do_rtcwake,
                         args=(cmd,), daemon=True).start()

    def _do_rtcwake(self, cmd):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except Exception as e:
            self.root.after(0, lambda err=str(e): self._on_rtcwake_error(err))
            return

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            self.root.after(0, lambda e=err: self._on_rtcwake_error(e))
            return

        self.root.after(0, self._on_woke_up)

    def _on_rtcwake_error(self, msg: str):
        self.btn_arm_sleep.configure(state="normal")
        self._log("ERROR", f"rtcwake failed: {msg}")
        low = (msg or "").lower()
        if ("sudo" in low or "password" in low
                or "a terminal is required" in low
                or "no tty present" in low):
            messagebox.showerror(
                "Sudo required",
                "rtcwake needs passwordless sudo.\n\n"
                "Click 'Setup wake permission (one-time)' in the Wake tab, "
                "then run the command it shows in a terminal.\n\n"
                "Or run this once manually:\n\n"
                "  echo \"$USER ALL=(ALL) NOPASSWD: /usr/bin/rtcwake\" | "
                "sudo tee /etc/sudoers.d/skyroom-rtcwake\n"
                "  sudo chmod 440 /etc/sudoers.d/skyroom-rtcwake\n",
            )
        else:
            messagebox.showerror("rtcwake failed", msg or "(no output)")

    def _on_woke_up(self):
        self.btn_arm_sleep.configure(state="normal")
        self._log("INFO", "=== Resumed from sleep ===")
        self.refresh_next_wake()
        if self.v_auto_after_wake.get():
            self._log("INFO", "Auto-start after wake is enabled; "
                              "launching bot in 5s…")
            self.root.after(5000, self._auto_start_after_wake)
        else:
            self._log("INFO", "Auto-start after wake is disabled.")

    def _auto_start_after_wake(self):
        if self.bot is not None:
            self._log("INFO", "Bot already running; skipping auto-start.")
            return
        if not self.users:
            self._log("WARNING", "No schedule loaded; cannot auto-start.")
            return
        self._log("INFO", "Auto-starting bot now.")
        self.start_bot()

    def setup_wake_permission(self):
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "$USER"

        try:
            r = subprocess.run(["sudo", "-n", "-l", "/usr/bin/rtcwake"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and "NOPASSWD" in (r.stdout or ""):
                messagebox.showinfo(
                    "Already set up",
                    "Passwordless rtcwake is already configured.",
                )
                return
        except Exception:
            pass

        cmd = (f"echo '{user} ALL=(ALL) NOPASSWD: /usr/bin/rtcwake' | "
               f"sudo tee /etc/sudoers.d/skyroom-rtcwake && "
               f"sudo chmod 440 /etc/sudoers.d/skyroom-rtcwake && "
               f"sudo -k")

        dlg = tk.Toplevel(self.root)
        dlg.title("Setup wake permission (one-time)")
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(dlg, text="Run this once in a terminal:",
                 anchor="w", font=("", 10, "bold")
                 ).pack(anchor="w", padx=12, pady=(12, 4))

        txt = tk.Text(dlg, height=5, width=72, wrap="word",
                      font=("monospace", 9))
        txt.insert("1.0", cmd)
        txt.configure(state="disabled")
        txt.pack(padx=12, pady=4)

        row = tk.Frame(dlg)
        row.pack(pady=(0, 12))

        def copy_cmd():
            self.root.clipboard_clear()
            self.root.clipboard_append(cmd)
            self._log("INFO", "Setup command copied to clipboard.")

        tk.Button(row, text="Copy to clipboard", command=copy_cmd).pack(side="left", padx=4)
        tk.Button(row, text="Close", command=dlg.destroy).pack(side="left", padx=4)

        dlg.wait_window()

    # =========================================================
    # Logging
    # =========================================================
    def _bot_log(self, level: str, message: str):
        self.log_queue.put((level, message))

    def _drain_log(self):
        try:
            while True:
                level, message = self.log_queue.get_nowait()
                self._append_log(level, message)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def _append_log(self, level: str, message: str):
        ts = time.strftime("%H:%M:%S")
        line = f"{ts} | {level:<7} | {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line, level)
        self.log_text.configure(state="disabled")
        if self.v_autoscroll.get():
            self.log_text.see("end")

    def _log(self, level: str, message: str):
        self._append_log(level, message)

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # =========================================================
    def _about(self):
        messagebox.showinfo(
            "About",
            "Skyroom Bot Manager\n\n"
            "Schedules incognito Chrome sessions per user/class.\n"
            "Times are interpreted in Asia/Tehran timezone.\n"
            "Sessions close automatically at their end_time.\n\n"
            "Wake tab: arms an RTC wake + hibernates, then auto-starts the bot.",
        )

    def _on_close(self):
        if self.bot is not None:
            if not messagebox.askyesno("Quit",
                                       "Bot is running. Stop it and quit?"):
                return
            self.bot.stop(join_timeout=5)
        self.root.destroy()


# ============================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = SkyroomGUI(root)

    if "--json" in sys.argv:
        idx = sys.argv.index("--json")
        if idx + 1 < len(sys.argv):
            p = Path(sys.argv[idx + 1])
            if p.is_file():
                app._load_json_from_path(p)

    if "--auto-start" in sys.argv and app.users:
        root.after(1000, app.start_bot)

    root.mainloop()