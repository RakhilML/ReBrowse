"""Configuration — all settings from .env, no heavy deps."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "local")  # local | openai | gemini | claude | ollama
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL_INTENT = os.getenv("LLM_MODEL_INTENT", "qwen/qwen3-8b")
LLM_MODEL_CODE = os.getenv("LLM_MODEL_CODE", "qwen/qwen3-coder-next")

DATA_DIR = Path.home() / ".rebrowse"
SKILLS_DIR = DATA_DIR / "skills"
DB_PATH = DATA_DIR / "skills.db"
VAULT_DIR = DATA_DIR / "vault"

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
MAX_CONCURRENT_BROWSERS = 3
CAPTURE_TIMEOUT_MS = 90_000
MAX_BODY_SIZE = 512 * 1024
MAX_JS_BUNDLES = 20


def ensure_dirs():
    for d in [DATA_DIR, SKILLS_DIR, VAULT_DIR]:
        d.mkdir(parents=True, exist_ok=True)
