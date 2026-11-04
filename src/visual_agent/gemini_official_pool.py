"""Rotating API-key pool backed by Google's supported GenAI Python SDK."""
from __future__ import annotations

import json
import os
from itertools import cycle
from pathlib import Path
from typing import Any, Optional

from google import genai
from google.genai import errors, types


class _ModelAdapter:
    """Keep the previous model.generate_content-shaped pool API."""

    def __init__(self, pool: "GeminiOfficialPool", api_key: str | None):
        self._pool = pool
        self._api_key = api_key

    def generate_content(self, contents: str, **kwargs: Any) -> Any:
        return self._pool._generate_with_key(self._api_key, contents, **kwargs)


class GeminiOfficialPool:
    def __init__(
        self,
        api_keys: Optional[list[str]] = None,
        model_name: str = "gemini-3.8-flash",
        config_path: Optional[Path | str] = None,
    ):
        if api_keys is None:
            api_keys_str = os.getenv("GEMINI_API_KEYS", "") or os.getenv("GOOGLE_API_KEY", "")
            api_keys = [key.strip() for key in api_keys_str.split(",") if key.strip()]

        if not api_keys:
            config_candidates = (
                [Path(config_path)]
                if config_path
                else [
                    Path.cwd() / ".pacer" / "gemini_keys.json",
                    Path(__file__).resolve().parents[2] / ".pacer" / "gemini_keys.json",
                    Path.home() / ".pacer" / "gemini_keys.json",
                ]
            )
            for candidate in config_candidates:
                if candidate.is_file():
                    try:
                        data = json.loads(candidate.read_text(encoding="utf-8"))
                        raw_keys = data.get("keys", [])
                        extracted: list[str] = []
                        for item in raw_keys:
                            key_val = ""
                            if isinstance(item, str):
                                key_val = item.strip()
                            elif isinstance(item, dict) and item.get("key"):
                                key_val = str(item["key"]).strip()
                            if key_val and not key_val.endswith("...") and key_val.startswith("AIzaSy"):
                                extracted.append(key_val)
                            elif key_val and not key_val.endswith("...") and not key_val.startswith("your_"):
                                extracted.append(key_val)
                        if extracted:
                            api_keys = extracted
                            configured_model = data.get("default_model")
                            if isinstance(configured_model, str):
                                model_name = configured_model
                            break
                    except (OSError, ValueError, TypeError):
                        continue

        self.api_keys = api_keys or []
        self.model_name = model_name
        self.key_cycle = cycle(self.api_keys) if self.api_keys else cycle([None])
        self.current_key: Optional[str] = next(self.key_cycle) if self.api_keys else None

    def get_next_key(self) -> Optional[str]:
        if not self.api_keys:
            return None
        self.current_key = next(self.key_cycle)
        return self.current_key

    def configure(self) -> None:
        """Retained for compatibility; the new SDK configures keys per client."""

    def get_model(self) -> _ModelAdapter:
        return _ModelAdapter(self, self.current_key)

    def _generate_with_key(self, api_key: str | None, contents: str, **kwargs: Any) -> Any:
        request_options = kwargs.pop("request_options", None)
        request_options = request_options if isinstance(request_options, dict) else {}
        generation_config = kwargs.pop("generation_config", kwargs.pop("config", None))
        timeout = request_options.get("timeout")
        http_options = None
        if timeout is not None:
            http_options = types.HttpOptions(timeout=max(1, int(float(timeout) * 1000)))

        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if http_options is not None:
            client_kwargs["http_options"] = http_options
        client = genai.Client(**client_kwargs)
        try:
            request_kwargs = dict(kwargs)
            if generation_config is not None:
                request_kwargs["config"] = generation_config
            return client.models.generate_content(
                model=self.model_name,
                contents=contents,
                **request_kwargs,
            )
        finally:
            client.close()

    @staticmethod
    def _is_rate_limit_error(exc: errors.ClientError) -> bool:
        code = getattr(exc, "code", None)
        status = str(getattr(exc, "status", "") or "").upper()
        message = str(exc).lower()
        return code == 429 or "RESOURCE_EXHAUSTED" in status or (
            code == 403 and ("quota" in message or "rate limit" in message)
        )

    def generate_content(self, contents: str, **kwargs: Any) -> Any:
        """Generate content and rotate to the next key on quota/rate-limit errors."""
        attempts = len(self.api_keys) or 1
        for _ in range(attempts):
            try:
                return self.get_model().generate_content(contents, **kwargs)
            except errors.ClientError as exc:
                if not self._is_rate_limit_error(exc):
                    raise
                self.get_next_key()
        raise RuntimeError("All API keys exhausted or rate limited.")

    def check_health(self, timeout: float = 5.0) -> bool:
        if not self.api_keys:
            return False
        try:
            self.generate_content(
                "Respond with a single word: healthy",
                generation_config={
                    "temperature": 0,
                    "max_output_tokens": 20,
                },
                request_options={"timeout": timeout},
            )
            return True
        except Exception:
            return False


gemini_official_pool = GeminiOfficialPool()


def is_gemini_official_pool_available() -> bool:
    """Return True if Gemini Official Pool has active keys configured."""
    return bool(gemini_official_pool and gemini_official_pool.api_keys)
