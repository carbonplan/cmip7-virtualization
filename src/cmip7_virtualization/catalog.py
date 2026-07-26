from collections import Counter
from typing import Dict, Iterator, List, Optional, Tuple

import httpx

STAC_BASES = {
    "test": {
        "east": {
            "read": ["https://api.stac.esgf-test.ceda.ac.uk"],
            "write": ["https://api.stac.esgf-test.ceda.ac.uk"],
        },
        "west": {
            # ``integration-testing.api.stac.esgf-west.org`` was retired (NXDOMAIN
            # as of 2026-07-25); discovery.integration is now the populated one
            # (~8.6k items) and is the only integration read endpoint.
            "read": ["https://discovery.integration.esgf-west.org"],
            "write": ["https://transaction.integration.esgf-west.org"],
        },
    },
    "production": {
        "east": {
            "read": ["https://api.stac.esgf.ceda.ac.uk"],
            "write": ["https://api.stac.esgf.ceda.ac.uk"],
        },
        "west": {
            "read": ["https://discovery.production.esgf-west.org"],
            "write": ["https://transaction.production.esgf-west.org"],
        },
    },
}


def is_reference_asset(asset: Dict) -> bool:
    """True if a STAC asset is a virtual-Zarr reference rather than a data file.

    Deliberately generous about spelling. ``references.MEDIA_TYPES`` writes
    kerchunk as ``application/vnd+zarr+kerchunk`` while East test published
    ``application/vnd.zarr+kerchunk``; we emit roles ``["virtual", "data"]`` while
    other publishers use ``reference``. Substring-matching the engine name means a
    punctuation disagreement cannot make a reference asset invisible — which
    matters most for the live monitor in ``tests/test_live_catalogs.py``, whose
    whole job is to notice the day a catalog starts serving these again.
    """
    media_type = (asset.get("type") or "").lower()
    roles = {str(r).lower() for r in asset.get("roles") or []}
    return (
        "kerchunk" in media_type
        or "icechunk" in media_type
        or bool(roles & {"reference", "virtual"})
    )


def files_from_stac_item(stac_item: Dict) -> Dict[str, str]:
    """Return {asset_id: href} for all data assets (excludes reference/kerchunk assets)."""
    return {
        aid: a["href"]
        for aid, a in stac_item["assets"].items()
        if "reference" not in a.get("roles", [])
    }


def urls_from_stac_item(stac_item: Dict) -> List[str]:
    """Return ordered list of NetCDF hrefs for a single STAC item."""
    return list(files_from_stac_item(stac_item).values())


def _iter_all_items(base: str, page_size: int = 250) -> Iterator[Dict]:
    """Yield every item from a STAC ``/search``, following ``next`` links."""
    url, method, body = f"{base}/search", "POST", {"limit": page_size}
    while True:
        r = (
            httpx.post(url, json=body, timeout=120)
            if method == "POST"
            else httpx.get(url, timeout=120)
        ).json()
        features = r.get("features", [])
        yield from features
        nxt = next(
            (link for link in r.get("links", []) if link.get("rel") == "next"), None
        )
        if not nxt or not features:
            break
        url, method = nxt["href"], (nxt.get("method") or "GET").upper()
        if method == "POST":
            body = (
                {**body, **nxt.get("body", {})}
                if nxt.get("merge")
                else nxt.get("body", {})
            )


def _collections_meta(base: str) -> List[Tuple[str, str]]:
    """Return ``[(collection_id, title)]`` for a STAC base URL.

    Prefers ``/collections``. That endpoint serialises *every* collection document
    in one response, so a single malformed document takes the whole listing down
    with a 500 (integration West does exactly this: ``/collections/obs4ref`` 500s,
    which 500s ``/collections``). Fall back to the root catalog's ``child`` links,
    which name each collection individually and so survive one bad document.

    ``title`` may be empty (East test publishes ``""``); on West it carries the
    canonical DRS casing (``CMIP6Plus``) that the lowercase id (``cmip6plus``)
    loses, which matters because ``/search`` is case-sensitive on collection ids.
    """
    try:
        r = httpx.get(f"{base}/collections", timeout=30)
        r.raise_for_status()
        return [(c["id"], c.get("title") or "") for c in r.json()["collections"]]
    except (httpx.HTTPError, KeyError, ValueError):
        root = httpx.get(f"{base}/", timeout=30)
        root.raise_for_status()
        return [
            (link["href"].rstrip("/").rsplit("/", 1)[-1], link.get("title") or "")
            for link in root.json().get("links", [])
            if link.get("rel") == "child"
        ]


def _search_count(base: str, collections: Optional[List[str]] = None) -> Optional[int]:
    """Total items matching a STAC ``/search`` (None if the server won't report it)."""
    body: Dict = {"limit": 1}
    if collections is not None:
        body["collections"] = collections
    r = httpx.post(f"{base}/search", json=body, timeout=60).json()
    return next(
        (r[k] for k in ("numMatched", "numberMatched") if r.get(k) is not None),
        (r.get("context") or {}).get("matched"),
    )


def collection_counts(base: str, verify: bool = True) -> Dict[str, Optional[int]]:
    """Item count for every collection exposed by a STAC base URL.

    Returns {collection_id: count}, keyed by the id from ``/collections``. count
    is None if the server doesn't report a match total.

    West advertises collection ids as esgvoc project names in lowercase (``cmip6``,
    ``cmip6plus``) but publishes items under the canonical DRS case (``CMIP6``,
    ``CMIP6Plus``), and ``/search`` is case-sensitive. ``CMIP6Plus`` is *not* the
    upper- or lower-case of ``cmip6plus``, so we OR the collection *title* (which
    carries the canonical casing) in with ``[cid, cid.upper(), cid.lower()]``.

    With ``verify`` True (default) we reconcile the per-collection sum against the
    unfiltered total; if they disagree (or the server won't report totals), we page
    through every item once and tally by each item's real ``collection`` field. That
    is robust to any canonical casing, not just upper/lower/title.
    """
    available = _collections_meta(base)

    counts: Dict[str, Optional[int]] = {}
    for cid, title in available:
        candidates = dict.fromkeys([cid, title, cid.upper(), cid.lower()])
        counts[cid] = _search_count(base, [c for c in candidates if c])

    if verify:
        total = _search_count(base)
        got = sum(v for v in counts.values() if v)
        if total is None or got != total:
            tally = Counter(
                (f.get("collection") or "").lower() for f in _iter_all_items(base)
            )
            counts = {cid: tally.get(cid.lower(), 0) for cid, _ in available}

    return counts
