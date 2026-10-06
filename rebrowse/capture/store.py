"""Save captures for offline re-extraction (without HTML or cookies)."""

from __future__ import annotations

import time
from pathlib import Path

from rebrowse import config
from rebrowse.models import CaptureResult


def save_capture(capture: CaptureResult, path: str | Path | None = None) -> Path:
    config.CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    if path is None:
        ts = time.strftime("%Y%m%d-%H%M%S")
        safe_domain = (capture.domain or "capture").replace(":", "_").replace("/", "_")
        path = config.CAPTURES_DIR / f"{safe_domain}-{ts}.json"
    payload = capture.model_copy(update={"html": None, "cookies": []})
    Path(path).write_text(payload.model_dump_json(), encoding="utf-8")
    return Path(path)


def load_capture(path: str | Path) -> CaptureResult:
    return CaptureResult.model_validate_json(Path(path).read_text(encoding="utf-8"))
