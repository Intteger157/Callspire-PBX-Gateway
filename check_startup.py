#!/usr/bin/env python3
"""Quick startup diagnostics for the PBX gateway (run on the server in app dir)."""

from __future__ import annotations

import importlib
import sys
import traceback


def step(title: str, fn) -> bool:
    print(f"\n== {title} ==")
    try:
        fn()
        print("OK")
        return True
    except Exception as exc:
        print(f"FAIL: {exc}")
        traceback.print_exc()
        return False


def main() -> int:
    ok = True

    ok &= step("Python version", lambda: print(sys.version.replace("\n", " ")))

    ok &= step(
        "Import config.load_config",
        lambda: importlib.import_module("config").load_config(),
    )

    ok &= step("Import auth", lambda: importlib.import_module("auth"))

    ok &= step(
        "Import gateway_web_softphone (optional)",
        lambda: importlib.import_module("gateway_web_softphone"),
    )

    ok &= step("Import app (full module load)", lambda: importlib.import_module("app"))

    print("\n" + ("All checks passed." if ok else "Some checks failed — see tracebacks above."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
