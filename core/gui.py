#!/usr/bin/env python3
"""Modern dark Tkinter control panel: port, Start/Stop, copy addresses."""
from __future__ import annotations

import os
import tkinter as tk

from .certs import ensure_cert_files
from .config import Config
from .net import local_ips
from .server import ServerError, ServerManager

BG = "#0f1115"
CARD = "#171a21"
FIELD = "#0c0e12"
FG = "#eef1f6"
MUTED = "#8b93a7"
ACCENT = "#4f7cff"
GREEN = "#22c55e"
GREEN_DARK = "#16a34a"
RED = "#ef4444"
RED_DARK = "#dc2626"
BORDER = "#2a2f3a"
FONT = ("Segoe UI", 10)
SMALL = ("Segoe UI", 9)
TITLE = ("Segoe UI", 15, "bold")
SUB = ("Segoe UI", 9)


class ServerGui:
    """Dark control panel driving a ServerManager."""

    def __init__(self, root: tk.Tk, config: Config | None = None) -> None:
        self.root = root
        self.config = config or Config()
        self.manager = ServerManager(self.config)
        self._rows: list = []
        root.title("MirrorLAN")
        try:
            from .config import APP_DIR
            root.iconbitmap(os.path.join(APP_DIR, "app.ico"))
        except Exception:
            pass
        root.geometry("480x600")
        root.resizable(False, False)
        root.configure(bg=BG)

        # -- header ------------------------------------------------------
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=18, pady=(16, 6))
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left")
        tk.Label(titles, text="MirrorLAN", font=TITLE, bg=BG, fg=FG).pack(anchor="w")
        tk.Label(titles, text="HTTPS screen share  •  offline",
                 font=SUB, bg=BG, fg=MUTED).pack(anchor="w")
        pill = tk.Frame(head, bg=CARD, highlightthickness=1, highlightbackground=BORDER,
                        padx=10, pady=5)
        pill.pack(side="right", anchor="n")
        self.dot = tk.Canvas(pill, width=10, height=10, bg=CARD,
                             highlightthickness=0)
        self.dot.pack(side="left")
        self._dot_id = self.dot.create_oval(1, 1, 9, 9, fill=MUTED, outline="")
        self.status = tk.Label(pill, text="Stopped", font=SMALL, bg=CARD, fg=MUTED)
        self.status.pack(side="left", padx=(6, 0))

        # -- server card -------------------------------------------------
        card = self._card(root)
        tk.Label(card, text="SERVER", font=SMALL, bg=CARD, fg=MUTED).pack(anchor="w")
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", pady=(8, 10))
        tk.Label(row, text="Port", font=FONT, bg=CARD, fg=FG).pack(side="left")
        self.port = tk.Entry(row, width=8, font=FONT, bg=FIELD, fg=FG,
                             insertbackground=FG, relief="flat",
                             highlightthickness=1, highlightbackground=BORDER)
        self.port.insert(0, str(self.config.https_port))
        self.port.pack(side="left", padx=(8, 0), ipady=4)
        tk.Label(row, text="443 needs admin", font=SMALL, bg=CARD,
                 fg=MUTED).pack(side="right")
        self.btn_power = tk.Button(card, text="Start Server", font=("Segoe UI", 11, "bold"),
                                   bg=GREEN, fg="#04120a", activebackground=GREEN_DARK,
                                   activeforeground="#04120a", relief="flat",
                                   cursor="hand2", command=self.toggle)
        self.btn_power.pack(fill="x", ipady=7)

        # -- addresses card ----------------------------------------------
        card2 = self._card(root)
        tk.Label(card2, text="ADDRESSES", font=SMALL, bg=CARD, fg=MUTED).pack(anchor="w")
        self.rows = tk.Frame(card2, bg=CARD)
        self.rows.pack(fill="both", expand=True, pady=(8, 0))
        self.empty = tk.Label(self.rows, text="No addresses yet — start the server.",
                              font=FONT, bg=CARD, fg=MUTED)
        self.empty.pack(pady=14)

        # -- footer ------------------------------------------------------
        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", padx=18, pady=10)
        tk.Button(foot, text="Copy All", font=FONT, bg=CARD, fg=FG,
                  activebackground=BORDER, activeforeground=FG, relief="flat",
                  highlightthickness=1, highlightbackground=BORDER,
                  cursor="hand2", padx=12, pady=4,
                  command=self.copy_all).pack(side="left")
        tk.Button(foot, text="Make Cert", font=FONT, bg=CARD, fg=FG,
                  activebackground=BORDER, activeforeground=FG, relief="flat",
                  highlightthickness=1, highlightbackground=BORDER,
                  cursor="hand2", padx=12, pady=4,
                  command=self.make_cert).pack(side="right")
        self.msg = tk.Label(root, text="Ready.", font=SMALL, bg=BG, fg=MUTED,
                            wraplength=440, justify="left")
        self.msg.pack(anchor="w", padx=18, pady=(0, 12))

        root.protocol("WM_DELETE_WINDOW", self.close)

    # -- widgets ---------------------------------------------------------
    def _card(self, parent: tk.Misc) -> tk.Frame:
        card = tk.Frame(parent, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER, padx=14, pady=12)
        card.pack(fill="x", padx=18, pady=8)
        return card

    def say(self, text: str, color: str = MUTED) -> None:
        self.msg.config(text=text, fg=color)

    def set_status(self, text: str, color: str) -> None:
        self.status.config(text=text, fg=color)
        self.dot.itemconfig(self._dot_id, fill=color)

    def _set_running(self, running: bool) -> None:
        self.port.config(state="disabled" if running else "normal")
        self.btn_power.config(
            text="Stop Server" if running else "Start Server",
            bg=RED if running else GREEN,
            activebackground=RED_DARK if running else GREEN_DARK,
            fg="#ffffff" if running else "#04120a",
            activeforeground="#ffffff" if running else "#04120a")
        self.set_status("Running" if running else "Stopped",
                        GREEN if running else MUTED)

    def _rebuild_rows(self, urls: list) -> None:
        for widget in self.rows.winfo_children():
            if widget is not self.empty:
                widget.destroy()
        self._rows = list(urls)
        if urls:
            self.empty.pack_forget()
        else:
            self.empty.pack(pady=14)
        for url in urls:
            row = tk.Frame(self.rows, bg=FIELD, padx=10, pady=6)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=url, font=("Consolas", 9), bg=FIELD,
                     fg="#9db4ff").pack(side="left", fill="x", expand=True)
            tk.Button(row, text="Copy", font=SMALL, bg=CARD, fg=FG,
                      activebackground=BORDER, activeforeground=FG,
                      relief="flat", cursor="hand2", padx=8,
                      command=lambda u=url: self._to_clipboard([u])).pack(side="right")

    # -- actions ---------------------------------------------------------
    def toggle(self) -> None:
        if self.manager.running:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self.manager.running:
            return
        raw = self.port.get().strip()
        if not (raw.isdigit() and 1 <= int(raw) <= 65535):
            self.say("Invalid port (1-65535).", RED)
            return
        self.config.https_port = int(raw)
        try:
            urls = self.manager.start()
        except (ValueError, ServerError) as exc:
            self.say(str(exc), RED)
            return
        self._rebuild_rows(urls)
        self._set_running(True)
        note = "" if self.manager.redirect_ok else " (http redirect off)"
        self.say("Serving ./www on port %s%s." % (raw, note), GREEN)

    def stop(self) -> None:
        self.manager.stop()
        self._set_running(False)
        self.say("Server stopped.")

    def copy_all(self) -> None:
        if not self._rows:
            self.say("No addresses yet - start the server first.", MUTED)
            return
        self._to_clipboard(self._rows)

    def _to_clipboard(self, lines: list) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.say("Copied %d address(es)." % len(lines), GREEN)

    def make_cert(self) -> None:
        try:
            ips = ["127.0.0.1"] + [ip for ip in local_ips() if ip != "127.0.0.1"]
            ensure_cert_files(self.config.cert_file, self.config.key_file,
                              list(self.config.dns_names), ips,
                              self.config.cert_days, self.config.common_name)
            self.say("Certificate ready.", GREEN)
        except Exception as exc:
            self.say("Cert failed: %s" % exc, RED)

    def close(self) -> None:
        try:
            self.manager.stop()
        except Exception:
            pass
        self.root.destroy()
