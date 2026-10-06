"""Local encrypted credential storage (Fernet; key file kept beside the vault)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from rebrowse import config


class VaultError(RuntimeError):
    pass


def _vault_dir() -> Path:
    config.VAULT_DIR.mkdir(parents=True, exist_ok=True)
    return config.VAULT_DIR


def _cred_file() -> Path:
    return _vault_dir() / "credentials.enc"


def _fernet() -> Fernet:
    key_file = _vault_dir() / ".key"
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
    return Fernet(key_file.read_bytes())


def _load_all(strict: bool) -> dict[str, str]:
    cred_file = _cred_file()
    if not cred_file.exists():
        return {}
    try:
        return json.loads(_fernet().decrypt(cred_file.read_bytes()))
    except (InvalidToken, ValueError) as e:
        msg = f"cannot decrypt {cred_file} (key mismatch or corruption)"
        if strict:
            raise VaultError(f"{msg}; refusing to overwrite stored credentials") from e
        print(f"[vault] warning: {msg}", file=sys.stderr)
        return {}


def _save_all(creds: dict[str, str]) -> None:
    _cred_file().write_bytes(_fernet().encrypt(json.dumps(creds).encode()))


def store_credential(account: str, value: str) -> None:
    creds = _load_all(strict=True)
    creds[account] = value
    _save_all(creds)


def get_credential(account: str) -> str | None:
    return _load_all(strict=False).get(account)


def delete_credential(account: str) -> bool:
    creds = _load_all(strict=True)
    if account not in creds:
        return False
    del creds[account]
    _save_all(creds)
    return True


def store_cookies(domain: str, cookies: list[dict]) -> None:
    store_credential(f"cookies:{domain}", json.dumps(cookies))


def get_cookies(domain: str) -> list[dict] | None:
    raw = get_credential(f"cookies:{domain}")
    return json.loads(raw) if raw else None


def store_api_key(domain: str, key: str, auth_type: str = "bearer") -> None:
    store_credential(f"apikey:{domain}", json.dumps({"key": key, "auth_type": auth_type}))


def get_api_key(domain: str) -> dict | None:
    raw = get_credential(f"apikey:{domain}")
    return json.loads(raw) if raw else None


def delete_api_key(domain: str) -> bool:
    return delete_credential(f"apikey:{domain}")


def list_api_keys() -> list[str]:
    return [k.removeprefix("apikey:") for k in _load_all(strict=False) if k.startswith("apikey:")]
