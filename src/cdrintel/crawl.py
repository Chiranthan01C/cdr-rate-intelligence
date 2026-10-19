"""Fetch product catalogues from every data holder, politely.

Two passes, because the Consumer Data Standards split the data:

  GET /banking/products        -> summaries. Names and ids. NO rates.
  GET /banking/products/{id}   -> the full record: deposit rates, lending
                                  rates, fees, eligibility, constraints.

There is no bulk detail endpoint, so the second pass is one request per
product across the whole market -- a few thousand. That is the real cost of
this project and the reason the concurrency controls below are not decoration.

Three things make this harder than a loop over URLs.

First, these are production endpoints at real banks serving a public
obligation. Concurrency is capped globally AND per host, and every request
identifies itself.

Second, holders disagree on which API version they support and answer 406
when you guess wrong, so versions are negotiated by walking down. The list
and detail endpoints have different version ceilings.

Third, a failure anywhere must not sink the run. A snapshot covering 112 of
117 holders with a named gap is useful; an exception at holder 40 is not.
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
    "cdr-rate-intelligence/0.2 (research project; contact via GitHub "
    "Chiranthan01C/cdr-rate-intelligence)"
)
GLOBAL_CONCURRENCY = 8
PER_HOST_CONCURRENCY = 2
MAX_PAGES = 40

# After this many consecutive detail failures, stop asking a holder for more.
# AMP Bank GO refused every version of GetProductDetail for all nine of its
# products in the first live run: nine products times five versions is 45
# pointless requests into someone's production estate to learn one fact.
DETAIL_FAILURE_THRESHOLD = 3

# Version ceilings come from the Consumer Data Standards endpoint versioning
# schedule, and they move. As of October 2026 GetProducts is at V5 and
# GetProductDetail at V7; for both, every version below the two most recent
# has passed its retirement date, so a compliant holder may serve ONLY the
# latest. Offering v4 and below to the whole market returned 406 from 53 of
# 115 holders, including ANZ, Westpac, Bendigo and BOQ -- they were current
# and this crawler was stale.
#
# The walk-down still reaches one version past retirement, because holders
# running behind the schedule exist: Wise serves GetProducts v3.
# https://consumerdatastandardsaustralia.github.io/standards/includes/endpoint-version-schedule/
LIST_VERSIONS = ("5", "4", "3")
DETAIL_VERSIONS = ("7", "6", "5", "4")


@dataclasses.dataclass(slots=True)
class HolderResult:
    holder: DataHolder
    summaries: list[dict] = dataclasses.field(default_factory=list)
    details: list[dict] = dataclasses.field(default_factory=list)
    error: str | None = None
    detail_failures: int = 0
    # Which API version each endpoint actually accepted. Worth recording: a
    # holder still serving a retired version is a conformance finding, and a
    # holder serving only the latest tells you it keeps up.
    list_version: str | None = None
    detail_version: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def product_ids(self) -> set[str]:
        return {p.get("productId") for p in self.summaries if p.get("productId")}


def unwrap_detail(payload: dict) -> dict | None:
    """Pull the product record out of a GetProductDetail envelope.

    The Standards wrap it as {"data": {...}}. A holder that returns the bare
    record instead is tolerated -- the marker is productId, which every
    product record carries and no envelope does.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict) and data.get("productId"):
        return data
    if payload.get("productId"):
        return payload
    return None


async def _get(client, url, headers, params=None):
    response = await client.get(url, params=params, headers=headers)
    response.raise_for_status()
    return response.json()


async def _negotiate(client, url, versions, params=None):
    """Try each version in turn, treating 406 as 'not this one'.

    Returns (payload, version). Raises the last error if none are accepted.
    """
    last = None
    for version in versions:
        headers = {"x-v": version, "accept": "application/json", "user-agent": USER_AGENT}
        try:
            return await _get(client, url, headers, params), version
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 406:
                raise
            last = exc
    raise last or RuntimeError("no supported version")


async def fetch_summaries(client, holder, gate, host_gate) -> HolderResult:
    """Pass one: every product summary for a holder, following pagination."""
    products: list[dict] = []
    try:
        async with gate, host_gate:
            first, version = await _negotiate(
                client, holder.products_url, LIST_VERSIONS, {"page": 1, "page-size": 100}
            )
        products.extend((first.get("data") or {}).get("products") or [])
        total_pages = int((first.get("meta") or {}).get("totalPages") or 1)

        for page in range(2, min(total_pages, MAX_PAGES) + 1):
            async with gate, host_gate:
                payload = await _get(
                    client,
                    holder.products_url,
                    {"x-v": version, "accept": "application/json", "user-agent": USER_AGENT},
                    {"page": page, "page-size": 100},
                )
            products.extend((payload.get("data") or {}).get("products") or [])

    except Exception as exc:  # noqa: BLE001 - one holder must not sink the run
        LOG.warning("%s summaries failed: %s", holder.brand_name, exc)
        return HolderResult(holder, error=f"{type(exc).__name__}: {exc}")

    return HolderResult(holder, summaries=products, list_version=version)


class HolderCircuit:
    """Stops a holder being hammered once it is clearly not serving detail.

    Concurrency means a few requests already in flight still land after the
    breaker trips. That is accepted: the point is to cap the damage, not to
    be exact.
    """

    def __init__(self, threshold: int = DETAIL_FAILURE_THRESHOLD):
        self.threshold = threshold
        self._consecutive: dict[str, int] = {}
        self.tripped: set[str] = set()
        self.names: dict[str, str] = {}

    def allow(self, key: str) -> bool:
        return key not in self.tripped

    def record(self, key: str, ok: bool) -> None:
        if ok:
            self._consecutive[key] = 0
            return
        count = self._consecutive.get(key, 0) + 1
        self._consecutive[key] = count
        if count >= self.threshold and key not in self.tripped:
            self.tripped.add(key)
            LOG.warning(
                "%s refused %d detail requests in a row - skipping its remaining products",
                self.names.get(key, key),
                count,
            )


async def fetch_detail(client, holder, product_id, gate, host_gate, circuit=None):
    """Returns (detail, version) -- both None when the product is unreadable."""
    key = holder.brand_id
    if circuit is not None:
        circuit.names.setdefault(key, holder.brand_name)
        if not circuit.allow(key):
            return None, None
    url = f"{holder.products_url}/{product_id}"
    try:
        async with gate, host_gate:
            payload, version = await _negotiate(client, url, DETAIL_VERSIONS)
        detail = unwrap_detail(payload)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("%s detail %s failed: %s", holder.brand_name, product_id, exc)
        if circuit is not None:
            circuit.record(key, False)
        return None, None
    if circuit is not None:
        circuit.record(key, detail is not None)
    return detail, version


async def crawl(
    holders: list[DataHolder], *, timeout: float = 30.0
) -> tuple[list[HolderResult], str]:
    """Crawl every holder: summaries, then details. Returns (results, captured_at)."""
    captured_at = dt.datetime.now(dt.timezone.utc).isoformat()
    gate = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    host_gates: dict[str, asyncio.Semaphore] = {}

    def host_gate_for(holder: DataHolder) -> asyncio.Semaphore:
        key = holder.public_base_uri
        if key not in host_gates:
            host_gates[key] = asyncio.Semaphore(PER_HOST_CONCURRENCY)
        return host_gates[key]

    limits = httpx.Limits(max_connections=GLOBAL_CONCURRENCY * 2)
    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
        results = list(
            await asyncio.gather(
                *(fetch_summaries(client, h, gate, host_gate_for(h)) for h in holders)
            )
        )

        live = [r for r in results if r.ok]
        total_products = sum(len(r.summaries) for r in live)
        LOG.info(
            "summaries: %d holders ok, %d products to detail-fetch",
            len(live),
            total_products,
        )

        circuit = HolderCircuit()
        jobs = [
            (result, pid)
            for result in live
            for pid in sorted(result.product_ids)
        ]
        details = await asyncio.gather(
            *(
                fetch_detail(
                    client, result.holder, pid, gate, host_gate_for(result.holder), circuit
                )
                for result, pid in jobs
            )
        )

    for (result, _pid), (detail, version) in zip(jobs, details):
        if detail is None:
            result.detail_failures += 1
        else:
            result.details.append(detail)
            if result.detail_version is None:
                result.detail_version = version

    failed = [r for r in results if not r.ok]
    detail_fails = sum(r.detail_failures for r in results)
    LOG.info(
        "crawled %d holders: %d ok, %d failed; %d product details, %d detail failures",
        len(results),
        len(results) - len(failed),
        len(failed),
        sum(len(r.details) for r in results),
        detail_fails,
    )
    return results, captured_at
