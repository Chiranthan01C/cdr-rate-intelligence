"""Fetch product catalogues from every data holder, politely.

Two things make this harder than a loop over 117 URLs.

First, these are production endpoints at real banks, serving a public good
under a regulatory obligation. A crawler that hammers them is both rude and
the fastest way to get the project's IP range blocked. Concurrency is capped
and every request identifies itself.

Second, the holders are not uniform in anything except the path. Some paginate,
some do not. Some are slow. Some return 406 unless the version header matches
exactly. A failure at one holder must never abort the run -- a partial snapshot
with a recorded failure list is useful; an exception at holder 40 of 117 is not.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import logging

import httpx

from .register import DataHolder

LOG = logging.getLogger(__name__)

USER_AGENT = (
    "cdr-rate-intelligence/0.1 (research project; contact via GitHub "
    "Chiranthan01C/cdr-rate-intelligence)"
)
MAX_CONCURRENCY = 6
MAX_PAGES = 40


@dataclasses.dataclass(slots=True)
class HolderResult:
    holder: DataHolder
    products: list[dict]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


async def _fetch_page(
    client: httpx.AsyncClient, url: str, page: int, version: str
) -> dict:
    response = await client.get(
        url,
        params={"page": page, "page-size": 100},
        headers={"x-v": version, "accept": "application/json", "user-agent": USER_AGENT},
    )
    response.raise_for_status()
    return response.json()


async def fetch_holder(
    client: httpx.AsyncClient,
    holder: DataHolder,
    semaphore: asyncio.Semaphore,
) -> HolderResult:
    """Fetch one holder's full product list, following pagination."""
    products: list[dict] = []
    async with semaphore:
        try:
            # Holders disagree on the supported version of GetProducts. Walk
            # down rather than guessing: 406 means "not this version".
            for version in ("4", "3", "2", "1"):
                try:
                    first = await _fetch_page(client, holder.products_url, 1, version)
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 406:
                        raise
            else:
                return HolderResult(holder, [], error="no supported GetProducts version")

            products.extend((first.get("data") or {}).get("products") or [])
            total_pages = int((first.get("meta") or {}).get("totalPages") or 1)

            for page in range(2, min(total_pages, MAX_PAGES) + 1):
                payload = await _fetch_page(client, holder.products_url, page, version)
                products.extend((payload.get("data") or {}).get("products") or [])

        except Exception as exc:  # noqa: BLE001 - one holder must not sink the run
            LOG.warning("%s failed: %s", holder.brand_name, exc)
            return HolderResult(holder, [], error=f"{type(exc).__name__}: {exc}")

    return HolderResult(holder, products)


async def crawl(
    holders: list[DataHolder], *, timeout: float = 30.0
) -> tuple[list[HolderResult], str]:
    """Crawl every holder. Returns (results, captured_at ISO timestamp)."""
    captured_at = dt.datetime.now(dt.timezone.utc).isoformat()
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    limits = httpx.Limits(max_connections=MAX_CONCURRENCY)

    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
        results = await asyncio.gather(
            *(fetch_holder(client, holder, semaphore) for holder in holders)
        )

    failed = [r for r in results if not r.ok]
    LOG.info(
        "crawled %d holders: %d ok, %d failed", len(results), len(results) - len(failed), len(failed)
    )
    return list(results), captured_at
