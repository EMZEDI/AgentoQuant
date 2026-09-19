"""GitHub release feeds for the sleeve B project repos (public, keyless).

``config/sources.yaml`` -> ``github_releases``: ``GET https://api.github.com/repos/{owner}/{repo}/releases``,
keyless, 30 calls/hour (GitHub's unauthenticated ceiling is 60/hour, so the default poll interval and
repo list stay under half of it: 4 repos every 600 s is 24 calls/hour). An optional ``GITHUB_TOKEN``
in the environment is used when present - it is never required, and never logged.

A release is a primary-source event for its own project (``SourceClass.GITHUB_RELEASE``), which is what
the Verifier (Task 11) needs in order to rank it above a headline about the same release. The default
repo list is the sleeve B universe from ``config/sleeves.yaml`` (VVV, TAO, RENDER, AKT) with the public
repository that actually exists for each; a repo that is renamed or removed simply yields nothing and
is logged, it never breaks the listener.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, ClassVar

from agentoquant.data.early_signals import (
    LOGGER,
    FetchError,
    HttpFetcher,
    JsonlLog,
    Listener,
    RateLimiter,
    SignalEvent,
    SignalWriter,
    as_utc,
    utcnow,
)
from agentoquant.enums import SourceClass

BASE_URL = "https://api.github.com"
RELEASES_PATH = "/repos/{owner}/{repo}/releases"

#: Quota from config/sources.yaml: 30 calls/hour.
CALLS_PER_HOUR = 30
DEFAULT_INTERVAL_SECONDS = 600.0

#: Default repos: one public repository per sleeve B project. Verified reachable 2026-09-19
#: (``opentensor/bittensor`` now redirects to ``RaoFoundation/bittensor``; httpx follows the redirect).
DEFAULT_REPOS: tuple[tuple[str, str], ...] = (
    ("RaoFoundation/bittensor", "TAO"),
    ("akash-network/node", "AKT"),
    ("veniceai/api-docs", "VVV"),
    ("rendernetwork/c4d-plugin", "RENDER"),
)

_ACCEPT_HEADER = "application/vnd.github+json"


def parse_github_releases(
    payload: Any,
    *,
    ticker: str | None = None,
    repo: str = "",
    observed_at: datetime | None = None,
    include_prereleases: bool = True,
    source: str = "github_releases",
) -> list[SignalEvent]:
    """Parse one ``/repos/{owner}/{repo}/releases`` response into release events.

    ``detected_at`` is the release's own ``published_at``: GitHub states it, so the ledger can measure
    detection latency against it. Drafts are skipped (they are not public yet).
    """
    if not isinstance(payload, list):
        return []
    observed_at = observed_at or utcnow()
    events: list[SignalEvent] = []
    for release in payload:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if release.get("prerelease") and not include_prereleases:
            continue
        url = str(release.get("html_url") or "").strip()
        tag = str(release.get("tag_name") or "").strip()
        name = str(release.get("name") or "").strip()
        if not (url or tag):
            continue
        published_raw = release.get("published_at") or release.get("created_at")
        published: datetime | None = None
        if isinstance(published_raw, str) and published_raw.strip():
            try:
                published = as_utc(datetime.fromisoformat(published_raw.strip().replace("Z", "+00:00")))
            except ValueError:
                published = None
        label = f"{repo} {tag}".strip()
        summary = f"{label}: {name}" if name else label
        events.append(
            SignalEvent(
                source_class=SourceClass.GITHUB_RELEASE,
                event_type="release",
                raw_text_or_ref=url or summary,
                detected_at=published or observed_at,
                ticker=ticker,
                source=source,
                observed_at=observed_at,
                published_at=published,
            )
        )
    return events


class GithubReleasesListener(Listener):
    """Polls the sleeve B repos' release feeds every 10 minutes (inside the 30 calls/hour budget)."""

    name: ClassVar[str] = "github_releases"
    source_class: ClassVar[SourceClass] = SourceClass.GITHUB_RELEASE
    interval_seconds: ClassVar[float] = DEFAULT_INTERVAL_SECONDS

    def __init__(
        self,
        writer: SignalWriter,
        *,
        repos: tuple[tuple[str, str], ...] | None = None,
        per_page: int = 5,
        include_prereleases: bool = True,
        token: str | None = None,
        fetcher: HttpFetcher | None = None,
        interval_seconds: float | None = None,
        log: JsonlLog | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(writer, interval_seconds=interval_seconds, log=log, **kwargs)
        self.repos = tuple(repos if repos is not None else DEFAULT_REPOS)
        self.per_page = per_page
        self.include_prereleases = include_prereleases
        # Read the token from the environment only when the caller did not pass one; never logged.
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        headers = {"Accept": _ACCEPT_HEADER}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        self.fetcher = fetcher or HttpFetcher(
            base_url=BASE_URL,
            headers=headers,
            rate_limiter=RateLimiter(3600.0 / CALLS_PER_HOUR),
            max_retries=3,
        )

    def poll(self) -> list[SignalEvent]:
        """One pass over every configured repo. A single repo failing is logged, not fatal."""
        observed_at = self._clock()
        events: list[SignalEvent] = []
        for repo, ticker in self.repos:
            owner, _, name = repo.partition("/")
            try:
                payload = self.fetcher.get_json(
                    RELEASES_PATH.format(owner=owner, repo=name),
                    params={"per_page": self.per_page},
                )
            except FetchError as exc:
                LOGGER.warning("%s: %s could not be read: %s", self.name, repo, exc)
                if self.log is not None:
                    self.log.write("repo_failed", listener=self.name, repo=repo, error=str(exc)[:200])
                continue
            events.extend(
                parse_github_releases(
                    payload,
                    ticker=ticker,
                    repo=repo,
                    observed_at=observed_at,
                    include_prereleases=self.include_prereleases,
                    source=self.name,
                )
            )
        return events

    def close(self) -> None:
        self.fetcher.close()


__all__ = [
    "BASE_URL",
    "CALLS_PER_HOUR",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_REPOS",
    "RELEASES_PATH",
    "GithubReleasesListener",
    "parse_github_releases",
]
