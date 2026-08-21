from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from petroleum_rto.domain_model.loader import (
    load_provider_catalog,
    packaged_provider_catalog_bytes,
)
from petroleum_rto.domain_model.models import (
    DMX_BASE_URL,
    DMX_CREDENTIAL_ENV,
    ProviderCatalog,
    ProviderModelInfo,
    ProviderProfile,
)


def test_checkout_and_packaged_catalog_are_byte_identical(repo_root: Path) -> None:
    checkout = (repo_root / "configs/domain_model/provider_catalog.json").read_bytes()

    assert checkout == packaged_provider_catalog_bytes()
    assert load_provider_catalog(repo_root) == load_provider_catalog()


def test_dmx_catalog_pins_endpoint_credentials_and_three_api_dialects(repo_root: Path) -> None:
    catalog = load_provider_catalog(repo_root)
    provider = catalog.provider("dmx-cn")

    assert provider.base_url == DMX_BASE_URL
    assert provider.credential_env == DMX_CREDENTIAL_ENV
    assert provider.endpoint_id == "dmx-cn-v1"
    assert provider.allowed_paths == (
        "/models",
        "/chat/completions",
        "/responses",
        "/messages",
    )
    assert provider.model("deepseek-v4-flash-0731").api_style == "openai_chat"
    assert provider.model("deepseek-v4-flash-0731").upstream_family == "deepseek"
    assert provider.model("gpt-5.6-sol").api_style == "openai_responses"
    assert provider.model("gpt-5.6-sol").upstream_family == "openai"
    assert provider.model("claude-opus-4-8").api_style == "anthropic_messages"
    assert provider.model("claude-opus-4-8").upstream_family == "anthropic"
    assert provider.endpoint("gpt-5.6-sol") == "https://www.dmxapi.cn/v1/responses"
    assert provider.connect_timeout_seconds == 5.0
    assert provider.read_timeout_seconds == 45.0
    assert provider.round_timeout_seconds == 120.0
    assert provider.maximum_physical_attempts == 2
    assert provider.maximum_concurrency == 1
    assert provider.maximum_retry_after_seconds == 20.0
    assert provider.maximum_raw_response_bytes == 128 * 1024

    for model in provider.models:
        assert model.maximum_output_tokens == 4096
        assert model.allowed_served_model_ids == (model.model_id,)
        assert model.output_mode == "prompt_json"
        assert model.json_object is False
        assert model.json_schema_strict is False


def test_profiles_reject_unknown_fields_widened_allowlist_and_path_style_mismatch(
    repo_root: Path,
) -> None:
    catalog = load_provider_catalog(repo_root)
    raw = catalog.as_dict()

    with pytest.raises(ValueError, match="fields differ"):
        ProviderCatalog.from_mapping({**raw, "secret": "must-not-exist"})

    provider = catalog.provider("dmx-cn")
    with pytest.raises(ValueError, match="four fixed exact paths"):
        ProviderProfile(
            **{
                **provider.__dict__,
                "allowed_paths": (*provider.allowed_paths, "/arbitrary"),
            }
        )
    model = provider.models[0]
    with pytest.raises(ValueError, match="differs from its API style"):
        replace(model, model_id="bad-model", endpoint_path="/responses")

    with pytest.raises(ValueError, match="transport policy are fixed"):
        replace(provider, connect_timeout_seconds=6.0)

    overlapping_chat = replace(
        provider.models[1],
        api_style="openai_chat",
        endpoint_path="/chat/completions",
        allowed_served_model_ids=provider.models[0].allowed_served_model_ids,
    )
    with pytest.raises(ValueError, match="disjoint served-model"):
        replace(provider, models=(provider.models[0], overlapping_chat, provider.models[2]))


def test_structured_output_modes_require_explicit_capabilities(
    repo_root: Path,
) -> None:
    model = load_provider_catalog(repo_root).provider("dmx-cn").model("deepseek-v4-flash-0731")

    with pytest.raises(ValueError, match="requires the json_object"):
        replace(model, output_mode="json_object")
    with pytest.raises(ValueError, match="requires the json_schema_strict"):
        replace(model, output_mode="json_schema_strict")

    structured = replace(
        model,
        output_mode="json_schema_strict",
        json_schema_strict=True,
    )
    assert structured.output_mode == "json_schema_strict"
    assert structured.json_schema_strict is True

    messages_model = load_provider_catalog(repo_root).provider("dmx-cn").model("claude-opus-4-8")
    with pytest.raises(ValueError, match="prompt_json mode only"):
        replace(
            messages_model,
            output_mode="json_object",
            json_object=True,
        )


def test_provider_discovery_info_is_strict_and_immutable() -> None:
    info = ProviderModelInfo.from_mapping(
        {"id": "gpt-5.6-sol", "owned_by": "openai", "metadata": {"created": 1}}
    )

    assert info.as_dict() == {
        "id": "gpt-5.6-sol",
        "owned_by": "openai",
        "metadata": {"created": 1},
    }
    with pytest.raises(TypeError):
        info.metadata["created"] = 2  # type: ignore[index]


def test_missing_model_fails_without_dynamic_endpoint_fallback(repo_root: Path) -> None:
    provider = load_provider_catalog(repo_root).provider("dmx-cn")

    with pytest.raises(KeyError, match="no configured model"):
        provider.model("not-configured")
