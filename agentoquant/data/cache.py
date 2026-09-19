"""TTL cache for connector responses.

Every source in ``config/sources.yaml`` declares a ``cache_ttl_seconds``; this module is what makes
that declaration real. A connector asks :class:`~agentoquant.data.Transport` for a URL and the
transport answers from here when the cached entry is still fresh, so a repeated call inside the TTL
costs neither a quota unit nor a network round trip.

Shape
-----
One JSON file per source (``data/cache/<source>.json``) holding a flat ``{key: entry}`` map::

    {"<sha256 of method+url+params>": {"stored_at": "...", "ttl_seconds": 300,
                                       "value": <the parsed JSON response>, "meta": {...}}}

The file is written atomically (temp file + ``os.replace``) so a crash mid-write cannot leave a
half-parsed cache behind, and it is **not** a source of truth: deleting it only costs API calls.

Staleness
---------
:meth:`CacheEntry.is_fresh` is TTL-based. :meth:`CacheEntry.is_stale` means "older than the source's
own declared refresh interval" and is what the ingest layer reports in the ledger's ``is_stale``
column when it has to fall back to a cached value after a failed refresh.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentoquant.config_loader import repo_root

#: Directory under the repo root. ``/data/`` is gitignored, so the cache is never committed.
DEFAULT_CACHE_DIRNAME = Path("data") / "cache"

#: Environment override, mirroring ``AGENTOQUANT_LEDGER_PATH``'s convention.
CACHE_DIR_ENV = "AGENTOQUANT_CACHE_DIR"


def default_cache_dir() -> Path:
    """``AGENTOQUANT_CACHE_DIR`` if set, else ``<repo>/data/cache``."""
    override = os.environ.get(CACHE_DIR_ENV)
    if override:
        return Path(override)
    return repo_root() / DEFAULT_CACHE_DIRNAME


def cache_key(method: str, url: str, params: Any = None, discriminator: str | None = None) -> str:
    """A stable, filesystem-safe key for one request.

    ``params`` is serialised with sorted keys so two calls that differ only in dict ordering share a
    key. ``discriminator`` separates requests that hit the same URL with a different body (the Grok
    X Search POST, where the prompt lives in the body, not the query string).
    """
    payload = json.dumps(
        {
            "method": method.upper(),
            "url": url,
            "params": params or {},
            "discriminator": discriminator or "",
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CacheEntry:
    """One cached response plus its age bookkeeping."""

    key: str
    source: str
    value: Any
    stored_at: datetime
    ttl_seconds: int
    meta: dict = field(default_factory=dict)

    def age_seconds(self, now: datetime | None = None) -> float:
        moment = now or datetime.now(UTC)
        return max(0.0, (moment - self.stored_at).total_seconds())

    def is_fresh(self, now: datetime | None = None, ttl_seconds: int | None = None) -> bool:
        """True while the entry is inside its TTL (``ttl_seconds`` overrides the stored TTL)."""
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            return False
        return self.age_seconds(now) < ttl

    def is_stale(self, now: datetime | None = None, stale_after_seconds: int | None = None) -> bool:
        """True when the entry is older than the caller's tolerance (default: its own TTL)."""
        return not self.is_fresh(now, stale_after_seconds)

    def to_json(self) -> dict:
        return {
            "stored_at": self.stored_at.astimezone(UTC).isoformat(),
            "ttl_seconds": self.ttl_seconds,
            "value": self.value,
            "meta": self.meta,
        }

    @classmethod
    def from_json(cls, source: str, key: str, blob: dict) -> CacheEntry:
        stored = blob.get("stored_at")
        moment = datetime.fromisoformat(stored) if isinstance(stored, str) else datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return cls(
            key=key,
            source=source,
            value=blob.get("value"),
            stored_at=moment.astimezone(UTC),
            ttl_seconds=int(blob.get("ttl_seconds") or 0),
            meta=dict(blob.get("meta") or {}),
        )


class Cache:
    """A TTL cache, one JSON file per source, safe to delete at any time."""

    def __init__(self, path: Path | str | None = None, *, enabled: bool = True) -> None:
        self.dir = Path(path) if path is not None else default_cache_dir()
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self.writes = 0

    # -- paths -------------------------------------------------------------------------------

    def path_for(self, source: str) -> Path:
        safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in source)
        return self.dir / f"{safe}.json"

    def _read_source(self, source: str) -> dict:
        path = self.path_for(source)
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt cache is a cache miss, never an error: the worst case is one extra API call.
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_source(self, source: str, blob: dict) -> None:
        path = self.path_for(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
        )
        try:
            json.dump(blob, handle, ensure_ascii=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, path)

    # -- read / write ------------------------------------------------------------------------

    def get(self, source: str, key: str) -> CacheEntry | None:
        """The entry for ``key`` regardless of age, or ``None`` when absent or the cache is off.

        Does not touch the hit/miss counters: only :meth:`get_fresh` does, because only there does
        "was it fresh" mean anything.
        """
        if not self.enabled:
            return None
        blob = self._read_source(source).get(key)
        if not isinstance(blob, dict):
            return None
        return CacheEntry.from_json(source, key, blob)

    def get_fresh(
        self,
        source: str,
        key: str,
        *,
        now: datetime | None = None,
        ttl_seconds: int | None = None,
    ) -> CacheEntry | None:
        """The entry for ``key`` only when it is still fresh. Counts hits and misses."""
        entry = self.get(source, key)
        if entry is None or not entry.is_fresh(now, ttl_seconds):
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def set(
        self,
        source: str,
        key: str,
        value: Any,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
        meta: dict | None = None,
    ) -> CacheEntry:
        """Store one response. ``ttl_seconds <= 0`` disables caching for this key."""
        entry = CacheEntry(
            key=key,
            source=source,
            value=value,
            stored_at=(now or datetime.now(UTC)).astimezone(UTC),
            ttl_seconds=int(ttl_seconds),
            meta=dict(meta or {}),
        )
        if not self.enabled or ttl_seconds <= 0:
            return entry
        blob = self._read_source(source)
        blob[key] = entry.to_json()
        self._write_source(source, blob)
        self.writes += 1
        return entry

    def delete(self, source: str, key: str) -> bool:
        blob = self._read_source(source)
        if key not in blob:
            return False
        blob.pop(key)
        self._write_source(source, blob)
        return True

    def clear(self, source: str | None = None) -> int:
        """Drop one source's cache file (or every one). Returns the number of files removed."""
        removed = 0
        targets = [self.path_for(source)] if source else sorted(self.dir.glob("*.json"))
        for path in targets:
            if path.exists():
                path.unlink()
                removed += 1
        return removed

    def sources(self) -> list[str]:
        """Every source with a cache file on disk."""
        if not self.dir.exists():
            return []
        return sorted(path.stem for path in self.dir.glob("*.json"))

    def stats(self) -> dict:
        """Hit/miss counters for the run's report. Resettable via :meth:`reset_stats`."""
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
            "sources": self.sources(),
        }

    def reset_stats(self) -> None:
        self.hits = 0
        self.misses = 0
        self.writes = 0


__all__ = [
    "CACHE_DIR_ENV",
    "DEFAULT_CACHE_DIRNAME",
    "Cache",
    "CacheEntry",
    "cache_key",
    "default_cache_dir",
]
