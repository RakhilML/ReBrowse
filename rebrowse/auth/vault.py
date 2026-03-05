"""Local encrypted credential storage."""

from __future__ import annotations
import json
import os
from pathlib import Path
from cryptography.fernet import Fernet
from rebrowse import config


def _ensure_vault() -> Path:
    config.VAULT_DIR.mkdir(parents=True, exist_ok=True)
    return config.VAULT_DIR


def _get_key() -> bytes:
    """Get or create the vault encryption key."""
    vault = _ensure_vault()
    key_file = vault / ".key"
    if key_file.exists():
        return key_file.read_bytes()
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    # Restrict permissions (best-effort on Windows)
    try:
        os.chmod(str(key_file), 0o600)
    except Exception:
        pass
    return key


def _fernet() -> Fernet:
    return Fernet(_get_key())


def store_credential(account: str, value: str) -> None:
    """Store an encrypted credential."""
    vault = _ensure_vault()
    cred_file = vault / "credentials.enc"

    creds = _load_all()
    creds[account] = value
    encrypted = _fernet().encrypt(json.dumps(creds).encode())
    cred_file.write_bytes(encrypted)


def get_credential(account: str) -> str | None:
    """Retrieve a stored credential."""
    creds = _load_all()
    return creds.get(account)


def delete_credential(account: str) -> bool:
    """Delete a stored credential."""
    creds = _load_all()
    if account not in creds:
        return False
    del creds[account]
    vault = _ensure_vault()
    cred_file = vault / "credentials.enc"
    encrypted = _fernet().encrypt(json.dumps(creds).encode())
    cred_file.write_bytes(encrypted)
    return True


def _load_all() -> dict[str, str]:
    vault = _ensure_vault()
    cred_file = vault / "credentials.enc"
    if not cred_file.exists():
        return {}
    try:
        decrypted = _fernet().decrypt(cred_file.read_bytes())
        return json.loads(decrypted)
    except Exception:
        return {}


def store_cookies(domain: str, cookies: list[dict]) -> None:
    """Store cookies for a domain."""
    store_credential(f"cookies:{domain}", json.dumps(cookies))


def get_cookies(domain: str) -> list[dict] | None:
    """Retrieve stored cookies for a domain."""
    raw = get_credential(f"cookies:{domain}")
    if raw:
        return json.loads(raw)
    return None


def store_api_key(domain: str, key: str, auth_type: str = "bearer") -> None:
    """Store an API key for a domain.

    auth_type: "bearer" | "header" | "query"
      - bearer: sent as Authorization: Bearer <key>
      - header: sent as X-API-Key: <key>
      - query: appended as ?api_key=<key>
    """
    store_credential(f"apikey:{domain}", json.dumps({
        "key": key,
        "auth_type": auth_type,
    }))


def get_api_key(domain: str) -> dict | None:
    """Retrieve stored API key for a domain. Returns {"key": ..., "auth_type": ...} or None."""
    raw = get_credential(f"apikey:{domain}")
    if raw:
        return json.loads(raw)
    return None


def delete_api_key(domain: str) -> bool:
    """Delete stored API key for a domain."""
    return delete_credential(f"apikey:{domain}")


def list_api_keys() -> list[str]:
    """List all domains that have stored API keys."""
    creds = _load_all()
    return [k.removeprefix("apikey:") for k in creds if k.startswith("apikey:")]
