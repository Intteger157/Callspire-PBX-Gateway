"""Outbound dial-string normalization (gateway originate, mirrors Callspire.Core PhoneNumberHelper)."""

from __future__ import annotations

# ITU codes that yield an 11-digit string starting with '8' (not Russian trunk 8).
_INTERNATIONAL_PREFIXES_ELEVEN_DIGITS_STARTING_WITH_8 = (
    "852",  # Hong Kong
    "853",  # Macau
    "81",   # Japan
    "82",   # South Korea
    "84",   # Vietnam
    "86",   # China (some 11-digit dial strings)
    "850",  # North Korea
    "855",  # Cambodia
    "856",  # Laos
    "880",  # Bangladesh
    "886",  # Taiwan
)


def digits_only(value: str | None) -> str:
    if not value:
        return ""
    return "".join(c for c in value if c.isdigit())


def phones_match_for_dedup(a: str | None, b: str | None) -> bool:
    """Same caller on web + mobile may send +447… vs 447… — treat as one call."""
    da = digits_only(a)
    db = digits_only(b)
    if not da or not db:
        return False
    if da == db:
        return True
    if len(da) >= 10 and len(db) >= 10 and da[-10:] == db[-10:]:
        return True
    return False


def should_convert_russian_trunk_eight_to_seven(digits: str, user_typed_plus: bool) -> bool:
    if user_typed_plus:
        return False
    if len(digits) != 11 or digits[0] != "8":
        return False
    for prefix in _INTERNATIONAL_PREFIXES_ELEVEN_DIGITS_STARTING_WITH_8:
        if digits.startswith(prefix):
            return False
    nsn_first = digits[1]
    return "3" <= nsn_first <= "9"


def normalize_for_dial(raw: str | None) -> str:
    """Compact E.164 for originate destination / callerid."""
    if not raw or not raw.strip():
        return ""

    trimmed = raw.strip()
    digits = digits_only(trimmed)
    if not digits:
        return trimmed

    user_typed_plus = trimmed.startswith("+")

    if should_convert_russian_trunk_eight_to_seven(digits, user_typed_plus):
        digits = "7" + digits[1:]

    if user_typed_plus or len(digits) >= 11:
        return f"+{digits}"

    return digits
