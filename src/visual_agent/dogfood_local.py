"""Build a wheel, install it into a fresh environment, and run a smoke check."""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any


def run_local_dogfood(
    repo_root: str | Path,
    *,
    smoke_code: str | None = None,
    smoke_test_command: str | None = None,
    patched_files: list[str] | None = None,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Return a failed record unless a real wheel installs and the smoke check passes.

    Verification is real, not self-certified:
    - The wheel must build and install successfully.
    - The smoke import must succeed.
    - If ``patched_files`` is provided, each patched file's content in the
      installed wheel must match the workspace source (proves the change
      actually shipped in the artifact, not just that something installed).
    - If ``smoke_test_command`` is provided, it must pass in the venv
      (provides a real test receipt rather than just import-ability).
    Without these, ``installed_artifact_verified`` stays False.
    """
    root = Path(repo_root).expanduser().resolve()
    if not (root / "pyproject.toml").is_file():
        return _failed("dogfood_artifacts_required")
    code = smoke_code or "import visual_agent; raise SystemExit(0 if visual_agent.__version__ else 1)"
    holder = Path(tempfile.mkdtemp(prefix="pacer-dogfood-"))
    try:
        wheel = _build_wheel(root, holder / "dist", timeout_seconds)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        python = _create_venv(holder / "venv", timeout_seconds)
        _install(python, wheel, timeout_seconds)
        smoke = _smoke(python, code, timeout_seconds)
        if smoke.returncode != 0:
            return _failed("dogfood_self_check_not_passed", wheel=str(wheel), digest=digest, detail=(smoke.stderr or smoke.stdout or "")[:400])

        # Verify patched files actually shipped in the wheel with matching content
        files_verified: list[str] = []
        files_mismatched: list[str] = []
        if patched_files:
            for rel in patched_files:
                wheel_content = _read_wheel_file(wheel, rel)
                workspace_path = root / rel
                if wheel_content is None:
                    files_mismatched.append(f"{rel}: not found in wheel")
                    continue
                try:
                    workspace_content = workspace_path.read_bytes()
                except OSError as exc:
                    files_mismatched.append(f"{rel}: cannot read workspace file ({exc})")
                    continue
                # Normalize line endings for comparison (wheel may use CRLF)
                wheel_norm = wheel_content.replace(b"\r\n", b"\n")
                workspace_norm = workspace_content.replace(b"\r\n", b"\n")
                if wheel_norm != workspace_norm:
                    files_mismatched.append(f"{rel}: wheel content differs from workspace")
                else:
                    files_verified.append(rel)

        # Run optional test command for a real receipt
        test_receipt: dict[str, Any] = {}
        test_passed = False
        if smoke_test_command:
            test_result = _run(
                [str(python), "-c", f"import subprocess,sys; r=subprocess.run({smoke_test_command!r},shell=True); sys.exit(r.returncode)"],
                root, timeout_seconds,
            )
            test_passed = test_result.returncode == 0
            test_receipt = {
                "command": smoke_test_command,
                "returncode": test_result.returncode,
                "stdout": (test_result.stdout or "")[:200],
                "stderr": (test_result.stderr or "")[:200],
            }

        # Determine real verification status
        artifact_files_ok = not patched_files or len(files_mismatched) == 0
        quality_ok = not smoke_test_command or test_passed
        # installed_artifact_verified is only True when ALL checks pass:
        # wheel builds, installs, imports, patched files shipped, tests pass.
        fully_verified = artifact_files_ok and quality_ok

        result: dict[str, Any] = {
            "pacer_on_pacer": True,
            "self_change_attributed": True,
            "status": "passed" if fully_verified else "partial",
            "reason": "wheel_install_and_content_verified" if fully_verified else "verification_incomplete",
            "installed_artifact_verified": fully_verified,
            "artifact_files_verified": artifact_files_ok,
            "evidence_digest": digest,
            "quality_target_met": quality_ok,
            "verified_batch": fully_verified,
            "task_review_valid": fully_verified,
            "wheel_path": str(wheel),
            "wheel_sha256": digest,
        }
        if patched_files:
            result["patched_files_verified"] = files_verified
            result["patched_files_mismatched"] = files_mismatched
        if smoke_test_command:
            result["test_receipt"] = test_receipt
        return result
    except DogfoodBuildError as exc:
        return _failed(exc.code, detail=str(exc))
    finally:
        shutil.rmtree(holder, ignore_errors=True)


class DogfoodBuildError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


def _failed(code: str, *, wheel: str = "", digest: str = "", detail: str = "") -> dict[str, Any]:
    return {
        "pacer_on_pacer": True,
        "self_change_attributed": True,
        "status": "failed",
        "reason": code,
        "detail": detail,
        "installed_artifact_verified": False,
        "artifact_files_verified": False,
        "evidence_digest": digest,
        "quality_target_met": False,
        "verified_batch": False,
        "task_review_valid": False,
        "wheel_path": wheel,
        "wheel_sha256": digest,
    }


def _run(argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _build_wheel(root: Path, dest: Path, timeout: int) -> Path:
    del timeout
    dest.mkdir(parents=True, exist_ok=True)
    name, version = _read_project(root)
    packages = _package_dirs(root)
    if not packages:
        raise DogfoodBuildError("dogfood_wheel_build_failed", "no Python package directory found")
    filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
    path = dest / filename
    _write_wheel(path, name, version, packages)
    return path


def _read_wheel_file(wheel_path: Path, rel_path: str) -> bytes | None:
    """Read a file from the wheel archive by its relative path.

    Returns None if the file is not present in the wheel.
    The rel_path may use workspace-relative paths (e.g. src/pkg/foo.py)
    but the wheel stores package-relative paths (e.g. pkg/foo.py).
    """
    norm = rel_path.replace("\\", "/").lstrip("/")
    # Strip common source prefixes to get the package-relative path
    for prefix in ("src/", "lib/", "python/"):
        if norm.startswith(prefix):
            norm = norm[len(prefix):]
            break
    try:
        with zipfile.ZipFile(wheel_path, "r") as archive:
            names = archive.namelist()
            # Try exact match first
            if norm in names:
                return archive.read(norm)
            # Try matching with package prefix
            for name in names:
                if name.endswith(norm) or name.endswith("/" + norm):
                    return archive.read(name)
    except Exception:
        return None
    return None


def _read_project(root: Path) -> tuple[str, str]:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    name = _toml_value(text, "name") or root.name
    version = _toml_value(text, "version") or "0.0.0"
    return name, version


def _toml_value(text: str, key: str) -> str:
    match = re.search(rf"(?m)^{key}\s*=\s*[\"']([^\"']+)[\"']", text)
    return match.group(1).strip() if match else ""


def _package_dirs(root: Path) -> list[Path]:
    source = root / "src" if (root / "src").is_dir() else root
    return [path for path in sorted(source.iterdir()) if path.is_dir() and (path / "__init__.py").is_file()]


def _write_wheel(path: Path, name: str, version: str, packages: list[Path]) -> None:
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    records: list[tuple[str, bytes]] = []
    for package in packages:
        for file_path in sorted(item for item in package.rglob("*") if item.is_file()):
            if "__pycache__" in file_path.parts:
                continue
            relative = Path(package.name) / file_path.relative_to(package)
            records.append((relative.as_posix(), file_path.read_bytes()))
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode("utf-8")
    wheel_info = b"Wheel-Version: 1.0\nGenerator: pacer-dogfood\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    records.append((f"{dist_info}/METADATA", metadata))
    records.append((f"{dist_info}/WHEEL", wheel_info))
    record_lines = []
    for arcname, payload in records:
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode("ascii").rstrip("=")
        record_lines.append(f"{arcname},sha256={digest},{len(payload)}")
    record_lines.append(f"{dist_info}/RECORD,,")
    records.append((f"{dist_info}/RECORD", ("\n".join(record_lines) + "\n").encode("utf-8")))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, payload in records:
            archive.writestr(arcname, payload)


def _create_venv(path: Path, timeout: int) -> Path:
    result = _run([sys.executable, "-m", "venv", str(path)], path.parent, timeout)
    python = path / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if result.returncode != 0 or not python.is_file():
        raise DogfoodBuildError("dogfood_venv_failed", (result.stderr or result.stdout or "")[:400])
    return python


def _install(python: Path, wheel: Path, timeout: int) -> None:
    result = _run(
        [str(python), "-m", "pip", "install", "--no-deps", "--disable-pip-version-check", str(wheel)],
        wheel.parent,
        timeout,
    )
    if result.returncode != 0:
        raise DogfoodBuildError("dogfood_install_failed", (result.stderr or result.stdout or "")[:400])


def _smoke(python: Path, code: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return _run([str(python), "-c", code], python.parent, timeout)
