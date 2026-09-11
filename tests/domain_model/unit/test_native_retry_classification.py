from __future__ import annotations

import httpx
import pytest
from test_native_protocol import Wire, chat, sse

from petroleum_rto.domain_model.native import NativeModelError, RetryableNativeModelError


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_http_status_is_structured_without_automatic_retry(status: int) -> None:
    wire = Wire([status])
    with pytest.raises(RetryableNativeModelError) as captured:
        wire.model().invoke("合成请求")
    code = "rate-limited" if status == 429 else "http-error"
    assert captured.value.code == str(captured.value) == code
    assert "fake-private-api-key" not in repr(captured.value)
    assert len(wire.requests) == wire.transport.request_count == 1


@pytest.mark.parametrize(
    "status,code",
    [
        (301, "http-error"),
        (400, "http-error"),
        (401, "authentication-failed"),
        (403, "permission-denied"),
        (404, "http-error"),
        (409, "http-error"),
        (422, "http-error"),
        (501, "http-error"),
        (505, "http-error"),
        (599, "http-error"),
    ],
)
def test_other_http_status_is_permanent_and_preserves_safe_code(status: int, code: str) -> None:
    wire = Wire([status])
    with pytest.raises(NativeModelError) as captured:
        wire.model().invoke("合成请求")
    assert type(captured.value) is NativeModelError
    assert captured.value.code == str(captured.value) == code
    assert "fake-private-api-key" not in repr(captured.value)
    assert len(wire.requests) == wire.transport.request_count == 1


@pytest.mark.parametrize(
    "error_type",
    [
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.CloseError,
        httpx.RemoteProtocolError,
    ],
)
def test_transient_request_exception_uses_type_and_never_provider_text(
    error_type: type[httpx.RequestError],
) -> None:
    wire = Wire([error_type("401 invalid credentials fake-private-api-key")])
    with pytest.raises(RetryableNativeModelError) as captured:
        wire.model().invoke("合成请求")
    assert captured.value.code == str(captured.value) == "transport-failed"
    assert "fake-private-api-key" not in repr(captured.value)
    assert captured.value.__suppress_context__ is True
    assert len(wire.requests) == wire.transport.request_count == 1


@pytest.mark.parametrize(
    "error_type",
    [
        httpx.RequestError,
        httpx.UnsupportedProtocol,
        httpx.LocalProtocolError,
        httpx.TooManyRedirects,
        httpx.DecodingError,
        httpx.ProxyError,
    ],
)
def test_other_request_exception_is_permanent_despite_transient_sounding_text(
    error_type: type[httpx.RequestError],
) -> None:
    wire = Wire([error_type("429 rate-limited timeout retry fake-private-api-key")])
    with pytest.raises(NativeModelError) as captured:
        wire.model().invoke("合成请求")
    assert type(captured.value) is NativeModelError
    assert captured.value.code == str(captured.value) == "transport-failed"
    assert "fake-private-api-key" not in repr(captured.value)
    assert captured.value.__suppress_context__ is True
    assert len(wire.requests) == wire.transport.request_count == 1


def test_completed_transport_with_truncated_stream_is_permanent() -> None:
    wire = Wire(
        [sse({"choices": [{"index": 0, "delta": {"content": "部分正文"}, "finish_reason": None}]})]
    )
    chunks = wire.model(stream=True).stream("合成请求")
    assert next(chunks).text == "部分正文"
    with pytest.raises(NativeModelError) as captured:
        list(chunks)
    assert type(captured.value) is NativeModelError
    assert captured.value.code == "incomplete-stream"
    assert len(wire.requests) == wire.transport.request_count == 1


def test_invalid_provider_response_remains_permanent() -> None:
    wire = Wire([chat("部分正文", finish="length")])
    with pytest.raises(NativeModelError) as captured:
        wire.model().invoke("合成请求")
    assert type(captured.value) is NativeModelError
    assert captured.value.code == "incomplete-response"
    assert len(wire.requests) == wire.transport.request_count == 1


def test_local_credential_rejection_remains_permanent_and_has_no_attempt() -> None:
    wire = Wire([])
    with pytest.raises(NativeModelError) as captured:
        wire.model().invoke("fake-private-api-key")
    assert type(captured.value) is NativeModelError
    assert captured.value.code == "credential-in-request"
    assert not wire.requests and wire.transport.request_count == 0
