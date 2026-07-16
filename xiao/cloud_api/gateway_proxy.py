from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from .auth import bearer_token
from .gateway_store import GatewayStore, GatewayStoreError


MAX_REQUEST_BYTES = 10 * 1024 * 1024
MAX_ERROR_BYTES = 1024 * 1024
RETRYABLE_UPSTREAM_STATUSES = {401, 403, 404, 408, 409, 429}
_STREAM_END = object()
_UNSUPPORTED_CONTENT_TYPES = {
    "audio",
    "image_url",
    "input_audio",
    "input_file",
    "input_image",
}


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    source: str


def estimate_input_tokens(payload: dict[str, Any]) -> int:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    character_estimate = int(math.ceil(len(serialized) / 4))
    # A UTF-8 BPE token cannot consume fewer than one serialized byte. Using the
    # complete request gives reservation safety for tools, schemas and framing.
    byte_upper_bound = len(serialized.encode("utf-8"))
    return max(1, character_estimate, byte_upper_bound)


def requested_max_output_tokens(payload: dict[str, Any], *, default: int) -> int:
    for key in ("max_completion_tokens", "max_output_tokens", "max_tokens"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return max(1, int(default))


def requested_output_choices(payload: dict[str, Any], *, endpoint: str) -> int:
    if not endpoint.endswith("/chat/completions"):
        return 1
    value = payload.get("n", 1)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 128:
        raise GatewayStoreError(
            "invalid_choice_count",
            "Chat completion n must be an integer from 1 to 128.",
        )
    return value


def validate_billable_request(payload: dict[str, Any], *, endpoint: str) -> None:
    if "web_search_options" in payload:
        raise GatewayStoreError(
            "metered_tool_not_supported",
            "Hosted web search is not supported by this price plan.",
        )
    service_tier = str(payload.get("service_tier") or "").strip().lower()
    if service_tier and service_tier != "default":
        raise GatewayStoreError(
            "service_tier_not_supported",
            "Only the default service tier is supported by this price plan.",
        )
    modalities = payload.get("modalities")
    if (
        isinstance(modalities, list)
        and any(str(item).lower() != "text" for item in modalities)
    ) or "audio" in payload:
        raise GatewayStoreError(
            "modality_not_supported",
            "Only text output is supported by this price plan.",
        )
    if endpoint.endswith("/responses") and payload.get("background") is True:
        raise GatewayStoreError(
            "background_not_supported",
            "Background responses are not supported by the token-billed gateway.",
        )
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if (
                isinstance(tool, dict)
                and str(tool.get("type") or "function") != "function"
            ):
                raise GatewayStoreError(
                    "metered_tool_not_supported",
                    "Hosted or externally metered tools are not supported by this price plan.",
                )
    if _contains_unsupported_content(payload):
        raise GatewayStoreError(
            "modality_not_supported",
            "Image, audio, and file inputs require a modality-specific price plan.",
        )


def extract_token_usage(payload: Any) -> TokenUsage | None:
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        response = payload.get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    input_tokens = _nonnegative_int(
        usage.get("input_tokens", usage.get("prompt_tokens", 0))
    )
    output_tokens = _nonnegative_int(
        usage.get("output_tokens", usage.get("completion_tokens", 0))
    )
    details = usage.get("input_tokens_details")
    if not isinstance(details, dict):
        details = usage.get("prompt_tokens_details")
    cached = (
        _nonnegative_int(details.get("cached_tokens", 0))
        if isinstance(details, dict)
        else 0
    )
    if not input_tokens and not output_tokens and not cached:
        return None
    return TokenUsage(
        input_tokens, min(cached, input_tokens), output_tokens, "upstream"
    )


def estimate_output_tokens(payload: Any) -> int:
    values: list[str] = []
    if isinstance(payload, dict):
        for key in ("output_text", "output"):
            if key in payload:
                _collect_text(payload[key], values)
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    _collect_text(choice.get("message", choice.get("text", "")), values)
    return (
        max(1, int(math.ceil(sum(len(value) for value in values) / 4))) if values else 1
    )


def build_upstream_url(base_url: str, endpoint: str) -> str:
    base = str(base_url).rstrip("/")
    path = "/" + str(endpoint).lstrip("/")
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path[3:]
    return base + path


def apply_output_token_limit(
    payload: dict[str, Any], *, endpoint: str, max_output_tokens: int
) -> dict[str, Any]:
    limited = dict(payload)
    limit = max(1, int(max_output_tokens))
    if endpoint.endswith("/responses"):
        limited.pop("max_tokens", None)
        limited.pop("max_completion_tokens", None)
        limited["max_output_tokens"] = limit
    elif endpoint.endswith("/chat/completions"):
        limited.pop("max_output_tokens", None)
        if "max_completion_tokens" in limited:
            limited.pop("max_tokens", None)
            limited["max_completion_tokens"] = limit
        else:
            limited["max_tokens"] = limit
    return limited


class StreamUsageTracker:
    def __init__(self) -> None:
        self.buffer = b""
        self.usage: TokenUsage | None = None
        self.output_chars = 0
        self.bytes_seen = 0
        self.terminal_seen = False
        self.terminal_failed = False

    def feed(self, chunk: bytes) -> None:
        self.bytes_seen += len(chunk)
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self._line(line.rstrip(b"\r"))
        if len(self.buffer) > 2 * 1024 * 1024:
            self.buffer = self.buffer[-64 * 1024 :]

    def finish(self) -> None:
        if self.buffer:
            self._line(self.buffer.rstrip(b"\r"))
            self.buffer = b""

    def resolved_usage(self, input_tokens: int) -> TokenUsage:
        if self.usage is not None:
            return self.usage
        estimated_output = max(1, int(math.ceil(self.output_chars / 4)))
        if not self.output_chars and self.bytes_seen:
            estimated_output = max(1, int(math.ceil(self.bytes_seen / 12)))
        return TokenUsage(max(1, input_tokens), 0, estimated_output, "estimated")

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        raw = line[5:].strip()
        if not raw:
            return
        if raw == b"[DONE]":
            self.terminal_seen = True
            return
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        usage = extract_token_usage(payload)
        if usage is not None:
            if self.usage is None:
                self.usage = usage
            else:
                self.usage = TokenUsage(
                    max(self.usage.input_tokens, usage.input_tokens),
                    max(self.usage.cached_input_tokens, usage.cached_input_tokens),
                    max(self.usage.output_tokens, usage.output_tokens),
                    "upstream",
                )
        event_type = str(payload.get("type") or "")
        if event_type == "response.completed":
            self.terminal_seen = True
        elif event_type in {"response.failed", "response.incomplete"}:
            self.terminal_seen = True
            self.terminal_failed = True
        self.output_chars += _stream_delta_chars(payload)


class GatewayProxy:
    def __init__(
        self,
        store: GatewayStore,
        *,
        transport_resolver: Callable[[], Any] | None = None,
    ) -> None:
        self.store = store
        self.transport_resolver = transport_resolver or (lambda: None)
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def forward(
        self,
        request: Any,
        *,
        endpoint: str,
        authorization: str,
        idempotency_key: str = "",
    ) -> Any:
        try:
            import httpx
            from fastapi.responses import JSONResponse, Response, StreamingResponse
        except ImportError as exc:
            raise RuntimeError(
                "Install cloud API dependencies with `pip install -e .[cloud]`."
            ) from exc

        if not str(idempotency_key or "").strip():
            raise GatewayStoreError(
                "idempotency_required",
                "Idempotency-Key or X-Request-Id is required for billable requests.",
            )

        body = await request.body()
        limit = _positive_env_int("PACER_GATEWAY_MAX_REQUEST_BYTES", MAX_REQUEST_BYTES)
        if len(body) > limit:
            raise GatewayStoreError(
                "request_too_large",
                "Request body exceeds the gateway limit.",
                status_code=413,
            )
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GatewayStoreError(
                "invalid_json", "Request body must be valid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise GatewayStoreError(
                "invalid_json", "Request body must be a JSON object."
            )
        validate_billable_request(payload, endpoint=endpoint)
        model = str(payload.get("model") or "").strip()
        if not model:
            raise GatewayStoreError("model_required", "Request model is required.")
        principal = self.store.authenticate_api_key(bearer_token(authorization))
        price = self.store.get_price(model)
        candidates = [
            item
            for item in self.store.eligible_upstreams(model)
            if item["secret_configured"]
        ]
        if not candidates:
            raise GatewayStoreError(
                "upstream_unavailable",
                "No configured upstream is currently available.",
                status_code=503,
            )
        input_estimate = estimate_input_tokens(payload)
        max_output = (
            0
            if endpoint.endswith("/embeddings")
            else requested_max_output_tokens(
                payload, default=int(price["max_output_tokens"])
            )
        )
        output_choices = requested_output_choices(payload, endpoint=endpoint)
        if output_choices > int(price["max_output_tokens"]):
            raise GatewayStoreError(
                "invalid_choice_count",
                "Chat completion n exceeds the configured total output limit.",
            )
        reserved_output = max_output * output_choices
        streaming = bool(payload.get("stream"))
        lease_seconds = max(float(item["timeout_seconds"]) for item in candidates) + 30
        request_fingerprint = hashlib.sha256(
            (
                endpoint
                + "\0"
                + json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            ).encode("utf-8")
        ).hexdigest()
        reservation = None
        selected_index = 0
        for selected_index, candidate in enumerate(candidates):
            try:
                reservation = self.store.begin_request(
                    principal=principal,
                    upstream_id=str(candidate["id"]),
                    endpoint=endpoint,
                    model=model,
                    idempotency_key=idempotency_key,
                    streaming=streaming,
                    estimated_input_tokens=input_estimate,
                    max_output_tokens=reserved_output,
                    lease_seconds=lease_seconds,
                    request_fingerprint=request_fingerprint,
                )
                break
            except GatewayStoreError as exc:
                if exc.code not in {"upstream_busy", "upstream_unavailable"}:
                    raise
        if reservation is None:
            raise GatewayStoreError(
                "upstream_unavailable",
                "No upstream had capacity for this request.",
                status_code=503,
            )
        candidates = candidates[selected_index:]
        payload = apply_output_token_limit(
            {**payload, "model": reservation["upstream_model"]},
            endpoint=endpoint,
            max_output_tokens=max(
                1, int(reservation["max_output_tokens"]) // output_choices
            ),
        )
        if streaming and endpoint.endswith("/chat/completions"):
            stream_options = payload.get("stream_options")
            payload["stream_options"] = {
                **(stream_options if isinstance(stream_options, dict) else {}),
                "include_usage": True,
            }
        request_id = str(reservation["id"])
        started = time.perf_counter()
        last_status = 502
        last_error = "upstream_unavailable"

        for index, upstream in enumerate(candidates):
            upstream_id = str(upstream["id"])
            if index:
                try:
                    self.store.switch_upstream(request_id, upstream_id)
                except GatewayStoreError:
                    continue
            token = os.environ.get(str(upstream["secret_env"]), "")
            if not token:
                continue
            timeout = httpx.Timeout(
                float(upstream["timeout_seconds"]),
                connect=min(15.0, float(upstream["timeout_seconds"])),
            )
            transport = self.transport_resolver()
            client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            )
            upstream_started = time.perf_counter()
            attempt = self.store.start_attempt(
                request_id, upstream_id, lease_seconds=lease_seconds
            )
            attempt_id = str(attempt["id"])
            try:
                upstream_request = client.build_request(
                    "POST",
                    build_upstream_url(str(upstream["base_url"]), endpoint),
                    content=json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8"),
                    headers=_upstream_headers(request.headers, token, request_id),
                )
                response = await client.send(upstream_request, stream=True)
            except httpx.HTTPError as exc:
                latency = (time.perf_counter() - upstream_started) * 1000
                if not isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
                ):
                    return await self._indeterminate_response(
                        client=client,
                        response=None,
                        request_id=request_id,
                        upstream_id=upstream_id,
                        attempt_id=attempt_id,
                        error_code="upstream_send_indeterminate",
                        http_status=502,
                        response_started=False,
                        started=started,
                    )
                self.store.finish_attempt(
                    attempt_id,
                    status="network_error",
                    http_status=0,
                    error_code="upstream_network_error",
                    latency_ms=latency,
                )
                self.store.record_upstream_result(
                    upstream_id, success=False, http_status=0, latency_ms=latency
                )
                await client.aclose()
                last_status = 502
                last_error = "upstream_network_error"
                continue
            latency = (time.perf_counter() - upstream_started) * 1000
            last_status = int(response.status_code)
            if response.status_code >= 400:
                try:
                    await _read_limited(response, MAX_ERROR_BYTES)
                except httpx.HTTPError:
                    await response.aclose()
                    await client.aclose()
                    self.store.finish_attempt(
                        attempt_id,
                        status="read_error",
                        http_status=response.status_code,
                        error_code="upstream_error_body_read_error",
                        response_started=True,
                        latency_ms=(time.perf_counter() - upstream_started) * 1000,
                    )
                    self.store.record_upstream_result(
                        upstream_id,
                        success=False,
                        http_status=0,
                        latency_ms=(time.perf_counter() - upstream_started) * 1000,
                    )
                    last_status = 502
                    last_error = "upstream_error_body_read_error"
                    continue
                await response.aclose()
                await client.aclose()
                retry_after = _retry_after(response.headers.get("retry-after", ""))
                self.store.record_upstream_result(
                    upstream_id,
                    success=False,
                    http_status=response.status_code,
                    latency_ms=latency,
                    retry_after_seconds=retry_after,
                )
                last_error = f"upstream_http_{response.status_code}"
                self.store.finish_attempt(
                    attempt_id,
                    status="http_error",
                    http_status=response.status_code,
                    error_code=last_error,
                    response_started=True,
                    latency_ms=latency,
                )
                if (
                    response.status_code in RETRYABLE_UPSTREAM_STATUSES
                    or response.status_code >= 500
                ):
                    continue
                self.store.fail_request(
                    request_id,
                    error_code=last_error,
                    http_status=response.status_code,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                return JSONResponse(
                    status_code=response.status_code,
                    content=_error_payload(
                        "Upstream rejected the request.", last_error, request_id
                    ),
                    headers={"X-Pacer-Request-Id": request_id},
                )

            if streaming:
                iterator = response.aiter_bytes()
                try:
                    first_chunk = await anext(iterator)
                except StopAsyncIteration:
                    return await self._indeterminate_response(
                        client=client,
                        response=response,
                        request_id=request_id,
                        upstream_id=upstream_id,
                        attempt_id=attempt_id,
                        error_code="stream_terminal_missing",
                        http_status=502,
                        response_started=True,
                        started=started,
                    )
                except httpx.HTTPError:
                    return await self._indeterminate_response(
                        client=client,
                        response=response,
                        request_id=request_id,
                        upstream_id=upstream_id,
                        attempt_id=attempt_id,
                        error_code="upstream_stream_start_indeterminate",
                        http_status=502,
                        response_started=True,
                        started=started,
                    )
                return StreamingResponse(
                    self._stream_response(
                        first_chunk=first_chunk,
                        iterator=iterator,
                        response=response,
                        client=client,
                        request_id=request_id,
                        upstream_id=upstream_id,
                        attempt_id=attempt_id,
                        input_estimate=input_estimate,
                        started=started,
                        lease_seconds=lease_seconds,
                    ),
                    status_code=response.status_code,
                    media_type=response.headers.get(
                        "content-type", "text/event-stream"
                    ),
                    headers={
                        "X-Pacer-Request-Id": request_id,
                        "X-Pacer-Reserved-MicroUSD": str(
                            reservation["reserved_microusd"]
                        ),
                        "Cache-Control": "no-cache",
                    },
                )

            heartbeat = asyncio.create_task(
                self._lease_heartbeat(request_id, lease_seconds=lease_seconds)
            )
            heartbeat_error: BaseException | None = None
            try:
                content = await response.aread()
            except httpx.HTTPError:
                return await self._indeterminate_response(
                    client=client,
                    response=response,
                    request_id=request_id,
                    upstream_id=upstream_id,
                    attempt_id=attempt_id,
                    error_code="upstream_read_indeterminate",
                    http_status=502,
                    response_started=True,
                    started=started,
                )
            finally:
                heartbeat_error = await _cancel_task(heartbeat)
            if heartbeat_error is not None:
                return await self._indeterminate_response(
                    client=client,
                    response=response,
                    request_id=request_id,
                    upstream_id=upstream_id,
                    attempt_id=attempt_id,
                    error_code="lease_renewal_failed",
                    http_status=502,
                    response_started=True,
                    started=started,
                )
            content_type = response.headers.get("content-type", "application/json")
            await response.aclose()
            await client.aclose()
            try:
                response_payload = json.loads(content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return await self._indeterminate_response(
                    client=client,
                    response=None,
                    request_id=request_id,
                    upstream_id=upstream_id,
                    attempt_id=attempt_id,
                    error_code="upstream_invalid_json_indeterminate",
                    http_status=502,
                    response_started=True,
                    started=started,
                )
            usage = extract_token_usage(response_payload)
            if usage is None:
                return await self._indeterminate_response(
                    client=client,
                    response=response,
                    request_id=request_id,
                    upstream_id=upstream_id,
                    attempt_id=attempt_id,
                    error_code="upstream_usage_missing",
                    http_status=502,
                    response_started=True,
                    started=started,
                )
            self.store.record_upstream_result(
                upstream_id,
                success=True,
                http_status=response.status_code,
                latency_ms=latency,
            )
            settled = self.store.settle_request(
                request_id,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                usage_source=usage.source,
                http_status=response.status_code,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            self.store.finish_attempt(
                attempt_id,
                status="success",
                http_status=response.status_code,
                response_started=True,
                upstream_request_id=_upstream_request_id(response),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                upstream_cost_microusd=int(settled["upstream_cost_microusd"]),
                latency_ms=(time.perf_counter() - upstream_started) * 1000,
            )
            return Response(
                content=content,
                status_code=response.status_code,
                media_type=content_type.split(";", 1)[0],
                headers={
                    "X-Pacer-Request-Id": request_id,
                    "X-Pacer-Cost-MicroUSD": str(settled["actual_microusd"]),
                    "X-Pacer-Usage-Source": str(settled["usage_source"]),
                },
            )

        self.store.fail_request(
            request_id,
            error_code=last_error,
            http_status=last_status,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        downstream_status = 429 if last_status == 429 else 502
        return JSONResponse(
            status_code=downstream_status,
            content=_error_payload(
                "All eligible upstreams failed.", last_error, request_id
            ),
            headers={"X-Pacer-Request-Id": request_id},
        )

    async def _indeterminate_response(
        self,
        *,
        client: Any,
        response: Any | None,
        request_id: str,
        upstream_id: str,
        attempt_id: str,
        error_code: str,
        http_status: int,
        response_started: bool,
        started: float,
    ) -> Any:
        from fastapi.responses import JSONResponse

        upstream_status = int(response.status_code) if response is not None else 0
        if response is not None:
            try:
                await response.aclose()
            except Exception:
                pass
        try:
            await client.aclose()
        except Exception:
            pass
        latency = (time.perf_counter() - started) * 1000
        self.store.finish_attempt(
            attempt_id,
            status="indeterminate",
            http_status=upstream_status,
            error_code=error_code,
            response_started=response_started,
            upstream_request_id=_upstream_request_id(response),
            latency_ms=latency,
        )
        self.store.record_upstream_result(
            upstream_id,
            success=False,
            http_status=upstream_status,
            latency_ms=latency,
        )
        self.store.mark_request_indeterminate(
            request_id,
            error_code=error_code,
            http_status=http_status,
            latency_ms=latency,
        )
        return JSONResponse(
            status_code=http_status,
            content=_error_payload(
                "Upstream result is indeterminate; manual reconciliation is required.",
                error_code,
                request_id,
            ),
            headers={"X-Pacer-Request-Id": request_id},
        )

    async def _lease_heartbeat(self, request_id: str, *, lease_seconds: float) -> None:
        interval = max(1.0, min(15.0, float(lease_seconds) / 3))
        while True:
            await asyncio.sleep(interval)
            renewed = False
            for retry in range(3):
                try:
                    renewed = await asyncio.to_thread(
                        self.store.renew_lease,
                        request_id,
                        lease_seconds=lease_seconds,
                    )
                    break
                except sqlite3.OperationalError:
                    if retry == 2:
                        raise
                    await asyncio.sleep(min(1.0, interval / 3))
            if not renewed:
                return

    async def _stream_response(
        self,
        *,
        first_chunk: bytes,
        iterator: AsyncIterator[bytes],
        response: Any,
        client: Any,
        request_id: str,
        upstream_id: str,
        attempt_id: str,
        input_estimate: int,
        started: float,
        lease_seconds: float,
    ) -> AsyncIterator[bytes]:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=16)
        disconnected = asyncio.Event()
        producer = asyncio.create_task(
            self._produce_stream(
                first_chunk=first_chunk,
                iterator=iterator,
                queue=queue,
                disconnected=disconnected,
                response=response,
                client=client,
                request_id=request_id,
                upstream_id=upstream_id,
                attempt_id=attempt_id,
                input_estimate=input_estimate,
                started=started,
                lease_seconds=lease_seconds,
            )
        )
        self._background_tasks.add(producer)
        producer.add_done_callback(self._background_tasks.discard)
        try:
            while True:
                item = await queue.get()
                if item is _STREAM_END:
                    break
                yield item
        finally:
            disconnected.set()
            if producer.done():
                await producer

    async def _produce_stream(
        self,
        *,
        first_chunk: bytes,
        iterator: AsyncIterator[bytes],
        queue: asyncio.Queue[Any],
        disconnected: asyncio.Event,
        response: Any,
        client: Any,
        request_id: str,
        upstream_id: str,
        attempt_id: str,
        input_estimate: int,
        started: float,
        lease_seconds: float,
    ) -> None:
        tracker = StreamUsageTracker()
        heartbeat = asyncio.create_task(
            self._lease_heartbeat(request_id, lease_seconds=lease_seconds)
        )
        try:
            tracker.feed(first_chunk)
            await _queue_or_disconnect(queue, first_chunk, disconnected)
            _raise_task_failure(heartbeat)
            async for chunk in iterator:
                tracker.feed(chunk)
                await _queue_or_disconnect(queue, chunk, disconnected)
                _raise_task_failure(heartbeat)
            tracker.finish()
            _raise_task_failure(heartbeat)
            if not tracker.terminal_seen:
                await _queue_or_disconnect(
                    queue,
                    _stream_error_chunk("stream_terminal_missing", request_id),
                    disconnected,
                )
                self.store.mark_request_indeterminate(
                    request_id,
                    error_code="stream_terminal_missing",
                    http_status=502,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                self.store.record_upstream_result(
                    upstream_id,
                    success=False,
                    http_status=response.status_code,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                self.store.finish_attempt(
                    attempt_id,
                    status="indeterminate",
                    http_status=response.status_code,
                    error_code="stream_terminal_missing",
                    response_started=True,
                    upstream_request_id=_upstream_request_id(response),
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                return
            if tracker.usage is None:
                self.store.mark_request_indeterminate(
                    request_id,
                    error_code="upstream_usage_missing",
                    http_status=502,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                self.store.record_upstream_result(
                    upstream_id,
                    success=False,
                    http_status=response.status_code,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                self.store.finish_attempt(
                    attempt_id,
                    status="indeterminate",
                    http_status=response.status_code,
                    error_code="upstream_usage_missing",
                    response_started=True,
                    upstream_request_id=_upstream_request_id(response),
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                await _queue_or_disconnect(
                    queue,
                    _stream_error_chunk("upstream_usage_missing", request_id),
                    disconnected,
                )
                return
            usage = tracker.resolved_usage(input_estimate)
            settled = self.store.settle_request(
                request_id,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                usage_source=usage.source,
                error_code=(
                    "stream_upstream_failed"
                    if tracker.terminal_failed
                    else ""
                    if tracker.terminal_seen
                    else "stream_terminal_missing"
                ),
                http_status=response.status_code,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            self.store.record_upstream_result(
                upstream_id,
                success=tracker.terminal_seen and not tracker.terminal_failed,
                http_status=response.status_code,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            self.store.finish_attempt(
                attempt_id,
                status=(
                    "upstream_failed"
                    if tracker.terminal_failed
                    else "success"
                    if tracker.terminal_seen
                    else "incomplete"
                ),
                http_status=response.status_code,
                error_code=(
                    "stream_upstream_failed"
                    if tracker.terminal_failed
                    else ""
                    if tracker.terminal_seen
                    else "stream_terminal_missing"
                ),
                response_started=True,
                upstream_request_id=_upstream_request_id(response),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                upstream_cost_microusd=int(settled["upstream_cost_microusd"]),
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except BaseException:
            tracker.finish()
            if tracker.terminal_seen and tracker.usage is not None:
                usage = tracker.usage
                settled = self.store.settle_request(
                    request_id,
                    input_tokens=usage.input_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    output_tokens=usage.output_tokens,
                    usage_source=usage.source,
                    error_code=(
                        "stream_upstream_failed" if tracker.terminal_failed else ""
                    ),
                    http_status=response.status_code,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                self.store.record_upstream_result(
                    upstream_id,
                    success=not tracker.terminal_failed,
                    http_status=response.status_code,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                self.store.finish_attempt(
                    attempt_id,
                    status="upstream_failed" if tracker.terminal_failed else "success",
                    http_status=response.status_code,
                    error_code=(
                        "stream_upstream_failed" if tracker.terminal_failed else ""
                    ),
                    response_started=True,
                    upstream_request_id=_upstream_request_id(response),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    upstream_cost_microusd=int(settled["upstream_cost_microusd"]),
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                return
            self.store.mark_request_indeterminate(
                request_id,
                error_code="upstream_stream_indeterminate",
                http_status=502,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            self.store.record_upstream_result(
                upstream_id,
                success=False,
                http_status=0,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            self.store.finish_attempt(
                attempt_id,
                status="indeterminate",
                http_status=0,
                error_code="upstream_stream_indeterminate",
                response_started=True,
                upstream_request_id=_upstream_request_id(response),
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            try:
                await _queue_or_disconnect(
                    queue,
                    _stream_error_chunk("upstream_stream_indeterminate", request_id),
                    disconnected,
                )
            except BaseException:
                pass
        finally:
            await _cancel_task(heartbeat)
            await response.aclose()
            await client.aclose()
            if not disconnected.is_set():
                await queue.put(_STREAM_END)


async def _queue_or_disconnect(
    queue: asyncio.Queue[Any], chunk: bytes, disconnected: asyncio.Event
) -> None:
    if disconnected.is_set():
        return
    put_task = asyncio.create_task(queue.put(chunk))
    disconnect_task = asyncio.create_task(disconnected.wait())
    done, pending = await asyncio.wait(
        {put_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if disconnect_task in done and not put_task.done():
        put_task.cancel()


async def _cancel_task(task: asyncio.Task[Any]) -> BaseException | None:
    was_done = task.done()
    if not task.done():
        task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)
    if was_done and result and isinstance(result[0], BaseException):
        return result[0]
    return None


def _raise_task_failure(task: asyncio.Task[Any]) -> None:
    if not task.done() or task.cancelled():
        return
    error = task.exception()
    if error is not None:
        raise error


def _stream_error_chunk(code: str, request_id: str) -> bytes:
    return (
        "data: "
        + json.dumps(
            _error_payload(
                "Upstream stream ended without a billable terminal event.",
                code,
                request_id,
            ),
            separators=(",", ":"),
        )
        + "\n\n"
    ).encode("utf-8")


def _upstream_headers(source: Any, token: str, request_id: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": str(source.get("accept") or "application/json"),
        "User-Agent": "Pacer-Gateway/0.1",
        "X-Request-Id": request_id,
    }
    return headers


def _upstream_request_id(response: Any | None) -> str:
    if response is None:
        return ""
    headers = getattr(response, "headers", {})
    for name in ("x-request-id", "request-id", "x-amzn-requestid"):
        value = str(headers.get(name) or "").strip()
        if value:
            return value[:200]
    return ""


def _error_payload(message: str, code: str, request_id: str) -> dict[str, Any]:
    return {
        "error": {"message": message, "type": "gateway_error", "code": code},
        "request_id": request_id,
    }


async def _read_limited(response: Any, limit: int) -> bytes:
    result = bytearray()
    async for chunk in response.aiter_bytes():
        room = limit - len(result)
        if room <= 0:
            break
        result.extend(chunk[:room])
    return bytes(result)


def _retry_after(value: str) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _collect_text(value: Any, target: list[str]) -> None:
    if isinstance(value, str):
        target.append(value)
    elif isinstance(value, list):
        for item in value:
            _collect_text(item, target)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in {"id", "type", "role", "name"}:
                _collect_text(item, target)


def _contains_unsupported_content(value: Any) -> bool:
    if isinstance(value, list):
        return any(_contains_unsupported_content(item) for item in value)
    if not isinstance(value, dict):
        return False
    if str(value.get("type") or "").lower() in _UNSUPPORTED_CONTENT_TYPES:
        return True
    return any(_contains_unsupported_content(item) for item in value.values())


def _stream_delta_chars(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    event_type = str(payload.get("type") or "")
    delta = payload.get("delta")
    if event_type.endswith(".delta") and isinstance(delta, str):
        return len(delta)
    total = 0
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            piece = choice.get("delta")
            if isinstance(piece, dict):
                values: list[str] = []
                _collect_text(piece, values)
                total += sum(len(value) for value in values)
    return total


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default
