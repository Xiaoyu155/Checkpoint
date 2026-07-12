from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_target(target_root: Path):
    path = target_root / "security_target.py"
    spec = importlib.util.spec_from_file_location("benchmark_security_target", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load benchmark target: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: b5_secret_redaction.py <target-root>", file=sys.stderr)
        return 2
    module = _load_target(Path(argv[0]).expanduser().resolve())
    redact = module.redact_persisted_text

    bearer = "Authorization: Bearer top-secret-token-value"
    bearer_result = redact(bearer)
    assert "top-secret-token-value" not in bearer_result, (
        "Bearer credential remained in persisted text: " + bearer_result
    )
    assert "Authorization: Bearer" in bearer_result

    url = "https://service.example/callback?access_token=query-secret-value&mode=debug"
    url_result = redact(url)
    assert "query-secret-value" not in url_result, (
        "access_token value remained in persisted URL: " + url_result
    )
    assert "mode=debug" in url_result
    print("B5_PRIVATE_VERIFIER_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
