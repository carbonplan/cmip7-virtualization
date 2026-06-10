"""Query a source STAC catalog and mirror suitable Items into the Playground.

Factored-out "prepopulate" step for the Track C esgadd demo. *Suitable* means an
Item whose data assets point to NetCDF on a real, reachable host — so we can
later build an Icechunk store from the actual files.

Source catalog: the CEDA East *production* catalog is empty right now
(2026-06-09), so we default to the live ESGF-West discovery API. Two caveats with
the current West data-challenge content:

* West serves **no kerchunk ``reference_file`` assets**, so the demo adds the
  *first* virtual reference to an Item rather than a second one alongside kerchunk.
* Many Items carry **dummy data hrefs** (``esgf-test.test.gov`` / ``app.globus.org``);
  we keep only Items with NetCDF on a known-reachable node (``REACHABLE_HOSTS``).

stac-fastapi-es uses the STAC transactions extension: **POST** to create an Item,
**PUT** to update an existing one. ``put_item`` does POST-then-PUT-on-409.
"""

from __future__ import annotations

from typing import List, Optional

import httpx

from cmip7_virtualization.catalog import urls_from_stac_item

# CEDA East prod is empty (2026-06-09); api.stac.esgf-west.org has no DNS yet —
# the integration/data-challenge discovery host is the one that serves Items.
# WEST_DISCOVERY = "https://api.stac.esgf.ceda.ac.uk"   # CEDA East prod — empty
WEST_DISCOVERY = "https://discovery.integration.esgf-west.org"  # ESGF-West discovery (live)

# Data nodes that actually serve the NetCDF over HTTP (exclude dummy/test hosts
# like esgf-test.test.gov and globus-only app.globus.org hrefs).
REACHABLE_HOSTS = {
    "esgf-node.ornl.gov",
    "esgf.nci.org.au",
    "noresg.nird.sigma2.no",
}


def reachable_data_urls(item: dict) -> List[str]:
    """NetCDF hrefs of an Item that live on a known-reachable host."""
    return [
        u
        for u in urls_from_stac_item(item)
        if u.startswith("http") and u.endswith(".nc") and u.split("/")[2] in REACHABLE_HOSTS
    ]


def fetch_source_items(
    source_stac: str = WEST_DISCOVERY,
    collection: str = "CMIP6",
    n: int = 2,
    *,
    page: int = 100,
    timeout: float = 60.0,
) -> List[dict]:
    """Return up to ``n`` source Items that have reachable-host NetCDF assets."""
    feats = httpx.get(
        f"{source_stac}/collections/{collection}/items?limit={page}", timeout=timeout
    ).json().get("features", [])
    out = [f for f in feats if reachable_data_urls(f)]
    return out[:n]


def ensure_collection(
    stac_url: str,
    collection: str = "CMIP6",
    *,
    source_stac: str = WEST_DISCOVERY,
    timeout: float = 30.0,
) -> None:
    """Create ``collection`` in the Playground, mirroring metadata from the source.

    The source collection id is lowercase (``cmip6``); we rewrite it to
    ``collection`` so it matches the dataset_id project prefix esgadd derives.
    stac-fastapi-es rejects several top-level fields, so they are stripped.
    Idempotent: an existing collection (409) is left as-is.
    """
    src = httpx.get(f"{source_stac}/collections/{collection.lower()}", timeout=timeout).json()
    src["id"] = collection
    for field in ("assets", "links", "item_assets", "summaries"):
        src.pop(field, None)
    r = httpx.post(f"{stac_url}/collections", json=src, timeout=timeout)
    if r.status_code == 409:
        print(f"Collection {collection} already exists — skipping")
    elif r.status_code in (200, 201):
        print(f"✓ Collection created: {collection}")
    else:
        r.raise_for_status()


def put_item(stac_url: str, collection: str, item: dict, *, timeout: float = 30.0) -> None:
    """Create (POST) or update (PUT on 409) an Item in the Playground."""
    iid = item["id"]
    r = httpx.post(f"{stac_url}/collections/{collection}/items", json=item, timeout=timeout)
    if r.status_code == 409:
        r = httpx.put(f"{stac_url}/collections/{collection}/items/{iid}", json=item, timeout=timeout)
    if r.status_code not in (200, 201):
        r.raise_for_status()


def mirror_items(
    stac_url: str,
    items: List[dict],
    collection: str = "CMIP6",
    *,
    strip_reference: bool = True,
    timeout: float = 30.0,
) -> List[str]:
    """Mirror source Items into the Playground; return the seeded ids.

    ``strip_reference`` drops any ``reference_file`` asset so esgadd's JSON-Patch
    ``add`` lands cleanly at ``/assets/reference_file`` (rather than nesting under
    ``/assets/reference_file/alternate/<site>``, which needs the ``alternate``
    object to pre-exist — an RFC-6902 / esgadd gap). West has none today, but we
    keep the guard for when the source catalog regains kerchunk refs.
    """
    seeded: List[str] = []
    for src in items:
        item = dict(src)
        item.pop("links", None)
        item["collection"] = collection
        if strip_reference:
            item.get("assets", {}).pop("reference_file", None)
        put_item(stac_url, collection, item, timeout=timeout)
        seeded.append(item["id"])
        print(f"✓ Seeded {item['id']}")
    return seeded


def prepopulate(
    stac_url: str,
    *,
    source_stac: str = WEST_DISCOVERY,
    collection: str = "CMIP6",
    n: int = 2,
    ensure_coll: bool = True,
    timeout: float = 30.0,
) -> List[str]:
    """Query ``source_stac`` and mirror ``n`` suitable Items into the Playground."""
    items = fetch_source_items(source_stac, collection, n, timeout=timeout)
    if not items:
        raise RuntimeError(
            f"No suitable Items in {source_stac}/{collection} "
            f"(need NetCDF on {sorted(REACHABLE_HOSTS)})."
        )
    if ensure_coll:
        ensure_collection(stac_url, collection, source_stac=source_stac, timeout=timeout)
    return mirror_items(stac_url, items, collection, timeout=timeout)


if __name__ == "__main__":
    # Quick standalone check: which source Items would we mirror?
    for it in fetch_source_items():
        print(it["id"], "->", len(reachable_data_urls(it)), "reachable NetCDF file(s)")
