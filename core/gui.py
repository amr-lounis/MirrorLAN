#!/usr/bin/env python3
"""Modern dark Tkinter control panel: port, Start/Stop, copy addresses."""
from __future__ import annotations

import os
import tkinter as tk

from .config import APP_DIR, Config
from .server import ServerError, ServerManager

BG = "#0f1115"
CARD = "#171a21"
FIELD = "#0c0e12"
FG = "#eef1f6"
MUTED = "#8b93a7"
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
        self._urls: list = []
        root.title("MirrorLAN")
        try:
            root.iconbitmap(os.path.join(APP_DIR, "app.ico"))
        except Exception:
            pass
        root.geometry("480x640")
        root.resizable(False, False)
        root.configure(bg=BG)

        # -- header ------------------------------------------------------
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=18, pady=(16, 6))
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left")
        tk.Label(titles, text="MirrorLAN", font=TITLE, bg=BG, fg=FG).pack(anchor="w")
        tk.Label(titles, text="LAN screen share  •  offline  •  http",
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
        row.pack(fill="x", pady=(8, 4))
        tk.Label(row, text="Port", font=FONT, bg=CARD, fg=FG).pack(side="left")
        self.port = tk.Entry(row, width=8, font=FONT, bg=FIELD, fg=FG,
                             insertbackground=FG, relief="flat",
                             highlightthickness=1, highlightbackground=BORDER)
        self.port.insert(0, str(self.config.port))
        self.port.pack(side="left", padx=(8, 0), ipady=4)
        tk.Label(row, text="share from localhost", font=SMALL, bg=CARD,
                 fg=MUTED).pack(side="right")
        row2 = tk.Frame(card, bg=CARD)
        row2.pack(fill="x", pady=(0, 10))
        tk.Label(row2, text="TURN", font=FONT, bg=CARD, fg=FG).pack(side="left")
        self.turn_port = tk.Entry(row2, width=8, font=FONT, bg=FIELD, fg=FG,
                                  insertbackground=FG, relief="flat",
                                  highlightthickness=1, highlightbackground=BORDER)
        self.turn_port.insert(0, str(self.config.turn_port))
        self.turn_port.pack(side="left", padx=(8, 0), ipady=4)
        self.relay_state = tk.Label(row2, text="relay: UDP :%s (0 = off)" % self.config.turn_port,
                                    font=SMALL, bg=CARD, fg=MUTED)
        self.relay_state.pack(side="right")
        self.btn_power = tk.Button(card, text="Start Server", font=("Segoe UI", 11, "bold"),
                                   bg=GREEN, fg="#04120a", activebackground=GREEN_DARK,
                                   activeforeground="#04120a", relief="flat",
                                   cursor="hand2", command=self.toggle)
        self.btn_power.pack(fill="x", ipady=7)

        # -- addresses card ----------------------------------------------
        # Fixed-height scroll area: any number of LAN addresses fits
        # without growing the window (canvas height ~= 4 rows).
        card2 = self._card(root)
        tk.Label(card2, text="ADDRESSES", font=SMALL, bg=CARD, fg=MUTED).pack(anchor="w")
        self._addr_wrap = tk.Frame(card2, bg=CARD)
        self._addr_wrap.pack(fill="x", pady=(8, 0))
        self.addr_canvas = tk.Canvas(self._addr_wrap, bg=CARD, highlightthickness=0,
                                     height=176)
        self.addr_scroll = tk.Scrollbar(self._addr_wrap, orient="vertical",
                                        command=self.addr_canvas.yview,
                                        bg=CARD, troughcolor=FIELD,
                                        activebackground=BORDER, relief="flat",
                                        borderwidth=0, width=12)
        self.addr_canvas.configure(yscrollcommand=self._on_canvas_scroll)
        self.addr_canvas.pack(side="left", fill="both", expand=True)
        self.rows = tk.Frame(self.addr_canvas, bg=CARD)
        self._rows_win = self.addr_canvas.create_window((0, 0), window=self.rows,
                                                        anchor="nw")
        self.rows.bind("<Configure>", self._on_rows_configure)
        self.addr_canvas.bind("<Configure>", self._on_canvas_configure)
        self.empty = tk.Label(self.rows, text="No addresses yet — start the server.",
                              font=FONT, bg=CARD, fg=MUTED)
        self.empty.pack(pady=14)
        self._hook_wheel()

        # -- footer ------------------------------------------------------
        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", padx=18, pady=10)
        tk.Button(foot, text="Copy All", font=FONT, bg=CARD, fg=FG,
                  activebackground=BORDER, activeforeground=FG, relief="flat",
                  highlightthickness=1, highlightbackground=BORDER,
                  cursor="hand2", padx=12, pady=4,
                  command=self.copy_all).pack(side="left")
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
        self.turn_port.config(state="disabled" if running else "normal")
        self.btn_power.config(
            text="Stop Server" if running else "Start Server",
            bg=RED if running else GREEN,
            activebackground=RED_DARK if running else GREEN_DARK,
            fg="#ffffff" if running else "#04120a",
            activeforeground="#ffffff" if running else "#04120a")
        self.set_status("Running" if running else "Stopped",
                        GREEN if running else MUTED)

    def _on_canvas_scroll(self, *args) -> None:
        self.addr_scroll.set(*args)
        self._update_scroll_visibility()

    def _on_rows_configure(self, _event=None) -> None:
        try:
            self.addr_canvas.configure(scrollregion=self.addr_canvas.bbox("all"))
        except Exception:
            pass
        self._update_scroll_visibility()

    def _on_canvas_configure(self, event=None) -> None:
        try:
            self.addr_canvas.itemconfig(self._rows_win, width=event.width)
        except Exception:
            pass
        self._update_scroll_visibility()

    def _update_scroll_visibility(self) -> None:
        # Hide the bar when everything fits; otherwise pin it right.
        try:
            self.root.update_idletasks()
            bbox = self.addr_canvas.bbox("all")
            fits = not bbox or (bbox[3] - bbox[1]) <= self.addr_canvas.winfo_height()
            if fits:
                if str(self.addr_scroll.winfo_manager()) == "pack":
                    self.addr_scroll.pack_forget()
                try:
                    self.addr_canvas.yview_moveto(0)
                except Exception:
                    pass
            else:
                if str(self.addr_scroll.winfo_manager()) != "pack":
                    self.addr_scroll.pack(side="right", fill="y")
        except Exception:
            pass

    def _on_mousewheel(self, event=None) -> str | None:
        try:
            delta = 0
            if event is not None and hasattr(event, "delta") and event.delta:
                delta = int(-event.delta / 120)
            elif event is not None and getattr(event, "num", None) in (4, 5):
                delta = -1 if event.num == 4 else 1
            if delta:
                self.addr_canvas.yview_scroll(delta, "units")
        except Exception:
            pass
        return "break"

    def _hook_wheel(self) -> None:
        # Route the wheel to the address list only while hovering it, so the
        # rest of the panel never scrolls by accident (Linux buttons too).
        try:
            self._addr_wrap.bind("<Enter>", lambda _e: (
                self.root.bind_all("<MouseWheel>", self._on_mousewheel),
                self.root.bind_all("<Button-4>", self._on_mousewheel),
                self.root.bind_all("<Button-5>", self._on_mousewheel)))
            self._addr_wrap.bind("<Leave>", lambda _e: (
                self.root.unbind_all("<MouseWheel>"),
                self.root.unbind_all("<Button-4>"),
                self.root.unbind_all("<Button-5>")))
        except Exception:
            pass

    def _rebuild_rows(self, urls: list) -> None:
        for widget in self.rows.winfo_children():
            if widget is not self.empty:
                widget.destroy()
        self._urls = list(urls)
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
        try:
            self.addr_canvas.yview_moveto(0)
        except Exception:
            pass
        try:
            self.root.update_idletasks()
            self.addr_canvas.configure(scrollregion=self.addr_canvas.bbox("all"))
        except Exception:
            pass
        self._update_scroll_visibility()

    def _paint_relay(self) -> None:
        if self.manager.running and self.manager.turn_ok and self.manager.turn is not None:
            self.relay_state.config(
                text="relay: on UDP :%d" % self.manager.turn.bound_port, fg=GREEN)
        elif self.manager.running:
            err = self.manager.turn_error or "off"
            self.relay_state.config(text="relay: off (%s)" % err, fg=RED)
        else:
            try:
                hint = self.turn_port.get().strip() or str(self.config.turn_port)
            except Exception:
                hint = str(self.config.turn_port)
            self.relay_state.config(text="relay: UDP :%s (0 = off)" % hint, fg=MUTED)

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
        raw_turn = self.turn_port.get().strip()
        if not (raw_turn.isdigit() and 0 <= int(raw_turn) <= 65535):
            self.say("Invalid TURN port (0-65535, 0 = off).", RED)
            return
        self.config.port = int(raw)
        self.config.turn_port = int(raw_turn)
        try:
            urls = self.manager.start()
        except (ValueError, ServerError) as exc:
            self.say(str(exc), RED)
            return
        self._rebuild_rows(urls)
        self._set_running(True)
        self._paint_relay()
        note = "" if self.manager.turn_ok else " (turn relay off)"
        self.say("Serving ./www on port %s%s. Share from localhost." % (raw, note), GREEN)

    def stop(self) -> None:
        self.manager.stop()
        self._set_running(False)
        self._paint_relay()
        self.say("Server stopped.")

    def copy_all(self) -> None:
        if not self._urls:
            self.say("No addresses yet - start the server first.", MUTED)
            return
        self._to_clipboard(self._urls)

    def _to_clipboard(self, lines: list) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.say("Copied %d address(es)." % len(lines), GREEN)

    def close(self) -> None:
        try:
            self.manager.stop()
        except Exception:
            pass
        self.root.destroy()
