"""Local skill store — SQLite + sentence-transformers for semantic search."""

from __future__ import annotations
import sqlite3
import numpy as np
from typing import Optional
from sentence_transformers import SentenceTransformer
from rebrowse import config
from rebrowse.models import SkillManifest, _now

_encoder: SentenceTransformer | None = None


def _get_encoder() -> SentenceTransformer:
    global _encoder
    if _encoder is None:
        _encoder = SentenceTransformer(config.EMBEDDING_MODEL)
    return _encoder


def _embed(text: str) -> bytes:
    vec = _get_encoder().encode(text, normalize_embeddings=True)
    return vec.tobytes()


def _cosine_sim(a: bytes, b: bytes) -> float:
    va = np.frombuffer(a, dtype=np.float32)
    vb = np.frombuffer(b, dtype=np.float32)
    return float(np.dot(va, vb))


def _get_db() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(config.DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
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
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_skills_domain ON skills(domain)")
    db.commit()
    return db


def save_skill(skill: SkillManifest) -> None:
    db = _get_db()
    embed_parts = [skill.name, skill.domain, skill.description, skill.intent_signature]
    for ep in skill.endpoints:
        embed_parts.append(f"{ep.method.value} {ep.url_template}")
        if ep.description:
            embed_parts.append(ep.description)
    embed_text = " ".join(p for p in embed_parts if p)

    embedding = _embed(embed_text)
    skill.updated_at = _now()

    db.execute(
        """INSERT OR REPLACE INTO skills
           (skill_id, domain, name, description, intent_signature, manifest_json, embedding, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            skill.skill_id, skill.domain, skill.name, skill.description,
            skill.intent_signature, skill.model_dump_json(), embedding,
            skill.created_at, skill.updated_at,
        ),
    )
    db.commit()
    db.close()


DOMAIN_ALIASES = {
    "twitter.com": "x.com",
    "twitter": "x.com",
    "x": "x.com",
    "insta": "www.instagram.com",
    "instagram": "www.instagram.com",
    "instagram.com": "www.instagram.com",
    "yt": "www.youtube.com",
    "youtube": "www.youtube.com",
    "youtube.com": "www.youtube.com",
    "fb": "www.facebook.com",
    "facebook": "www.facebook.com",
    "reddit": "www.reddit.com",
    "reddit.com": "www.reddit.com",
    "amazon": "www.amazon.com",
    "amazon.com": "www.amazon.com",
    "ebay": "www.ebay.com",
    "ebay.com": "www.ebay.com",
    "spotify": "open.spotify.com",
    "spotify.com": "open.spotify.com",
    "imdb": "www.imdb.com",
    "imdb.com": "www.imdb.com",
    "twitch": "www.twitch.tv",
    "twitch.tv": "www.twitch.tv",
    "linkedin": "www.linkedin.com",
    "linkedin.com": "www.linkedin.com",
    "pinterest": "www.pinterest.com",
    "pinterest.com": "www.pinterest.com",
    "netflix": "www.netflix.com",
    "netflix.com": "www.netflix.com",
    "flipkart": "www.flipkart.com",
    "flipkart.com": "www.flipkart.com",
    "goodreads": "www.goodreads.com",
    "goodreads.com": "www.goodreads.com",
    "medium": "medium.com",
    "stackoverflow": "stackoverflow.com",
    "npm": "www.npmjs.com",
    "npmjs": "www.npmjs.com",
    "github": "github.com",
    "hn": "news.ycombinator.com",
    "hackernews": "news.ycombinator.com",
    "wikipedia": "en.wikipedia.org",
    "wiki": "en.wikipedia.org",
    "devto": "dev.to",
}


def _resolve_domain(domain: str) -> str:
    """Resolve a domain alias to its canonical form."""
    low = domain.lower().strip()
    # Strip www. for lookup
    stripped = low.removeprefix("www.")
    return DOMAIN_ALIASES.get(stripped, DOMAIN_ALIASES.get(low, domain))


def find_by_domain(domain: str) -> Optional[SkillManifest]:
    resolved = _resolve_domain(domain)
    db = _get_db()
    # Try resolved domain first, then original
    for d in [resolved, domain]:
        row = db.execute(
            "SELECT manifest_json FROM skills WHERE domain = ? ORDER BY updated_at DESC LIMIT 1",
            (d,),
        ).fetchone()
        if row:
            db.close()
            return SkillManifest.model_validate_json(row[0])
    # Try partial match
    row = db.execute(
        "SELECT manifest_json FROM skills WHERE domain LIKE ? ORDER BY updated_at DESC LIMIT 1",
        (f"%{domain}%",),
    ).fetchone()
    db.close()
    if row:
        return SkillManifest.model_validate_json(row[0])
    return None


def search_skills(query: str, limit: int = 5) -> list[tuple[SkillManifest, float]]:
    db = _get_db()
    query_embedding = _embed(query)
    rows = db.execute(
        "SELECT manifest_json, embedding FROM skills WHERE embedding IS NOT NULL"
    ).fetchall()
    db.close()

    if not rows:
        return []

    results = []
    for manifest_json, emb_bytes in rows:
        if not emb_bytes:
            continue
        score = _cosine_sim(query_embedding, emb_bytes)
        skill = SkillManifest.model_validate_json(manifest_json)
        results.append((skill, score))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:limit]


def list_all_skills() -> list[SkillManifest]:
    db = _get_db()
    rows = db.execute("SELECT manifest_json FROM skills ORDER BY updated_at DESC").fetchall()
    db.close()
    return [SkillManifest.model_validate_json(r[0]) for r in rows]


def delete_skill(skill_id: str) -> bool:
    db = _get_db()
    cursor = db.execute("DELETE FROM skills WHERE skill_id = ?", (skill_id,))
    db.commit()
    db.close()
    return cursor.rowcount > 0
