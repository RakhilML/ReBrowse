"""Configuration — all settings from .env, no heavy deps."""

import os
from pathlib import Path

from dotenv import load_dotenv

from rebrowse import __version__

load_dotenv(Path(__file__).parent.parent / ".env")

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "local")  # local | openai | gemini | claude | ollama
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL_INTENT = os.getenv("LLM_MODEL_INTENT", "qwen/qwen3-8b")
LLM_MODEL_CODE = os.getenv("LLM_MODEL_CODE", "qwen/qwen3-coder-next")

DATA_DIR = Path(os.getenv("REBROWSE_DATA_DIR") or Path.home() / ".rebrowse")
CAPTURES_DIR = DATA_DIR / "captures"
DB_PATH = DATA_DIR / "skills.db"
VAULT_DIR = DATA_DIR / "vault"

REBROWSE_UA = os.getenv(
    "REBROWSE_UA",
    f"rebrowse/{__version__} (+https://github.com/RakhilML/ReBrowse)",
)

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
MAX_CONCURRENT_BROWSERS = 3
CAPTURE_TIMEOUT_MS = 90_000
CAPTURE_SETTLE_S = 2.5
MAX_BODY_SIZE = 512 * 1024
MAX_JS_BUNDLES = 20
MAX_BUNDLE_CHARS = 2_000_000
MAX_RESULT_CHARS = 20_000
HOST_MIN_INTERVAL_S = float(os.getenv("REBROWSE_HOST_INTERVAL", "1.0"))


def ensure_dirs():
    for d in [DATA_DIR, CAPTURES_DIR, VAULT_DIR]:
        d.mkdir(parents=True, exist_ok=True)
