#!/usr/bin/env python3
"""Entry point. Everything else lives in the core/ package.

Usage:
    python main.py              launch GUI (port, Start/Stop, copy addresses)
    python main.py --serve      run headless server (default port 443)
    python main.py --serve 8443 run headless server on custom port
    python main.py --serve 8443 --dir ./site   serve another folder

Layout:
    core/config.py     all settings in one Config dataclass
    core/certs.py      self-signed ECDSA certificates (stdlib only)
    core/net.py        local IPs and public URLs
    core/signaling.py  thread-safe viewer offer/answer store
    core/server.py     https server + http redirect + ServerManager
    core/gui.py        Tkinter control panel
    www/               served pages (Sharer.html, Viewer.html, ...)
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
            config.https_port = int(rest.pop(0))
    while rest:
        flag = rest.pop(0)
        if flag == "--dir" and rest:
            config.www_dir = os.path.abspath(rest.pop(0))
        elif flag == "--https-port" and rest:
            config.https_port = int(rest.pop(0))
        elif flag == "--http-port" and rest:
            config.http_port = int(rest.pop(0))
        else:
            raise ValueError("unknown argument: %s" % flag)
    config.validate()
    return mode, config


def run_headless(config: Config) -> int:
    """Start the server and block until Ctrl+C. Returns exit code."""
    from core.certs import ensure_cert_files
    from core.net import local_ips

    manager = ServerManager(config)
    if not (os.path.exists(config.cert_file) and os.path.exists(config.key_file)):
        ips = ["127.0.0.1"] + [ip for ip in local_ips() if ip != "127.0.0.1"]
        ensure_cert_files(config.cert_file, config.key_file,
                          list(config.dns_names), ips,
                          config.cert_days, config.common_name)
        _say("created cert.pem / key.pem")
    try:
        urls = manager.start()
    except ServerError as exc:
        _say("ERROR: %s" % exc)
        return 1
    _say("serving %s" % config.www_dir)
    for url in urls:
        _say(url + " (http redirects here)")
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
