from __future__ import annotations

import os
import subprocess
from typing import Any


def hidden_subprocess_kwargs(*, detached: bool = False) -> dict[str, Any]:
    """Return Popen kwargs that keep Windows background workers off-screen."""
    if os.name != "nt":
        return {}

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if detached:
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)

    kwargs: dict[str, Any] = {"creationflags": creationflags}
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo
    return kwargs
