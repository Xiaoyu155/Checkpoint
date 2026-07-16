from __future__ import annotations

import re


def redact_persisted_text(text: str) -> str:
    """Seed implementation: the private verifier intentionally exposes gaps."""
    return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", str(text or ""))
