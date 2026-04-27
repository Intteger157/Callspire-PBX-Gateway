"""Async AMI (Asterisk Manager Interface) client for MikoPBX.

Connects via TCP to port 5038, authenticates, and sends an Originate action
so the PBX calls the user's extension first, then bridges to the destination
with the chosen CallerID.
"""

import asyncio
import uuid


async def ami_originate(
    config: dict,
    extension: str,
    destination: str,
    callerid: str,
    originate_id: str | None = None,
    timeout: float = 10.0,
) -> dict:
    """Issue an AMI Originate command.

    Parameters
    ----------
    config : dict
        Keys: host, port, username, secret (from permissions_db.get_ami_config).
    extension : str
        Internal extension to ring first (e.g. "204").
    destination : str
        External number to call after the extension answers.
    callerid : str
        Outbound CallerID to present (e.g. "+441156612771").
    originate_id : str, optional
        Correlation token sent as ``X-Callspire-Originate`` SIP header so the
        softphone can auto-answer the PBX callback.  Generated if omitted.
    timeout : float
        Seconds to wait for each AMI response.

    Returns
    -------
    dict  ``{success: bool, originate_id: str, error: str | None}``
    """
    if originate_id is None:
        originate_id = uuid.uuid4().hex[:16]

    host = config.get("host", "127.0.0.1")
    port = int(config.get("port", 5038))
    username = config.get("username", "")
    secret = config.get("secret", "")

    if not username or not secret:
        return {"success": False, "originate_id": originate_id, "error": "AMI credentials not configured"}

    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )

        # AMI sends a banner line on connect (e.g. "Asterisk Call Manager/5.0.2")
        await asyncio.wait_for(reader.readline(), timeout=timeout)

        # --- Login ---
        login_cmd = (
            f"Action: Login\r\n"
            f"Username: {username}\r\n"
            f"Secret: {secret}\r\n"
            f"\r\n"
        )
        writer.write(login_cmd.encode())
        await writer.drain()

        login_resp = await _read_response(reader, timeout)
        if "success" not in login_resp.lower():
            return {"success": False, "originate_id": originate_id, "error": f"AMI login failed: {login_resp.strip()}"}

        # --- Originate ---
        # MikoPBX internal-originate is a 1C-style dialplan.
        #
        # In many installations:
        # - pt1c_cid is (ab)used as both "destination number" and CallerID,
        #   because the dialplan does: Set(CALLERID(num)=${pt1c_cid})
        # - pt1c_dst is an optional explicit destination override:
        #   ORIGINATE_DST_EXTEN = IF(pt1c_dst set, pt1c_dst, pt1c_cid)
        #
        # If we pass pt1c_cid=destination, CallerID becomes destination, and PBX
        # outbound rules can fail ("cannot be completed as dialed").
        #
        # Therefore we pass:
        #   pt1c_dst = destination
        #   pt1c_cid = callerid
        #
        # Additionally we set OUTGOING_CID (and __OUTGOING_CID) to make trunk
        # caller-id override explicit for other dialplan variants.
        originate_cmd = (
            f"Action: Originate\r\n"
            f"Channel: Local/{extension}@internal-originate\r\n"
            f"Context: all_peers\r\n"
            f"Exten: {destination}\r\n"
            f"Priority: 1\r\n"
            f"Callerid: \"{callerid}\" <{callerid}>\r\n"
            f"Variable: pt1c_dst={destination}\r\n"
            f"Variable: pt1c_cid={callerid}\r\n"
            # Inheritable copies (prefix "__") so the chosen CID survives
            # Local -> PJSIP/*-WS -> PJSIP/SIP-TRUNK-* channel transitions.
            # MikoPBX by default keeps pt1c_cid non-inheritable and overrides
            # OUTGOING_CID inside SIP-TRUNK-*-outgoing using per-group rules.
            # Our extensions.conf hooks read __CALLSPIRE_CID to force the
            # user-selected CID on the outgoing trunk leg.
            f"Variable: __pt1c_cid={callerid}\r\n"
            f"Variable: OUTGOING_CID={callerid}\r\n"
            f"Variable: __OUTGOING_CID={callerid}\r\n"
            f"Variable: __CALLSPIRE_CID={callerid}\r\n"
            f"Variable: __SIPADDHEADER01=X-Callspire-Originate: {originate_id}\r\n"
            f"Async: true\r\n"
            f"ActionID: {originate_id}\r\n"
            f"\r\n"
        )
        writer.write(originate_cmd.encode())
        await writer.drain()

        orig_resp = await _read_response(reader, timeout)
        success = "success" in orig_resp.lower()

        # --- Logoff ---
        writer.write(b"Action: Logoff\r\n\r\n")
        await writer.drain()

        return {
            "success": success,
            "originate_id": originate_id,
            "error": None if success else f"Originate failed: {orig_resp.strip()}",
        }

    except asyncio.TimeoutError:
        return {"success": False, "originate_id": originate_id, "error": "AMI connection timed out"}
    except OSError as exc:
        return {"success": False, "originate_id": originate_id, "error": f"AMI connection error: {exc}"}
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


async def _read_response(reader: asyncio.StreamReader, timeout: float) -> str:
    """Read AMI response lines until a blank line (end of message)."""
    lines: list[str] = []
    while True:
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        line = raw.decode("utf-8", errors="replace")
        if line.strip() == "":
            break
        lines.append(line)
    return "".join(lines)
