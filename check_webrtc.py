#!/usr/bin/env python3
"""Diagnose browser WebRTC readiness: MikoPBX pjsip, gateway config, app-user mapping.

Run on the gateway host (same machine as callspire-pbx-gateway service)::

    cd /opt/callspire-pbx-gateway
    source venv/bin/activate
    python3 check_webrtc.py
    python3 check_webrtc.py --extension 202
    python3 check_webrtc.py --extension 202 --verbose

Exit code 0 = all critical checks passed; 1 = at least one failure.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from config import load_config
from connection_check import check_wss_tls
import permissions_db
from pjsip_secrets import get_peer_secret, invalidate_cache


_AUTH_SECTION_RE = re.compile(r"^\[([^\]]+-AUTH)\]", re.IGNORECASE)
_DOCKER_DB_CANDIDATES = (
    "/cf/conf/mikopbx.db",
    "/storage/usbdisk1/mikopbx/cf/conf/mikopbx.db",
)


class _Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, msg: str) -> None:
        print(f"  OK   {msg}")

    def warn(self, msg: str) -> None:
        self.warnings += 1
        print(f"  WARN {msg}")

    def fail(self, msg: str) -> None:
        self.failures += 1
        print(f"  FAIL {msg}")

    def info(self, msg: str) -> None:
        print(f"       {msg}")


def _section(title: str) -> None:
    print(f"\n== {title} ==")


def _docker_bin() -> str:
    found = shutil.which("docker")
    if found:
        return found
    for candidate in ("/usr/bin/docker", "/usr/local/bin/docker", "/snap/bin/docker"):
        if Path(candidate).is_file():
            return candidate
    return ""


def _docker_exec(container: str, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    docker = _docker_bin()
    if not docker:
        raise RuntimeError("docker binary not found")
    return subprocess.run(
        [docker, "exec", container, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_pjsip(cfg: dict) -> str:
    path = (cfg.get("mikopbx_pjsip_conf_path") or "/etc/asterisk/pjsip.conf").strip()
    container = (cfg.get("mikopbx_docker_container") or "").strip()
    invalidate_cache()
    if container:
        r = _docker_exec(container, "cat", path)
        if r.returncode != 0:
            raise RuntimeError(
                f"docker exec {container} cat {path} failed: {(r.stderr or r.stdout or '').strip()}"
            )
        return r.stdout or ""
    p = Path(path)
    if not p.is_file():
        raise RuntimeError(f"pjsip.conf not found on host: {path}")
    return p.read_text(encoding="utf-8", errors="replace")


def _parse_auth_sections(pjsip_text: str) -> dict[str, str]:
    """Return {section_name: has_password} for every *-AUTH block."""
    sections: dict[str, bool] = {}
    current: str | None = None
    has_pw = False
    for raw in pjsip_text.splitlines():
        line = raw.strip()
        m = _AUTH_SECTION_RE.match(line)
        if m:
            if current is not None:
                sections[current] = has_pw
            current = m.group(1)
            has_pw = False
            continue
        if current and line.lower().startswith("password="):
            has_pw = bool(line.partition("=")[2].strip())
    if current is not None:
        sections[current] = has_pw
    return sections


def _sqlite_peers(db_path: str) -> list[tuple[str, str]]:
    if not db_path or not Path(db_path).is_file():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT extension, description FROM m_Sip "
            "WHERE type='peer' AND disabled='0' ORDER BY extension"
        ).fetchall()
        return [(str(r[0]), str(r[1] or "")) for r in rows]
    finally:
        conn.close()


def _docker_mikopbx_db_peers(container: str) -> tuple[str, list[tuple[str, str]]]:
    for db_path in _DOCKER_DB_CANDIDATES:
        r = _docker_exec(container, "test", "-f", db_path, timeout=5.0)
        if r.returncode != 0:
            continue
        r2 = _docker_exec(
            container,
            "sqlite3",
            db_path,
            "SELECT extension, description FROM m_Sip WHERE type='peer' AND disabled='0' ORDER BY extension;",
            timeout=15.0,
        )
        if r2.returncode != 0:
            continue
        peers: list[tuple[str, str]] = []
        for line in (r2.stdout or "").splitlines():
            if "|" not in line:
                continue
            ext, _, desc = line.partition("|")
            peers.append((ext.strip(), desc.strip()))
        return db_path, peers
    return "", []


def _auth_id_for_extension(ext: str) -> tuple[str, str]:
    ext = (ext or "").strip()
    if not ext:
        return "", ext
    if ext.upper().endswith("-WS"):
        return ext, ext[:-3] if ext.upper().endswith("-WS") else ext
    return f"{ext}-WS", ext


async def _resolve_password(cfg: dict, extension: str) -> tuple[str, str]:
    import app as gw  # noqa: WPS433 — optional heavy import after config load

    return await gw.resolve_webrtc_sip_password_and_auth_user(extension)


def run_checks(*, extension: str | None, verbose: bool) -> int:
    rep = _Report()
    cfg = load_config()
    permissions_db.init_db()

    _section("Gateway config")
    container = (cfg.get("mikopbx_docker_container") or "").strip()
    pjsip_path = (cfg.get("mikopbx_pjsip_conf_path") or "/etc/asterisk/pjsip.conf").strip()
    config_db = (cfg.get("config_db_path") or "").strip()
    use_rest = bool(cfg.get("use_rest_api"))

    rep.info(f"config_db_path: {config_db or '(empty)'}")
    rep.info(f"mikopbx_docker_container: {container or '(empty — read pjsip from host)'}")
    rep.info(f"mikopbx_pjsip_conf_path: {pjsip_path}")
    rep.info(f"use_rest_api: {use_rest}")

    if not container:
        rep.warn("mikopbx_docker_container not set — set it to 'mikopbx' when PBX runs in Docker")
    if not use_rest:
        rep.warn(
            "use_rest_api is false — PBX Users in admin may come from stale host SQLite; "
            "enable REST API for accurate extension list"
        )

    _section("Browser WebRTC URLs (gateway admin → Browser calling)")
    wrtc = permissions_db.get_webrtc_public_config()
    ws_url = (wrtc.get("ws_url") or "").strip()
    sip_host = (wrtc.get("sip_host") or "").strip()
    if ws_url:
        rep.ok(f"WSS URL: {ws_url}")
    else:
        rep.fail("WSS URL is empty — set in admin → Settings → Browser calling")
    if sip_host and sip_host not in ("pbx.example.com", "example.com"):
        rep.ok(f"SIP host: {sip_host}")
    elif sip_host:
        rep.fail(f"SIP host looks like a placeholder: {sip_host}")
    else:
        rep.fail("SIP host is empty — set in admin → Settings → Browser calling")

    if ws_url:
        ok, detail = check_wss_tls(ws_url)
        if ok:
            rep.ok(f"WSS TCP/TLS: {detail}")
        else:
            rep.fail(f"WSS TCP/TLS: {detail}")

    _section("MikoPBX pjsip.conf (live Asterisk credentials)")
    try:
        pjsip_text = _read_pjsip(cfg)
        rep.ok(f"Read pjsip.conf ({len(pjsip_text)} bytes)")
    except Exception as exc:
        rep.fail(str(exc))
        print(f"\nResult: {rep.failures} failure(s), {rep.warnings} warning(s)")
        return 1

    auth_sections = _parse_auth_sections(pjsip_text)
    desk = sorted(k for k in auth_sections if k.upper().endswith("-AUTH") and "-WS-" not in k.upper())
    webrtc = sorted(k for k in auth_sections if k.upper().endswith("-WS-AUTH"))

    if webrtc:
        rep.ok(f"WebRTC auth sections (*-WS-AUTH): {len(webrtc)}")
        if verbose:
            for name in webrtc:
                flag = "password set" if auth_sections.get(name) else "NO password"
                rep.info(f"  {name} ({flag})")
    else:
        rep.fail(
            "No *-WS-AUTH sections in pjsip.conf — enable Web Phone for at least one "
            "extension in MikoPBX, then Apply config"
        )

    if verbose and desk:
        rep.info(f"Desk phone auth sections (*-AUTH, no -WS-): {len(desk)}")
        for name in desk[:20]:
            rep.info(f"  {name}")

    _section("Extension list: host SQLite vs Docker MikoPBX")
    host_peers = _sqlite_peers(config_db)
    docker_db_path = ""
    docker_peers: list[tuple[str, str]] = []
    if container:
        try:
            docker_db_path, docker_peers = _docker_mikopbx_db_peers(container)
        except Exception as exc:
            rep.warn(f"Could not read MikoPBX DB from docker: {exc}")

    if host_peers:
        rep.info(f"Host config_db ({config_db}): {len(host_peers)} peer(s)")
        if verbose:
            for ext, desc in host_peers:
                rep.info(f"  {ext}  {desc}")
    else:
        rep.warn(f"Host config_db empty or missing: {config_db}")

    if docker_peers:
        rep.ok(f"Docker mikopbx.db ({docker_db_path}): {len(docker_peers)} peer(s)")
        if verbose:
            for ext, desc in docker_peers:
                rep.info(f"  {ext}  {desc}")
    elif container:
        rep.warn("Could not read peer list from docker mikopbx.db (sqlite3 missing in container?)")

    host_exts = {e for e, _ in host_peers}
    docker_exts = {e for e, _ in docker_peers}
    if host_exts and docker_exts and host_exts != docker_exts:
        rep.fail(
            "Host config_db and Docker mikopbx.db list DIFFERENT extensions — "
            "gateway admin shows wrong numbers; enable use_rest_api or fix config_db_path"
        )
        only_host = sorted(host_exts - docker_exts)
        only_docker = sorted(docker_exts - host_exts)
        if only_host:
            rep.info(f"  only on host: {', '.join(only_host)}")
        if only_docker:
            rep.info(f"  only in docker: {', '.join(only_docker)}")

    live_exts = docker_exts or host_exts

    _section("App users (/softphone login → extension mapping)")
    users = permissions_db.list_app_users()
    if not users:
        rep.warn("No app users in permissions.db — create in admin → Softphone accounts")
    for u in users:
        email = u.get("email") or ""
        ext = (u.get("mikopbx_extension") or "").strip()
        disabled = bool(u.get("disabled"))
        if disabled:
            rep.warn(f"{email} → ext {ext} (disabled)")
            continue
        ws_id, _ = _auth_id_for_extension(ext)
        ws_section = f"{ws_id}-AUTH".upper()
        desk_section = f"{ext}-AUTH".upper()
        has_ws = any(k.upper() == ws_section for k in auth_sections)
        has_desk = any(k.upper() == desk_section for k in auth_sections)
        in_db = ext in live_exts if live_exts else None

        if in_db is False:
            rep.fail(f"{email} → ext {ext} NOT in live MikoPBX peer list")
        elif has_ws:
            rep.ok(f"{email} → ext {ext} (WebRTC section {ws_id}-AUTH present)")
        elif has_desk:
            rep.warn(
                f"{email} → ext {ext} has desk *-AUTH but no *-WS-AUTH — enable Web Phone in MikoPBX"
            )
        else:
            rep.fail(f"{email} → ext {ext} not found in pjsip.conf auth sections")

    if extension:
        _section(f"SIP password resolution for extension {extension}")
        ext = extension.strip()
        ws_id, _ = _auth_id_for_extension(ext)
        path = pjsip_path
        cont = container
        cache = int(cfg.get("mikopbx_pjsip_cache_seconds") or 60)
        for auth_id in (ws_id, ext):
            pw = get_peer_secret(auth_id, path=path, container=cont, cache_ttl_seconds=cache)
            if pw:
                rep.ok(f"pjsip secret for {auth_id}-AUTH: found ({len(pw)} chars)")
                break
            rep.info(f"pjsip secret for {auth_id}-AUTH: not found")
        else:
            rep.fail(f"No SIP password in pjsip.conf for {ws_id} or {ext}")

        try:
            pw, auth_user = asyncio.run(_resolve_password(cfg, ext))
            if pw:
                rep.ok(f"gateway resolve_webrtc_sip_password_and_auth_user: auth={auth_user}")
            else:
                rep.fail("gateway resolve_webrtc_sip_password_and_auth_user: password empty")
        except Exception as exc:
            rep.fail(f"gateway resolve_webrtc_sip_password_and_auth_user: {exc}")

    print(f"\nResult: {rep.failures} failure(s), {rep.warnings} warning(s)")
    if rep.failures:
        print("Fix failures above, then re-run: python3 check_webrtc.py --extension <ext>")
        return 1
    if rep.warnings:
        print("Warnings present — review before production use.")
    else:
        print("All critical checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check MikoPBX WebRTC readiness for web softphone")
    parser.add_argument(
        "-e",
        "--extension",
        help="Also verify SIP password resolution for this MikoPBX extension (e.g. 202)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Print extension lists and auth sections")
    args = parser.parse_args()
    return run_checks(extension=args.extension, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
