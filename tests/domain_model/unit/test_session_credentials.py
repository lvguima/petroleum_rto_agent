"""Known credentials must never enter the checkpoint through user input."""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from langchain_core.messages import HumanMessage

from petroleum_rto.assistant.native_tools import AgentDomainTools
from petroleum_rto.assistant.react import ReactAgent
from petroleum_rto.assistant.session import SessionError, SessionStore
from petroleum_rto.domain_model.models import DEFAULT_MODEL_ID, ModelSelection, model_profile
from petroleum_rto.domain_model.native import DmxNativeModel, NativeModelError, NativeTransport

_KEY = "synthetic-api/+secret=value~~~!"
_VARIANTS = sorted(
    {
        _KEY,
        quote(_KEY, safe=""),
        base64.b64encode(_KEY.encode()).decode(),
        base64.b64encode(_KEY.encode()).decode().rstrip("="),
        base64.urlsafe_b64encode(_KEY.encode()).decode(),
        base64.urlsafe_b64encode(_KEY.encode()).decode().rstrip("="),
    }
)


def model() -> DmxNativeModel:
    def no_network(request: httpx.Request) -> httpx.Response:
        pytest.fail("credential rejection or restore must not send a model request")

    return DmxNativeModel(
        transport=NativeTransport(_KEY, http_transport=httpx.MockTransport(no_network)),
        selection=ModelSelection(model_profile(DEFAULT_MODEL_ID)),
    )


@pytest.mark.parametrize("value", _VARIANTS)
def test_persistence_guard_checks_nested_fields_without_reflecting_secret(value: str) -> None:
    configured = model()
    try:
        with pytest.raises(NativeModelError, match="credential-in-persistence") as failure:
            configured.transport.assert_safe_persistence({"nested": [{value: "text"}]})
        assert value not in str(failure.value) and _KEY not in str(failure.value)
        configured.transport.assert_safe_persistence({"text": "ordinary synthetic content"})
        assert configured.transport.request_count == 0
    finally:
        configured.transport.close()


@pytest.mark.parametrize("value", _VARIANTS)
@pytest.mark.parametrize("prefix", ["误粘贴的值：", "/model "])
def test_known_credential_input_is_rejected_before_any_checkpoint_write(
    tmp_path: Path, repo_root: Path, value: str, prefix: str
) -> None:
    path = tmp_path.resolve() / "session.sqlite"
    configured = model()
    runtime = ReactAgent(configured, AgentDomainTools(repo_root), store=SessionStore(path))
    try:
        result = runtime.handle(prefix + value)
        assert result.errors
        assert all(value not in text and _KEY not in text for text in result.errors)
        assert configured.transport.request_count == 0
        assert runtime.messages == []
        assert runtime.data["user_message"] == ""
    finally:
        runtime.close()
    assert value.encode() not in path.read_bytes()
    assert _KEY.encode() not in path.read_bytes()


@pytest.mark.parametrize("location", ["messages", "session"])
def test_restore_rejects_preexisting_credential_contamination_without_network(
    tmp_path: Path, repo_root: Path, location: str
) -> None:
    path = tmp_path.resolve() / "session.sqlite"
    runtime = ReactAgent(model(), AgentDomainTools(repo_root), store=SessionStore(path))
    try:
        # Manufacture damaged legacy disk data directly, bypassing the UI guard.
        if location == "messages":
            runtime._update({}, [HumanMessage(content=_KEY)])
        else:
            runtime._update({"user_message": _KEY})
    finally:
        runtime.close()
    configured = model()
    store = SessionStore(path)
    try:
        with pytest.raises(SessionError) as failure:
            ReactAgent(configured, AgentDomainTools(repo_root), store=store)
        assert _KEY not in str(failure.value)
        assert configured.transport.request_count == 0
    finally:
        store.close()
        configured.transport.close()
