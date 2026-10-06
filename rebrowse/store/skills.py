"""Local skill store — SQLite + sentence-transformers for semantic search."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import numpy as np

from rebrowse import config
from rebrowse.models import SKILL_SCHEMA_VERSION, SkillManifest, _now

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

# The model truncates at 256 tokens; average fixed windows so long skills aren't cut off.
EMBED_WINDOW_WORDS = 120

_encoder: SentenceTransformer | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    intent_signature TEXT DEFAULT '',
    manifest_json TEXT NOT NULL,
    embedding BLOB,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _encode(texts: list[str]) -> np.ndarray:
    global _encoder
    if _encoder is None:
        from sentence_transformers import SentenceTransformer
        _encoder = SentenceTransformer(config.EMBEDDING_MODEL)
    return np.asarray(_encoder.encode(texts, normalize_embeddings=True), dtype=np.float32)


def _embed(text: str) -> bytes:
    words = text.split() or [""]
    windows = [
        " ".join(words[i:i + EMBED_WINDOW_WORDS])
        for i in range(0, len(words), EMBED_WINDOW_WORDS)
    ]
    vec = _encode(windows).mean(axis=0)
    norm = float(np.linalg.norm(vec))
    return (vec / norm if norm else vec).astype(np.float32).tobytes()


def _cosine_sim(a: bytes, b: bytes) -> float:
    va = np.frombuffer(a, dtype=np.float32)
    vb = np.frombuffer(b, dtype=np.float32)
    return float(np.dot(va, vb)) if va.shape == vb.shape else 0.0


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_skills_domain ON skills(domain)")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _embed_text(skill: SkillManifest) -> str:
    parts = [skill.name, skill.domain, skill.description, skill.intent_signature]
    for ep in skill.endpoints:
        parts.append(f"{ep.method.value} {ep.url_template}")
        if ep.description:
            parts.append(ep.description)
    return " ".join(p for p in parts if p)


def save_skill(skill: SkillManifest) -> None:
    embedding = _embed(_embed_text(skill))
    skill.schema_version = SKILL_SCHEMA_VERSION
    skill.updated_at = _now()
    with _db() as db:
        db.execute(
            """INSERT OR REPLACE INTO skills
               (skill_id, domain, name, description, intent_signature, manifest_json,
                embedding, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (skill.skill_id, skill.domain, skill.name, skill.description,
             skill.intent_signature, skill.model_dump_json(), embedding,
             skill.created_at, skill.updated_at),
        )


DOMAIN_ALIASES = {
    "twitter.com": "x.com", "twitter": "x.com", "x": "x.com",
    "insta": "www.instagram.com", "instagram": "www.instagram.com",
    "instagram.com": "www.instagram.com",
    "yt": "www.youtube.com", "youtube": "www.youtube.com", "youtube.com": "www.youtube.com",
    "fb": "www.facebook.com", "facebook": "www.facebook.com",
    "reddit": "www.reddit.com", "reddit.com": "www.reddit.com",
    "amazon": "www.amazon.com", "amazon.com": "www.amazon.com",
    "ebay": "www.ebay.com", "ebay.com": "www.ebay.com",
    "spotify": "open.spotify.com", "spotify.com": "open.spotify.com",
    "imdb": "www.imdb.com", "imdb.com": "www.imdb.com",
    "twitch": "www.twitch.tv", "twitch.tv": "www.twitch.tv",
    "linkedin": "www.linkedin.com", "linkedin.com": "www.linkedin.com",
    "pinterest": "www.pinterest.com", "pinterest.com": "www.pinterest.com",
    "netflix": "www.netflix.com", "netflix.com": "www.netflix.com",
    "flipkart": "www.flipkart.com", "flipkart.com": "www.flipkart.com",
    "goodreads": "www.goodreads.com", "goodreads.com": "www.goodreads.com",
    "medium": "medium.com", "stackoverflow": "stackoverflow.com",
    "npm": "www.npmjs.com", "npmjs": "www.npmjs.com",
    "github": "github.com",
    "hn": "news.ycombinator.com", "hackernews": "news.ycombinator.com",
    "wikipedia": "en.wikipedia.org", "wiki": "en.wikipedia.org",
    "devto": "dev.to",
}


def _resolve_domain(domain: str) -> str:
    low = domain.lower().strip()
    return DOMAIN_ALIASES.get(low.removeprefix("www."), DOMAIN_ALIASES.get(low, low))


def _latest(db: sqlite3.Connection, where: str, arg: str) -> SkillManifest | None:
    row = db.execute(
        f"SELECT manifest_json FROM skills WHERE {where} ORDER BY updated_at DESC LIMIT 1",
        (arg,),
    ).fetchone()
    return SkillManifest.model_validate_json(row[0]) if row else None


def find_exact_domain(domain: str) -> SkillManifest | None:
    with _db() as db:
        return _latest(db, "domain = ?", domain)


def find_by_domain(domain: str) -> SkillManifest | None:
    domain = domain.strip()
    if not domain:
        return None
    with _db() as db:
        for d in dict.fromkeys([_resolve_domain(domain), domain]):
            skill = _latest(db, "domain = ?", d)
            if skill:
                return skill
        if len(domain) < 3:
            return None
        escaped = domain.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return _latest(db, "domain LIKE ? ESCAPE '\\'", f"%{escaped}%")


def resolve_skill(target: str) -> SkillManifest | None:
    with _db() as db:
        skill = _latest(db, "skill_id = ?", target)
    return skill or find_by_domain(target)


def search_skills(query: str, limit: int = 5) -> list[tuple[SkillManifest, float]]:
    with _db() as db:
        rows = db.execute(
            "SELECT manifest_json, embedding FROM skills WHERE embedding IS NOT NULL"
        ).fetchall()
    if not rows:
        return []
    query_embedding = _embed(query)
    results = [
        (SkillManifest.model_validate_json(manifest), _cosine_sim(query_embedding, emb))
        for manifest, emb in rows if emb
    ]
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:limit]


def list_all_skills() -> list[SkillManifest]:
    with _db() as db:
        rows = db.execute("SELECT manifest_json FROM skills ORDER BY updated_at DESC").fetchall()
    return [SkillManifest.model_validate_json(r[0]) for r in rows]


def delete_skill(skill_id: str) -> bool:
    with _db() as db:
        return db.execute("DELETE FROM skills WHERE skill_id = ?", (skill_id,)).rowcount > 0
