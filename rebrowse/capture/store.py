"""Save captures for offline re-extraction (without HTML or cookies)."""

from __future__ import annotations

import glob
import os
import re
import time
from pathlib import Path

from pydantic import ValidationError

from rebrowse import config
from rebrowse.capture.har import har_capture, read_json
from rebrowse.models import CaptureResult
from rebrowse.reverse.extractor import registrable_domain

_STAMP = "-" + "[0-9]" * 8 + "-" + "[0-9]" * 6 + ".json"


def _stem(domain: str) -> str:
    return (domain or "capture").replace(":", "_").replace("/", "_")


def save_capture(capture: CaptureResult, path: str | Path | None = None) -> Path:
    config.CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    if path is None:
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = config.CAPTURES_DIR / f"{_stem(capture.domain)}-{ts}.json"
    payload = capture.model_copy(update={"html": None, "cookies": []})
    Path(path).write_text(payload.model_dump_json(), encoding="utf-8")
    return Path(path)


def write_atomic(path: Path, data: bytes) -> None:
    """Replace PATH in one step, leaving no partial file behind on failure."""
    partial = path.with_name(f".{path.name}.tmp")
    try:
        partial.write_bytes(data)
        os.replace(partial, path)
    except OSError:
        partial.unlink(missing_ok=True)
        raise


def load_capture(path: str | Path) -> CaptureResult:
    return CaptureResult.model_validate_json(Path(path).read_text(encoding="utf-8"))


def latest_capture(domain: str) -> Path | None:
    found = sorted(config.CAPTURES_DIR.glob(glob.escape(_stem(domain.lower())) + _STAMP))
    return found[-1] if found else None


def saved_domains() -> list[str]:
    stems = {p.name[:-len("-YYYYmmdd-HHMMSS.json")] for p in config.CAPTURES_DIR.glob("*" + _STAMP)}
    return sorted(re.sub(r"_(\d+)$", r":\1", stem) for stem in stems)


def load_traffic(path: str | Path, domain: str | None = None) -> CaptureResult:
    """Read a HAR file or a saved capture; raises ValueError."""
    path = Path(path)
    data = read_json(path)
    if isinstance(data, dict) and "log" in data:
        return har_capture(data, path, domain)
    try:
        capture = CaptureResult.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"{path} is neither a HAR file nor a rebrowse capture") from e
    if not domain or domain.lower() == capture.domain.lower():
        return capture
    if registrable_domain(domain) != registrable_domain(capture.domain):
        raise ValueError(f"{path} is a capture of {capture.domain}, not {domain}")
    return capture.model_copy(update={"domain": domain.lower()})
