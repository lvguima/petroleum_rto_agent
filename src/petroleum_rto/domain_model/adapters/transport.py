"""Small HTTP boundary used by domain-model provider adapters."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from time import monotonic
from typing import Literal, Protocol, cast

TransportFailureKind = Literal[
    "connect",
    "read-timeout",
    "write-timeout",
    "transport",
    "response-too-large",
]


@dataclass(frozen=True)
class HttpRequest:
    """Fully resolved HTTP request; provider adapters own URL allow-listing."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    connect_timeout_seconds: float
    read_timeout_seconds: float
    total_timeout_seconds: float
    max_response_bytes: int

    def __post_init__(self) -> None:
        for field, value in (
            ("connect_timeout_seconds", self.connect_timeout_seconds),
            ("read_timeout_seconds", self.read_timeout_seconds),
            ("total_timeout_seconds", self.total_timeout_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field} must be finite and positive")
        if isinstance(self.max_response_bytes, bool) or not isinstance(
            self.max_response_bytes, int
        ):
            raise TypeError("max_response_bytes must be an integer")
        if self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")


@dataclass(frozen=True)
class HttpResponse:
    """Buffered provider response returned by a transport implementation."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransportFailure(RuntimeError):
    """Transport failure carrying an explicit retry-safety decision."""

    def __init__(
        self,
        kind: TransportFailureKind,
        message: str,
        *,
        retryable_before_send: bool,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable_before_send = retryable_before_send


class HttpTransport(Protocol):
    """Injectable HTTP transport for provider adapters and offline tests."""

    def send(self, request: HttpRequest) -> HttpResponse: ...


class HttpxTransport:
    """HTTPX-backed transport with lazy dependency loading and no redirects."""

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock

    def send(self, request: HttpRequest) -> HttpResponse:
        started = self._clock()
        deadline = started + request.total_timeout_seconds
        try:
            httpx = import_module("httpx")
        except ImportError as exc:
            raise HttpTransportFailure(
                "transport",
                "httpx is required for live domain-model HTTP calls",
                retryable_before_send=False,
            ) from exc

        timeout_type = httpx.Timeout
        client_type = httpx.Client
        timeout = timeout_type(
            connect=min(request.connect_timeout_seconds, request.total_timeout_seconds),
            read=min(request.read_timeout_seconds, request.total_timeout_seconds),
            write=min(request.read_timeout_seconds, request.total_timeout_seconds),
            pool=min(request.connect_timeout_seconds, request.total_timeout_seconds),
        )
        try:
            with (
                client_type(
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                    verify=True,
                ) as client,
                client.stream(
                    request.method,
                    request.url,
                    headers=dict(request.headers),
                    content=request.body,
                ) as response,
            ):
                status_code = self._status_code(response)
                headers = self._headers(response)
                body = self._read_body(
                    response,
                    request.max_response_bytes,
                    deadline=deadline,
                )
        except HttpTransportFailure:
            raise
        except Exception as exc:
            failure_kind = self._exception_kind(exc)
            raise HttpTransportFailure(
                failure_kind,
                f"domain-model HTTP {failure_kind} failure",
                retryable_before_send=failure_kind == "connect",
            ) from exc
        return HttpResponse(status_code=status_code, headers=headers, body=body)

    @staticmethod
    def _exception_kind(exc: Exception) -> TransportFailureKind:
        name = type(exc).__name__
        if name in {"ConnectError", "ConnectTimeout"}:
            return "connect"
        if name == "ReadTimeout":
            return "read-timeout"
        if name == "WriteTimeout":
            return "write-timeout"
        return "transport"

    @staticmethod
    def _status_code(response: object) -> int:
        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int):
            raise HttpTransportFailure(
                "transport",
                "httpx response has no integer status code",
                retryable_before_send=False,
            )
        return status_code

    @staticmethod
    def _headers(response: object) -> dict[str, str]:
        raw_headers = getattr(response, "headers", None)
        if not isinstance(raw_headers, Mapping):
            raise HttpTransportFailure(
                "transport",
                "httpx response headers are not a mapping",
                retryable_before_send=False,
            )
        headers = cast(Mapping[object, object], raw_headers)
        return {str(key).lower(): str(value) for key, value in headers.items()}

    def _read_body(self, response: object, maximum: int, *, deadline: float) -> bytes:
        iterator = getattr(response, "iter_bytes", None)
        if not callable(iterator):
            raise HttpTransportFailure(
                "transport",
                "httpx response does not expose iter_bytes",
                retryable_before_send=False,
            )
        body = bytearray()
        if self._clock() >= deadline:
            raise HttpTransportFailure(
                "read-timeout",
                "domain-model response exceeded the total request deadline",
                retryable_before_send=False,
            )
        for chunk in iterator():
            if self._clock() >= deadline:
                raise HttpTransportFailure(
                    "read-timeout",
                    "domain-model response exceeded the total request deadline",
                    retryable_before_send=False,
                )
            if not isinstance(chunk, bytes):
                raise HttpTransportFailure(
                    "transport",
                    "httpx response yielded a non-bytes chunk",
                    retryable_before_send=False,
                )
            body.extend(chunk)
            if len(body) > maximum:
                raise HttpTransportFailure(
                    "response-too-large",
                    "domain-model response exceeds the configured byte limit",
                    retryable_before_send=False,
                )
            if self._clock() >= deadline:
                raise HttpTransportFailure(
                    "read-timeout",
                    "domain-model response exceeded the total request deadline",
                    retryable_before_send=False,
                )
        return bytes(body)
