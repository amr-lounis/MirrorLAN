#!/usr/bin/env python3
"""Entry point. Everything else lives in the core/ package.

Plain HTTP only (no TLS). Browsers allow screen capture solely from
http://localhost, so sharing works from this PC; LAN devices can watch.

Usage:
    python main.py              launch GUI (port, Start/Stop, copy addresses)
    python main.py --serve      run headless server (default port 80)
    python main.py --serve 8081 run headless server on custom port
    python main.py --serve 8081 --dir ./site   serve another folder
    python main.py --serve --turn-port 0       disable the TURN relay

Layout:
    core/config.py     all settings in one Config dataclass
    core/net.py        local IPs and public URLs
    core/signaling.py  thread-safe viewer offer/answer store
    core/turn.py       TURN/UDP relay fallback (stdlib only)
    core/server.py     http server + ServerManager
    core/gui.py        Tkinter control panel
    www/               served pages (myshares.html, Viewer.html, ...)
"""
from __future__ import annotations

import os
import sys
import threading

from core.config import Config
from core.server import ServerError, ServerManager


def _say(text: str) -> None:
    """Print that never crashes when there is no console (windowed exe)."""
    try:
        print(text, flush=True)
    except Exception:
        pass


def parse_args(argv: list) -> tuple:
    """Split CLI into (mode, Config). Mode is 'gui' or 'serve'."""
    config = Config()
    mode = "gui"
    rest = list(argv)
    if rest and rest[0] == "--serve":
        mode = "serve"
        rest = rest[1:]
        if rest and not rest[0].startswith("--"):
            config.port = int(rest.pop(0))
    while rest:
        flag = rest.pop(0)
        if flag == "--dir" and rest:
            config.www_dir = os.path.abspath(rest.pop(0))
        elif flag == "--port" and rest:
            config.port = int(rest.pop(0))
        elif flag == "--turn-port" and rest:
            config.turn_port = int(rest.pop(0))
        else:
            raise ValueError("unknown argument: %s" % flag)
    config.validate()
    return mode, config


def run_headless(config: Config) -> int:
    """Start the server and block until Ctrl+C. Returns exit code."""
    manager = ServerManager(config)
    try:
        urls = manager.start()
    except ServerError as exc:
        _say("ERROR: %s" % exc)
        return 1
    _say("serving %s" % config.www_dir)
    for url in urls:
        _say(url)
    _say("share from http://localhost:%d/ (browsers block capture on LAN http)"
         % config.port)
    if manager.turn_ok and manager.turn is not None:
        _say("turn relay on udp :%d" % manager.turn.bound_port)
    elif config.turn_port:
        _say("turn relay off (%s)" % (manager.turn_error or "bind failed"))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    manager.stop()
    return 0


def run_gui(config: Config) -> int:
    """Open the desktop control panel."""
    import tkinter as tk

    from core.gui import ServerGui

    root = tk.Tk()
    ServerGui(root, config)
    root.mainloop()
    return 0


def main(argv: list | None = None) -> int:
    try:
        mode, config = parse_args(sys.argv[1:] if argv is None else argv)
    except ValueError as exc:
        _say("ERROR: %s" % exc)
        return 2
    if mode == "serve":
        return run_headless(config)
    return run_gui(config)


if __name__ == "__main__":
    raise SystemExit(main())
