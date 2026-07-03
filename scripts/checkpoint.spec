# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the DevPacer / Checkpoint desktop workbench."""

import sys
from pathlib import Path

block_cipher = None
spec_dir = Path(SPECPATH).resolve()
repo_root = spec_dir.parent
src_root = repo_root / "src"

a = Analysis(
    [str(repo_root / "checkpoint_app.py")],
    pathex=[str(src_root)],
    binaries=[],
    datas=[
        # Agent profile YAMLs read at runtime
        (str(src_root / "visual_agent" / "agent_profiles" / "*.yaml"), "visual_agent/agent_profiles"),
    ],
    hiddenimports=[
        # Lazy imports inside visual_agent that PyInstaller can't trace statically
        "visual_agent.goal_intake",
        "visual_agent.chief_engineer",
        "visual_agent.chief_run",
        "visual_agent.dashboard",
        "visual_agent.mission_plan_import",
        "visual_agent.llm_providers",
        "visual_agent.model_router",
        "visual_agent.model_credentials",
        "visual_agent.agent_backends",
        "visual_agent.subscription_quota",
        "visual_agent.workspace",
        "visual_agent.missions",
        "visual_agent.reports",
        "visual_agent.planner",
        "visual_agent.scheduler",
        "visual_agent.env",
        "visual_agent.locks",
        "visual_agent.db",
        "visual_agent.telemetry",
        "visual_agent.project_memory",
        "visual_agent.repo_map",
        # stdlib extras sometimes missed on Windows
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.scrolledtext",
        "_tkinter",
        "queue",
        "threading",
        "urllib.request",
        "urllib.error",
        # Third-party
        "yaml",
        "portalocker",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Not needed for the desktop workbench
        "playwright",
        "boto3",
        "fastapi",
        "uvicorn",
        "celery",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DevPacer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # no terminal window — pure GUI
    icon=str(repo_root / "assets" / "DevPacer.ico"),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DevPacer",
)
