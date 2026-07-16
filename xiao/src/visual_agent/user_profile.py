from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LocalUserProfile:
    email: str = ""
    display_name: str = ""
    organization: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.email.strip())

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "configured": self.configured,
            "email": self.email,
            "display_name": self.display_name,
            "organization": self.organization,
        }


def user_profile_path() -> Path:
    override = os.environ.get("PACER_PROFILE_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pacer" / "profile.json"


def load_user_profile(path: str | Path | None = None) -> LocalUserProfile:
    source = Path(path).expanduser() if path else user_profile_path()
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return LocalUserProfile(
        email=str(payload.get("email") or "").strip(),
        display_name=str(payload.get("display_name") or "").strip(),
        organization=str(payload.get("organization") or "").strip(),
    )


def save_user_profile(profile: LocalUserProfile, path: str | Path | None = None) -> Path:
    email = profile.email.strip()
    if email and not _looks_like_email(email):
        raise ValueError("邮箱格式不正确")
    target = Path(path).expanduser() if path else user_profile_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(profile.to_public_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def profile_from_payload(payload: dict[str, Any]) -> LocalUserProfile:
    return LocalUserProfile(
        email=str(payload.get("email") or "").strip(),
        display_name=str(payload.get("display_name") or "").strip(),
        organization=str(payload.get("organization") or "").strip(),
    )


def _looks_like_email(value: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value.strip()))
