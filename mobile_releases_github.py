"""Fetch GitHub Releases for private IntermarkCaller APK proxy."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

log = logging.getLogger("mobile_releases")

_TAG_VERSION_RE = re.compile(r"^v?(?P<version>\d+(?:\.\d+)*)$", re.I)

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class GithubReleaseAsset:
    asset_id: int
    name: str
    size: int
    content_type: str


@dataclass(frozen=True)
class GithubRelease:
    tag: str
    version: str
    name: str
    body: str
    published_at: str
    asset: GithubReleaseAsset


def parse_version_from_tag(tag: str) -> str:
    raw = (tag or "").strip()
    match = _TAG_VERSION_RE.match(raw)
    if match:
        return match.group("version")
    return raw.lstrip("v") or raw


def _cache_key(owner: str, repo: str) -> str:
    return f"{owner}/{repo}"


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


def _curl_ipv4_fallback_enabled() -> bool:
    """When httpx cannot connect (often broken IPv6 on VPS), retry with ``curl -4``."""
    raw = (os.environ.get("GITHUB_HTTP_FORCE_IPV4") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _curl_executable() -> str:
    explicit = (os.environ.get("CURL_BIN") or "").strip()
    if explicit:
        return explicit
    found = shutil.which("curl")
    if found:
        return found
    for candidate in ("/usr/bin/curl", "/bin/curl"):
        if os.path.isfile(candidate):
            return candidate
    raise RuntimeError("curl not found on PATH (required for GitHub API on this host)")


def _curl_github_cmd(
    url: str,
    token: str,
    *,
    accept: str,
    max_time: str = "30",
    write_out: str | None = "\n%{http_code}",
    follow: bool = False,
) -> list[str]:
    """curl with IPv4 + no proxy (systemd often sets a broken HTTPS_PROXY)."""
    cmd = [
        _curl_executable(),
        "-4",
        "-sS",
        "--noproxy",
        "*",
        "--max-time",
        max_time,
        "-H",
        f"Accept: {accept}",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        "-H",
        f"Authorization: Bearer {token}",
    ]
    if follow:
        cmd.append("-L")
    if write_out is not None:
        cmd.extend(["-w", write_out])
    cmd.append(url)
    return cmd


def _curl_failure_message(*, status: int, stderr: str, exit_code: int | None) -> str:
    hint = (
        "Check outbound access from this server: "
        "curl -4 --noproxy '*' -I https://api.github.com/"
    )
    detail = stderr.strip() or (f"exit {exit_code}" if exit_code else "")
    if status < 100:
        return f"Could not connect to GitHub API (curl HTTP {status:03d}). {hint}. {detail}".strip()
    return f"curl to GitHub failed: {detail or status}".strip()


async def _fetch_via_curl(url: str, token: str, *, accept: str) -> tuple[int, bytes]:
    cmd = _curl_github_cmd(url, token, accept=accept)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    err_text = stderr.decode("utf-8", errors="replace")
    if not stdout:
        raise RuntimeError(_curl_failure_message(status=0, stderr=err_text, exit_code=proc.returncode))
    if b"\n" not in stdout:
        raise RuntimeError(_curl_failure_message(status=0, stderr=err_text or stdout[:200].decode(errors="replace"), exit_code=proc.returncode))
    body, status_raw = stdout.rsplit(b"\n", 1)
    try:
        status = int(status_raw.decode().strip())
    except ValueError as exc:
        raise RuntimeError("curl to GitHub returned invalid HTTP status") from exc
    if proc.returncode != 0 or status < 100:
        raise RuntimeError(_curl_failure_message(status=status, stderr=err_text, exit_code=proc.returncode))
    return status, body


def _parse_github_json(status: int, body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace")
    if status == 404:
        raise LookupError(
            "GitHub release not found (no published release, wrong owner/repo, or token cannot read the repo)"
        )
    if status < 100:
        raise RuntimeError(
            f"Could not connect to GitHub API (HTTP {status:03d}). "
            "Run on server: curl -4 --noproxy '*' -I https://api.github.com/"
        )
    if status >= 400:
        raise RuntimeError(f"GitHub API HTTP {status}: {text[:200]}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GitHub returned non-JSON (HTTP {status}): {text[:120]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("GitHub response was not a JSON object")
    return data


def _release_from_payload(data: dict[str, Any], asset_name: str) -> GithubRelease:
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        raise LookupError("Release has no tag_name")

    assets = data.get("assets") or []
    wanted = (asset_name or "IGCaller.apk").strip()
    asset_row: Optional[dict[str, Any]] = None
    for row in assets:
        if str(row.get("name") or "") == wanted:
            asset_row = row
            break
    if asset_row is None and assets:
        asset_row = assets[0]
    if asset_row is None or not asset_row.get("id"):
        raise LookupError(f"Release asset '{wanted}' not found")

    asset = GithubReleaseAsset(
        asset_id=int(asset_row["id"]),
        name=str(asset_row.get("name") or wanted),
        size=int(asset_row.get("size") or 0),
        content_type=str(asset_row.get("content_type") or "application/vnd.android.package-archive"),
    )
    return GithubRelease(
        tag=tag,
        version=parse_version_from_tag(tag),
        name=str(data.get("name") or tag),
        body=str(data.get("body") or ""),
        published_at=str(data.get("published_at") or ""),
        asset=asset,
    )


async def _fetch_release_json(owner: str, repo: str, token: str, url: str) -> dict[str, Any]:
    clean_token = (token or "").strip()
    if not clean_token:
        raise RuntimeError("GitHub token is not configured")
    headers = _github_headers(clean_token)
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            response = await client.get(url, headers=headers)
    except httpx.ConnectError as exc:
        if not _curl_ipv4_fallback_enabled():
            raise RuntimeError(f"GitHub request failed: {exc}") from exc
        log.warning("GitHub httpx connect failed (%s), retrying via curl -4", exc)
        status, body = await _fetch_via_curl(
            url,
            clean_token,
            accept=headers["Accept"],
        )
        return _parse_github_json(status, body)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"GitHub request failed: {exc}") from exc
    return _parse_github_json(response.status_code, response.content)


async def fetch_latest_release(
    owner: str,
    repo: str,
    token: str,
    asset_name: str,
) -> GithubRelease:
    key = _cache_key(owner, repo)
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        data = cached[1]
    else:
        url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
        data = await _fetch_release_json(owner, repo, token, url)
        _cache[key] = (now, data)
    return _release_from_payload(data, asset_name)


async def fetch_release_by_tag(
    owner: str,
    repo: str,
    token: str,
    tag: str,
    asset_name: str,
) -> GithubRelease:
    clean_tag = (tag or "").strip()
    if not clean_tag:
        raise LookupError("Release tag is required")
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{clean_tag}"
    data = await _fetch_release_json(owner, repo, token, url)
    return _release_from_payload(data, asset_name)


async def stream_release_asset(
    owner: str,
    repo: str,
    token: str,
    asset_id: int,
):
    headers = {
        "Accept": "application/octet-stream",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}"
    clean_token = (token or "").strip()

    try:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=30.0),
            follow_redirects=True,
            trust_env=False,
        )
        request = client.build_request("GET", url, headers=headers)
        response = await client.send(request, stream=True)
    except httpx.ConnectError as exc:
        if not _curl_ipv4_fallback_enabled():
            raise RuntimeError(f"GitHub asset request failed: {exc}") from exc
        log.warning("GitHub asset httpx connect failed (%s), streaming via curl -4", exc)
        return await _stream_asset_via_curl(url, clean_token, headers["Accept"])

    if response.status_code >= 400:
        body = (await response.aread())[:200]
        await response.aclose()
        await client.aclose()
        raise RuntimeError(f"GitHub asset HTTP {response.status_code}: {body!r}")

    async def _iter():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return response, _iter()


async def _stream_asset_via_curl(url: str, token: str, accept: str):
    cmd = _curl_github_cmd(
        url,
        token,
        accept=accept,
        max_time="600",
        write_out=None,
        follow=True,
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    class _CurlResponse:
        headers: dict[str, str] = {}

    async def _iter():
        try:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                await proc.wait()
            if proc.returncode not in (0, None):
                err = ""
                if proc.stderr is not None:
                    err = (await proc.stderr.read()).decode("utf-8", errors="replace")[:200]
                raise RuntimeError(f"curl asset download failed: {err or proc.returncode}")

    return _CurlResponse(), _iter()
