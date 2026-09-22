"""Read plaintext SIP secrets from MikoPBX-generated ``pjsip.conf``.

Why this module exists:
    ``m_Sip.secret`` in the MikoPBX SQLite database is stored XOR-encrypted
    (values starting with ``E``/``S`` etc.). The plaintext password Asterisk
    actually uses for SIP digest auth is written to ``pjsip.conf`` at PBX
    config-regen time, in a section named ``[<EXT>-AUTH]`` with key
    ``password``. For a dockerized MikoPBX where ``/etc/asterisk`` is *not*
    bind-mounted to the host, we fetch the file via ``docker exec ... cat``.
    If the path is reachable on the host directly, we read it as a regular
    file (set ``mikopbx_docker_container`` to empty in ``config.yaml``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


_CACHE: dict = {"text": None, "ts": 0.0, "key": ""}
_LOCK = threading.Lock()

# Common docker locations — covers systemd units that ship a minimal PATH.
_DOCKER_CANDIDATES = ("/usr/bin/docker", "/usr/local/bin/docker", "/snap/bin/docker")


def _cache_key(path: str, container: str) -> str:
    return f"{container}|{path}"


def _resolve_docker() -> str:
    """Find a usable ``docker`` binary path (PATH may be empty under systemd)."""
    found = shutil.which("docker")
    if found:
        return found
    for candidate in _DOCKER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return ""


def _log(msg: str) -> None:
    try:
        print(f"[pjsip_secrets] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _read_pjsip_text(path: str, container: str, ttl_seconds: int) -> str:
    """Return raw contents of MikoPBX ``pjsip.conf`` (cached, best-effort).

    Empty string on any failure — failure reason is logged to stderr so it
    surfaces in ``journalctl -u mikopbx-cdr-proxy``.
    """
    if not path:
        _log("empty pjsip path — set mikopbx_pjsip_conf_path in config.yaml")
        return ""
    key = _cache_key(path, container)
    with _LOCK:
        now = time.time()
        if (
            _CACHE["text"] is not None
            and _CACHE["key"] == key
            and now - _CACHE["ts"] < ttl_seconds
        ):
            return _CACHE["text"]

        text = ""
        try:
            if container:
                docker_bin = _resolve_docker()
                if not docker_bin:
                    _log("docker binary not found in PATH or common locations")
                else:
                    env = dict(os.environ)
                    env.setdefault(
                        "PATH",
                        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    )
                    r = subprocess.run(
                        [docker_bin, "exec", "-i", container, "cat", path],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        env=env,
                    )
                    if r.returncode == 0:
                        text = r.stdout or ""
                    else:
                        _log(
                            f"docker exec failed rc={r.returncode} container={container!r} "
                            f"path={path!r} stderr={(r.stderr or '').strip()!r}"
                        )
            else:
                p = Path(path)
                if p.is_file():
                    text = p.read_text(encoding="utf-8", errors="replace")
                else:
                    _log(f"pjsip path not a file on host: {path!r}")
        except Exception as e:
            _log(f"exception reading pjsip.conf: {type(e).__name__}: {e}")
            text = ""

        _CACHE["text"] = text
        _CACHE["ts"] = now
        _CACHE["key"] = key
        return text


def invalidate_cache() -> None:
    """Drop the cached pjsip.conf text — useful in tests or after PBX changes."""
    with _LOCK:
        _CACHE["text"] = None
        _CACHE["ts"] = 0.0
        _CACHE["key"] = ""


def get_peer_secret(
    extension: str,
    *,
    path: str,
    container: str = "",
    cache_ttl_seconds: int = 60,
) -> str:
    """Return ``password`` from section ``[<EXT>-AUTH]`` in MikoPBX ``pjsip.conf``.

    Returns empty string when the file is unavailable, the section is missing,
    or no ``password=`` line is found in it.
    """
    extension = (extension or "").strip()
    if not extension:
        return ""

    text = _read_pjsip_text(path, container, max(0, int(cache_ttl_seconds)))
    if not text:
        return ""

    target = f"{extension}-AUTH".lower()
    in_target = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("["):
            # Section header. MikoPBX uses both ``[201-AUTH]`` and template-instantiation
            # forms like ``[201](aor-common)`` — name is between ``[`` and the first ``]``.
            end = line.find("]")
            if end <= 1:
                in_target = False
                continue
            name = line[1:end].strip()
            in_target = name.lower() == target
            continue
        if in_target and "=" in line:
            key, _, value = line.partition("=")
            if key.strip().lower() == "password":
                return value.strip()

    return ""
