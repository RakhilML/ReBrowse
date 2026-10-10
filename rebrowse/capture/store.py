"""Save captures for offline re-extraction (without HTML or cookies)."""

from __future__ import annotations

import glob
import os
import re
import time
from pathlib import Path
from typing import NoReturn

from pydantic import ValidationError

from rebrowse import config
from rebrowse.capture.har import HarError, har_capture, holds_har, is_zip, load_hars, read_json
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


_SKIPPED_DIRS = frozenset({"node_modules", "__MACOSX"})
_isjunction = getattr(os.path, "isjunction", lambda path: False)


def _unlisted(error: OSError) -> NoReturn:
    raise HarError(f"Cannot list {error.filename}: {error.strerror}") from error


def _walked(root: str, name: str) -> bool:
    return not (name.startswith(".") or name in _SKIPPED_DIRS
                or _isjunction(os.path.join(root, name)))


def _recorded(root: str, name: str) -> bool:
    if name.startswith("._"):
        return False
    path = Path(root, name)
    return name.lower().endswith(".har") or (is_zip(path) and holds_har(path))


def har_files(directory: Path) -> list[Path]:
    """The .har files and HAR archives under DIRECTORY by POSIX relative path, skipping dot
    folders, node_modules, __MACOSX and junctions; raises HarError if there are none or a zip
    is damaged."""
    found: list[Path] = []
    for root, dirs, names in os.walk(directory, onerror=_unlisted):
        dirs[:] = [name for name in dirs if _walked(root, name)]
        found += [Path(root, name) for name in names if _recorded(root, name)]
    if not found:
        raise HarError(f"No .har files under {directory}")
    return sorted(found, key=lambda path: path.relative_to(directory).as_posix())


def read_recording(path: Path, domain: str | None = None,
                   every_script: bool = False) -> tuple[CaptureResult, int]:
    """Read a HAR file, a directory of them or a saved capture, and how many HAR files a
    directory held (0 for a file); raises ValueError."""
    if path.is_dir():
        files = har_files(path)
        return load_hars(files, domain, every_script), len(files)
    return _load_file(path, domain, every_script), 0


def load_traffic(path: str | Path, domain: str | None = None,
                 every_script: bool = False) -> CaptureResult:
    """Read a HAR file, a directory of them or a saved capture; raises ValueError."""
    return read_recording(Path(path), domain, every_script)[0]


def _load_file(path: Path, domain: str | None, every_script: bool) -> CaptureResult:
    if is_zip(path):
        return load_hars([path], domain, every_script)
    data = read_json(path)
    if isinstance(data, dict) and "log" in data:
        return har_capture(data, path, domain, every_script)
    try:
        capture = CaptureResult.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"{path} is neither a HAR file nor a rebrowse capture") from e
    if not domain or domain.lower() == capture.domain.lower():
        return capture
    if registrable_domain(domain) != registrable_domain(capture.domain):
        raise ValueError(f"{path} is a capture of {capture.domain}, not {domain}")
    return capture.model_copy(update={"domain": domain.lower()})
