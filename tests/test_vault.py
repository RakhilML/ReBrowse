from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from rebrowse import config
from rebrowse.auth import vault


def test_api_key_roundtrip():
    vault.store_api_key("api.x.com", "k", "header")
    assert vault.get_api_key("api.x.com") == {"key": "k", "auth_type": "header"}
    assert vault.list_api_keys() == ["api.x.com"]
    assert vault.delete_api_key("api.x.com") is True
    assert vault.get_api_key("api.x.com") is None
    assert vault.delete_api_key("api.x.com") is False


def test_cookies_roundtrip():
    vault.store_cookies("x.com", [{"name": "sid", "value": "1"}])
    assert vault.get_cookies("x.com") == [{"name": "sid", "value": "1"}]
    assert vault.get_cookies("y.com") is None


def test_damaged_vault_reads_empty_but_refuses_writes(capsys):
    vault.store_api_key("a.com", "k1")
    cred_file = config.VAULT_DIR / "credentials.enc"
    original = cred_file.read_bytes()
    (config.VAULT_DIR / ".key").write_bytes(Fernet.generate_key())

    assert vault.get_api_key("a.com") is None
    assert "cannot decrypt" in capsys.readouterr().err
    with pytest.raises(vault.VaultError):
        vault.store_api_key("b.com", "k2")
    assert cred_file.read_bytes() == original
