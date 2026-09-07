#!/usr/bin/env python3
"""Public surface of the core package."""
from .config import BASE_DIR, Config
from .certs import ensure_cert_files, generate_self_signed
from .net import local_ips, server_urls
from .server import ServerError, ServerManager, build_manager
from .signaling import SignalingStore

__version__ = "2.0.0"
__all__ = [
    "BASE_DIR",
    "Config",
    "ServerError",
    "ServerManager",
    "SignalingStore",
    "build_manager",
    "ensure_cert_files",
    "generate_self_signed",
    "local_ips",
    "server_urls",
]
