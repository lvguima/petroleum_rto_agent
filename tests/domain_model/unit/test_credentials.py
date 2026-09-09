from __future__ import annotations

from pathlib import Path

import pytest

from petroleum_rto.domain_model.chat_settings import (
    DMX_CHAT_MODEL,
    DMX_CHAT_URL,
    DMX_SYSTEM_PROMPT,
    DmxChatSettingsError,
    load_dmx_chat_settings,
)
from petroleum_rto.domain_model.credentials import LocalCredentialError, load_local_dmx_api_key


def test_default_system_prompt_does_not_repeat_operational_disclaimers() -> None:
    assert "未经现场验证" not in DMX_SYSTEM_PROMPT
    assert "现场控制权" not in DMX_SYSTEM_PROMPT
    assert "快照" not in DMX_SYSTEM_PROMPT
    assert "不补造" in DMX_SYSTEM_PROMPT
    assert "已经执行" in DMX_SYSTEM_PROMPT


def test_settings_loader_uses_injected_key_loader_without_exposing_failure() -> None:
    settings = load_dmx_chat_settings(key_loader=lambda: "sk-loaded-locally")

    assert settings.model == DMX_CHAT_MODEL
    assert settings.url == DMX_CHAT_URL

    with pytest.raises(DmxChatSettingsError) as captured:
        load_dmx_chat_settings(key_loader=lambda: None)
    assert "sk-" not in str(captured.value)


@pytest.mark.parametrize(
    "payload",
    [
        "sk-local-bare-key\n",
        '{"api_key":"sk-local-json-key"}\n',
    ],
)
def test_local_credential_file_accepts_only_protected_bare_or_strict_json(
    tmp_path: Path,
    payload: str,
) -> None:
    path = tmp_path / "dmx_api.json"
    path.write_text(payload, encoding="ascii")
    path.chmod(0o600)

    assert load_local_dmx_api_key(path) in {"sk-local-bare-key", "sk-local-json-key"}

    path.write_text('{"api_key":"sk-one","api_key":"sk-two"}', encoding="ascii")
    with pytest.raises(LocalCredentialError, match="JSON contract"):
        load_local_dmx_api_key(path)

    path.write_text('{"api_key":"sk-local-key","extra":true}', encoding="ascii")
    with pytest.raises(LocalCredentialError, match="JSON contract"):
        load_local_dmx_api_key(path)


def test_local_credential_file_rejects_broad_permissions_and_symbolic_links(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dmx_api.json"
    path.write_text("sk-local-protected-key", encoding="ascii")
    path.chmod(0o644)
    with pytest.raises(LocalCredentialError, match="permissions"):
        load_local_dmx_api_key(path)

    path.chmod(0o600)
    link = tmp_path / "linked-key.json"
    link.symlink_to(path)
    with pytest.raises(LocalCredentialError, match="opened safely"):
        load_local_dmx_api_key(link)
