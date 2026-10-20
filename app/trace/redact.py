"""Mask email addresses in anything we record, so traces never hold full contact details."""

import re
from typing import Any

_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def mask_email(text: str) -> str:
    """jane.doe@example.com -> j***@example.com"""
    return _EMAIL.sub(r"\1***@\2", text)


def mask_emails(value: Any) -> Any:
    """Return a copy of a JSON-like value with every email inside it masked."""
    if isinstance(value, str):
        return mask_email(value)
    if isinstance(value, dict):
        return {k: mask_emails(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_emails(v) for v in value]
    return value
