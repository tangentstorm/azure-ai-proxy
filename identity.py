from typing import Optional


def canonicalize_username(value: Optional[str]) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "@" not in value:
        return value
    local_part, _domain = value.split("@", 1)
    return local_part.strip()
