"""Discovery against the CDR Register.

The Register is the only entry point that lists every CDR data holder and,
critically, the ``publicBaseUri`` where that holder serves its unauthenticated
Product Reference Data. No accreditation, no OAuth, no consumer consent --
just a version header.

Reference: https://www.cdr.gov.au/for-providers/how-find-data-holders-product-data-request-service
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Iterator

import httpx

LOG = logging.getLogger(__name__)

REGISTER_BASE = "https://api.cdr.gov.au/cdr-register/v1"


@dataclasses.dataclass(frozen=True, slots=True)
class DataHolder:
    """One brand in the Register. A legal entity may run several."""

    brand_id: str
    brand_name: str
    public_base_uri: str
    legal_entity_name: str
    abn: str | None
    industries: tuple[str, ...]
    last_updated: str | None

    @property
    def products_url(self) -> str:
        """Where this holder serves its product catalogue.

        The Consumer Data Standards mandate the path; only the host varies.
        """
        return f"{self.public_base_uri.rstrip('/')}/cds-au/v1/banking/products"


def _coerce(entry: dict) -> DataHolder | None:
    """Pull a DataHolder out of one Register record.

    Returns None rather than raising: the Register is a live feed of ~117
    independently-maintained records and a single malformed one must not
    abort discovery. Callers count the Nones.
    """
    base = entry.get("publicBaseUri")
    brand_id = entry.get("dataHolderBrandId")
    if not base or not brand_id:
        return None

    legal = entry.get("legalEntity") or {}
    return DataHolder(
        brand_id=brand_id,
        brand_name=entry.get("brandName") or legal.get("legalEntityName") or brand_id,
        public_base_uri=base,
        legal_entity_name=legal.get("legalEntityName") or "",
        abn=legal.get("abn"),
        industries=tuple(entry.get("industries") or ()),
        last_updated=entry.get("lastUpdated"),
    )


def parse_holders(payload: dict, industry: str = "banking") -> list[DataHolder]:
    """Parse a Register brands-summary payload. Pure, so it is testable offline."""
    records = payload.get("data") or []
    holders = []
    unreachable = []
    for entry in records:
        industries = tuple(entry.get("industries") or ())
        if industry and industry not in industries:
            # Energy and non-bank-lending brands are filtered out BEFORE the
            # reachability check. The Standards do not guarantee a usable
            # public base URI outside banking, so warning about them is noise.
            continue
        holder = _coerce(entry)
        if holder is None:
            unreachable.append(entry.get("brandName") or "<unnamed>")
            continue
        holders.append(holder)

    if unreachable:
        # A BANKING brand with no public base URI is a genuine finding: it is
        # registered as a data holder but publishes nothing anyone can read.
        LOG.warning(
            "%d of %d %s brands publish no public base URI and cannot be crawled: %s",
            len(unreachable),
            len(unreachable) + len(holders),
            industry or "registered",
            ", ".join(sorted(unreachable)),
        )
    return holders


def fetch_holders(
    industry: str = "banking",
    *,
    client: httpx.Client | None = None,
    timeout: float = 30.0,
) -> list[DataHolder]:
    """Call the Register and return its data holder brands.

    ``x-v: 1`` is mandated by the Consumer Data Standards. Without it the
    Register answers 400, which is the single most common reason a first
    attempt at this API appears to be "blocked".
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = client.get(
            f"{REGISTER_BASE}/all/data-holders/brands/summary",
            headers={"x-v": "1", "accept": "application/json"},
        )
        response.raise_for_status()
        return parse_holders(response.json(), industry=industry)
    finally:
        if owns_client:
            client.close()


def iter_products_urls(holders: list[DataHolder]) -> Iterator[tuple[DataHolder, str]]:
    for holder in holders:
        yield holder, holder.products_url
