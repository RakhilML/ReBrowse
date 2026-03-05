"""Extract cookies from Chrome and Firefox local databases."""

from __future__ import annotations
import os
import sys
import sqlite3
import shutil
import tempfile
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class BrowserCookie:
    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = False
    httpOnly: bool = False
    sameSite: str = "Lax"
    expires: float = -1


@dataclass
class ExtractionResult:
    cookies: list[BrowserCookie] = field(default_factory=list)
    source: str | None = None
    warnings: list[str] = field(default_factory=list)


def _chrome_cookie_paths() -> list[Path]:
    """Find Chrome cookie database paths per platform."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        base = Path.home() / ".config" / "google-chrome"

    paths = []
    for profile in ["Default", "Profile 1", "Profile 2", "Profile 3"]:
        p = base / profile / "Cookies"
        if p.exists():
            paths.append(p)
    return paths


def _firefox_cookie_paths() -> list[Path]:
    """Find Firefox cookie database paths per platform."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "Profiles"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles"
    else:
        base = Path.home() / ".mozilla" / "firefox"

    paths = []
    if base.exists():
        for profile_dir in base.iterdir():
            if profile_dir.is_dir():
                p = profile_dir / "cookies.sqlite"
                if p.exists():
                    paths.append(p)
    return paths


def _domain_match(cookie_domain: str, target_domain: str) -> bool:
    """Check if a cookie domain matches the target."""
    cd = cookie_domain.lstrip(".").lower()
    td = target_domain.lower()
    return td == cd or td.endswith(f".{cd}")


def extract_from_firefox(domain: str) -> ExtractionResult:
    """Extract cookies from Firefox (unencrypted SQLite)."""
    result = ExtractionResult(source="firefox")
    paths = _firefox_cookie_paths()

    if not paths:
        result.warnings.append("No Firefox cookie databases found")
        return result

    for db_path in paths:
        try:
            # Copy to temp file to avoid lock issues
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite")
            tmp.close()
            shutil.copy2(str(db_path), tmp.name)

            conn = sqlite3.connect(tmp.name)
            rows = conn.execute(
                "SELECT name, value, host, path, isSecure, isHttpOnly, sameSite, expiry "
                "FROM moz_cookies"
            ).fetchall()
            conn.close()
            os.unlink(tmp.name)

            for name, value, host, path, secure, http_only, same_site, expiry in rows:
                if _domain_match(host, domain):
                    result.cookies.append(BrowserCookie(
                        name=name,
                        value=value,
                        domain=host,
                        path=path or "/",
                        secure=bool(secure),
                        httpOnly=bool(http_only),
                        sameSite=["None", "Lax", "Strict"][same_site] if isinstance(same_site, int) and 0 <= same_site <= 2 else "Lax",
                        expires=float(expiry) if expiry else -1,
                    ))
        except Exception as e:
            result.warnings.append(f"Firefox extraction error ({db_path}): {e}")

    return result


def extract_from_chrome(domain: str) -> ExtractionResult:
    """
    Extract cookies from Chrome.
    NOTE: On Windows, Chrome cookies are encrypted with DPAPI.
    We read what we can — on some systems values may be encrypted.
    """
    result = ExtractionResult(source="chrome")
    paths = _chrome_cookie_paths()

    if not paths:
        result.warnings.append("No Chrome cookie databases found")
        return result

    for db_path in paths:
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite")
            tmp.close()
            shutil.copy2(str(db_path), tmp.name)

            conn = sqlite3.connect(tmp.name)
            rows = conn.execute(
                "SELECT name, host_key, path, is_secure, is_httponly, "
                "samesite, expires_utc, encrypted_value, value "
                "FROM cookies"
            ).fetchall()
            conn.close()
            os.unlink(tmp.name)

            for name, host, path, secure, http_only, same_site, expires, encrypted_val, plain_val in rows:
                if not _domain_match(host, domain):
                    continue

                value = plain_val or ""
                # Try to decrypt on Windows
                if not value and encrypted_val and sys.platform == "win32":
                    value = _try_decrypt_windows(encrypted_val)

                if not value:
                    continue

                result.cookies.append(BrowserCookie(
                    name=name,
                    value=value,
                    domain=host,
                    path=path or "/",
                    secure=bool(secure),
                    httpOnly=bool(http_only),
                    sameSite=["no_restriction", "lax", "strict"][same_site] if isinstance(same_site, int) and 0 <= same_site <= 2 else "Lax",
                    expires=float(expires) / 1_000_000 - 11644473600 if expires else -1,
                ))
        except Exception as e:
            result.warnings.append(f"Chrome extraction error ({db_path}): {e}")

    return result


def _try_decrypt_windows(encrypted_value: bytes) -> str:
    """Try to decrypt Chrome cookies on Windows using DPAPI."""
    try:
        import win32crypt  # type: ignore
        decrypted = win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)
        return decrypted[1].decode("utf-8", errors="replace")
    except Exception:
        pass

    # Try AES-GCM (Chrome v80+)
    try:
        if not encrypted_value.startswith(b"v10") and not encrypted_value.startswith(b"v11"):
            return ""

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = _get_chrome_encryption_key()
        if not key:
            return ""

        nonce = encrypted_value[3:15]
        ciphertext = encrypted_value[15:]
        return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _get_chrome_encryption_key() -> bytes | None:
    """Get Chrome's AES encryption key on Windows."""
    try:
        import json
        import base64
        local_state_path = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data" / "Local State"
        if not local_state_path.exists():
            return None

        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)

        encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
        # Remove DPAPI prefix
        encrypted_key = encrypted_key[5:]

        import win32crypt  # type: ignore
        return win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
    except Exception:
        return None


def extract_browser_cookies(domain: str) -> ExtractionResult:
    """Try Firefox first, then Chrome. Return first success with cookies."""
    ff = extract_from_firefox(domain)
    if ff.cookies:
        return ff

    chrome = extract_from_chrome(domain)
    if chrome.cookies:
        return chrome

    # Merge warnings
    return ExtractionResult(
        cookies=[],
        source=None,
        warnings=ff.warnings + chrome.warnings,
    )
