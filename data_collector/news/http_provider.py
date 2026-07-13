"""Generic HTTP JSON news provider (httpx). Vendor-neutral skeleton for the Faza 2 news
subphase: it maps a JSON news API onto the bitemporal NewsItem model and enforces the
point-in-time as_of view. Tested via httpx.MockTransport — no live vendor is wired yet.

Expected JSON shape (one object per revision):
    {"source": "...", "external_id": "...", "revision": 0,
     "published_at": "<iso8601>", "first_seen_at": "<iso8601>",
     "headline": "...", "impact": "low|medium|high", "sentiment_for_gold": 0.0}

`first_seen_at` (transaction time) is REQUIRED for replay-grade history. If the vendor
cannot supply a per-revision first-seen, this provider refuses to fabricate one for a
historical as_of (that would invite look-ahead) — see get_news.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .base import NewsItem, NewsProvider, as_of_view


class NewsProviderError(RuntimeError):
    """Any unrecoverable failure fetching/parsing news."""


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise NewsProviderError(f"news timestamp not tz-aware: {value!r}")
    return dt.astimezone(timezone.utc)


class HttpNewsProvider(NewsProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        path: str = "/v1/news",
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds, transport=transport)
        self._key = api_key
        self._path = path

    async def __aenter__(self) -> "HttpNewsProvider":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _to_item(self, row: dict, *, require_first_seen: bool) -> NewsItem:
        try:
            first_seen = row.get("first_seen_at")
            if first_seen is None:
                if require_first_seen:
                    # Replay-grade correctness needs a real transaction time; refuse to invent one.
                    raise NewsProviderError(
                        f"revision {row.get('source')}:{row.get('external_id')} has no first_seen_at; "
                        "cannot be used point-in-time without look-ahead risk"
                    )
                first_seen = datetime.now(timezone.utc).isoformat()
            return NewsItem(
                source=row["source"],
                external_id=str(row["external_id"]),
                revision=int(row.get("revision", 0)),
                publication_time=_parse_dt(row["published_at"]),
                ingestion_time=_parse_dt(first_seen) if isinstance(first_seen, str) else first_seen,
                headline=row["headline"],
                impact=row.get("impact", "low"),
                sentiment_for_gold=float(row.get("sentiment_for_gold", 0.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NewsProviderError(f"malformed news row: {exc}") from exc

    async def get_news(self, symbol: str, count: int, *, as_of: datetime | None = None) -> list[NewsItem]:
        params: dict = {"symbol": symbol, "limit": count}
        if self._key:
            params["apiKey"] = self._key
        if as_of is not None:
            # Ask the vendor for the point-in-time slice; we STILL re-gate locally below.
            params["as_of"] = as_of.astimezone(timezone.utc).isoformat()
        try:
            resp = await self._client.get(self._path, params=params)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise NewsProviderError(f"news request failed: {exc}") from exc
        except ValueError as exc:
            raise NewsProviderError(f"invalid news JSON: {exc}") from exc

        rows = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise NewsProviderError("news response is not a list")
        # For a historical as_of, first_seen_at is mandatory (no fabricated transaction time).
        items = [self._to_item(r, require_first_seen=as_of is not None) for r in rows]
        # Enforce the decision view locally regardless of what the vendor returned:
        # never trust a vendor to have filtered out look-ahead for us.
        return as_of_view(items, as_of) if as_of is not None else items
