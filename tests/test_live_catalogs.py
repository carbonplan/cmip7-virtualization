"""Live monitoring tests against the ESGF STAC catalogs in ``STAC_BASES``.

These hit the network on purpose. We do not control any of these catalogs, and
every one of them has already moved under us at least once: East prod went from
empty to 391k items, East test lost its kerchunk assets, West's
``integration-testing`` host vanished, West integration's ``/collections`` started
returning 500. A fixture-backed unit test cannot see any of that. Those live in
``test_catalog.py`` and cover the parsing logic; this module covers reality.

Two markers, both applied to this file:

``live``
    Hits the network. ``uv run pytest -m "not live"`` skips the whole module.

``watch``
    Asserts that a *currently observed* server-side state still holds. A failure
    is not necessarily bad news — it means the catalogs changed and something
    here needs a decision (drop a workaround, re-point a notebook, celebrate).
    Run only the hard invariants with ``-m "live and not watch"``.

Every count is a floor, not an equality: catalogs grow, and a test that fails on
publication is a test people learn to ignore. Floors sit ~10% below the values
observed on 2026-07-25, so they catch a catalog being wiped or a collection
filter silently returning nothing, which is the failure that actually bites us.
"""

import httpx
import pytest

from cmip7_virtualization.catalog import (
    STAC_BASES,
    _search_count,
    collection_counts,
    is_reference_asset,
)

pytestmark = pytest.mark.live

EAST_PROD = "https://api.stac.esgf.ceda.ac.uk"
EAST_TEST = "https://api.stac.esgf-test.ceda.ac.uk"
WEST_INTEGRATION = "https://discovery.integration.esgf-west.org"
WEST_PROD = "https://discovery.production.esgf-west.org"

READ_BASES = [
    base
    for sites in STAC_BASES.values()
    for endpoints in sites.values()
    for base in endpoints["read"]
]

# Floors, ~10% under the 2026-07-25 probe. Collections observed at 0 are asserted
# as exactly 0 by the watch tests below instead, because "0 -> non-zero" is the
# interesting event for those, not a regression.
MIN_COUNTS = {
    EAST_PROD: {"CMIP6": 350_000, "CORDEX-CMIP6": 890},
    EAST_TEST: {"CMIP6": 26_500, "CMIP7": 8, "CORDEX-CMIP6": 17},
    WEST_INTEGRATION: {"cmip6": 7_700, "cmip6plus": 9, "cmip7": 5, "cordex-cmip6": 5},
    WEST_PROD: {},
}

# Collections observed empty on 2026-07-25. CMIP7 on East prod is the one this
# project is actually waiting for.
OBSERVED_EMPTY = {
    EAST_PROD: ["CMIP6Plus", "CMIP7", "obs4REF"],
    EAST_TEST: ["CMIP6Test", "obs4MIPs"],
    WEST_INTEGRATION: ["obs4ref"],
    WEST_PROD: ["cmip6", "cmip6plus", "cmip7", "cordex-cmip6", "obs4ref"],
}


def _sample_assets(base, limit=200):
    """Return every asset dict from the first ``limit`` items of ``/search``."""
    r = httpx.post(f"{base}/search", json={"limit": limit}, timeout=120)
    r.raise_for_status()
    return [
        a
        for f in r.json().get("features", [])
        for a in (f.get("assets") or {}).values()
    ]


# --------------------------------------------------------------------------
# Hard invariants — these must hold for any of our tooling to work at all.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("base", READ_BASES)
def test_base_is_a_reachable_stac_api(base):
    """Root document resolves and self-identifies as a STAC catalog."""
    r = httpx.get(f"{base}/", timeout=60)
    r.raise_for_status()
    root = r.json()
    assert root.get("type") == "Catalog", f"{base} root is not a STAC Catalog"
    assert any("api.stacspec.org" in c for c in root.get("conformsTo", [])), (
        f"{base} advertises no STAC conformance classes"
    )


@pytest.mark.parametrize("base", READ_BASES)
def test_collection_discovery_survives_a_broken_collections_endpoint(base):
    """``collection_counts`` returns collections for every base.

    This is the regression guard for the West ``/collections`` 500: if the root
    ``child``-link fallback in ``_collections_meta`` ever breaks, this fails here
    rather than silently in a notebook.

    ``verify=False`` on purpose. The reconciliation path pages every item in the
    catalog, which on East prod means 391k items — fine as a one-off diagnostic,
    not something to fire on every test run. The reconciliation itself is asserted
    separately below, cheaply.
    """
    counts = collection_counts(base, verify=False)
    assert counts, f"{base} exposed no collections at all"
    assert all(isinstance(n, int) for n in counts.values()), (
        f"{base} did not report match totals: {counts}"
    )


@pytest.mark.parametrize("base", READ_BASES)
def test_per_collection_counts_reconcile_with_the_unfiltered_total(base):
    """Per-collection sum must equal the unfiltered ``/search`` total.

    A shortfall means at least one collection id is being filtered with the wrong
    casing — the West ``cmip6plus`` vs ``CMIP6Plus`` bug — and those items are
    invisible to anything that filters by collection.
    """
    counts = collection_counts(base, verify=False)
    total = _search_count(base)
    assert total is not None, f"{base} would not report a match total"
    assert sum(counts.values()) == total, (
        f"{base}: collections sum to {sum(counts.values())} but /search reports "
        f"{total}; a collection id is likely being filtered with the wrong case. "
        f"Got {counts}"
    )


@pytest.mark.parametrize("base,floors", MIN_COUNTS.items())
def test_populated_collections_have_not_been_emptied(base, floors):
    """Collections we depend on still hold roughly what they held on 2026-07-25."""
    counts = collection_counts(base, verify=False)
    for cid, floor in floors.items():
        assert cid in counts, f"{base} no longer exposes collection {cid!r}"
        assert counts[cid] >= floor, (
            f"{base} {cid}: {counts[cid]} items, below the floor of {floor}. "
            f"The catalog was wiped, re-indexed, or the collection was renamed."
        )


# --------------------------------------------------------------------------
# Watch tests — assert the world is still as we last observed it. Failing means
# something changed and a decision is due, not that our code is broken.
# --------------------------------------------------------------------------


@pytest.mark.watch
def test_west_integration_collections_endpoint_still_returns_500():
    """``/collections`` on West integration is broken by one bad document.

    Every other collection serialises; ``/collections/obs4ref`` alone 500s, and
    that takes the whole listing down with it. If this test fails, upstream fixed
    it — retire the root-``child``-link fallback in ``_collections_meta`` and
    close the report against ``esgf2-us/west-discovery``.
    """
    listing = httpx.get(f"{WEST_INTEGRATION}/collections", timeout=60)
    obs4ref = httpx.get(f"{WEST_INTEGRATION}/collections/obs4ref", timeout=60)

    assert listing.status_code == 500, (
        f"/collections now returns {listing.status_code} — upstream may have fixed it"
    )
    assert obs4ref.status_code == 500, (
        f"/collections/obs4ref now returns {obs4ref.status_code} — the blamed "
        f"document is fine, so the 500 has a different cause now"
    )


@pytest.mark.watch
def test_west_search_is_still_case_sensitive_on_collection_ids():
    """West advertises ``cmip6`` but stores ``CMIP6``, and ``/search`` cares.

    If this fails, the casing was reconciled upstream and the title/upper/lower
    fan-out in ``collection_counts`` can collapse to the advertised id.
    """
    assert _search_count(WEST_INTEGRATION, ["CMIP6"]) > 0, (
        "canonical casing found nothing"
    )
    assert _search_count(WEST_INTEGRATION, ["cmip6"]) == 0, (
        "lowercase collection id now matches items — casing may be fixed upstream"
    )


@pytest.mark.watch
@pytest.mark.parametrize("base", [EAST_PROD, EAST_TEST, WEST_INTEGRATION])
def test_no_catalog_serves_reference_assets_yet(base):
    """The premise of this whole project: nobody publishes virtual refs today.

    East test carried 19,105 kerchunk ``reference_file`` assets on 2026-06-09 and
    has none now. When this fails, a catalog started serving references again —
    that is the event Track 3 has been waiting for, so go look at what it emits.
    """
    assets = _sample_assets(base)
    assert assets, f"{base} returned no assets to inspect"
    found = [a for a in assets if is_reference_asset(a)]
    assert not found, (
        f"{base} now serves {len(found)} reference asset(s), e.g. {found[0]}"
    )


@pytest.mark.watch
@pytest.mark.parametrize("base", list(OBSERVED_EMPTY))
def test_collections_observed_empty_are_still_empty(base):
    """Notably: CMIP7 on East prod, and all of West production.

    CMIP7 arriving on East prod is the single most useful thing that could happen
    to this project, so it is worth a loud test rather than a quiet notebook run.
    """
    counts = collection_counts(base, verify=False)
    non_empty = {
        cid: counts.get(cid) for cid in OBSERVED_EMPTY[base] if counts.get(cid)
    }
    assert not non_empty, (
        f"{base} now has items in previously empty collections: {non_empty}"
    )


@pytest.mark.watch
def test_retired_west_host_is_still_gone():
    """``integration-testing.api.stac.esgf-west.org`` was NXDOMAIN on 2026-07-25.

    It was removed from ``STAC_BASES``. If DNS resolves again, reconsider — it
    used to be the best-populated West endpoint.
    """
    with pytest.raises(httpx.ConnectError):
        httpx.get("https://integration-testing.api.stac.esgf-west.org/", timeout=30)
