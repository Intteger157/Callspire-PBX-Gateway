"""Reachability checks from the proxy host (TCP/TLS), not full SIP/WebSocket handshake."""

from __future__ import annotations

import socket
import ssl
from urllib.parse import urlparse


def _tcp_connect(host: str, port: int, *, timeout: float = 4.0) -> None:
    sock = socket.create_connection((host, int(port)), timeout=timeout)
    sock.close()


def check_ami_tcp(host: str, port: int | str, *, timeout: float = 4.0) -> tuple[bool, str]:
    """Try TCP to AMI and read a short banner (Asterisk Call Manager)."""
    host = (host or "").strip() or "127.0.0.1"
    try:
        p = int(port)
    except (TypeError, ValueError):
        p = 5038
    try:
        sock = socket.create_connection((host, p), timeout=timeout)
        sock.settimeout(3.0)
        try:
            banner = sock.recv(512)
        except OSError:
            banner = b""
        sock.close()
        if banner and (b"Asterisk" in banner or b"Call Manager" in banner or b"Manager" in banner):
            return True, "AMI port reachable (Asterisk banner received)."
        if banner:
            return True, "TCP port open (unexpected banner; check AMI on this host/port)."
        return True, "TCP port open (no banner yet; firewall OK)."
    except OSError as e:
        return False, str(e) or "Connection refused or timed out."


def check_wss_tls(ws_url: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    """TLS + TCP to WSS host:port. Does not complete WebSocket upgrade (browser will)."""
    ws_url = (ws_url or "").strip()
    if not ws_url:
        return False, "WSS URL is empty."

    parsed = urlparse(ws_url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("ws", "wss"):
        return False, "Use ws:// or wss://"

    host = parsed.hostname
    if not host:
        return False, "Invalid URL: missing host."

    port = parsed.port
    if port is None:
        port = 443 if scheme == "wss" else 80

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        if scheme == "wss":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.close()
        return True, f"TLS/TCP to {host}:{port} OK (path not verified; browser still does WebSocket)."
    except ssl.SSLError as e:
        return False, f"TLS error: {e}"
    except OSError as e:
        return False, str(e) or "Connection failed."
