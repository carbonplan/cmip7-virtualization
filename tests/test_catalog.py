"""Tests for collection discovery in :mod:`cmip7_virtualization.catalog`.

Offline: ``httpx.get`` is monkeypatched, so these encode the two catalog shapes we
actually observed rather than hitting the live ESGF STAC APIs.
"""

import httpx
import pytest

from cmip7_virtualization import catalog

BASE = "https://example.test"

# East: /collections works, ids already canonical, titles empty.
EAST_COLLECTIONS = {
    "collections": [{"id": "CMIP6", "title": ""}, {"id": "CMIP6Test", "title": ""}]
}

# West: lowercase ids, canonical DRS casing only in the title.
WEST_COLLECTIONS = {
    "collections": [
        {"id": "cmip6", "title": "CMIP6"},
        {"id": "cmip6plus", "title": "CMIP6Plus"},
    ]
}
WEST_ROOT = {
    "links": [
        {"rel": "self", "href": f"{BASE}/"},
        {"rel": "data", "href": f"{BASE}/collections"},
        {"rel": "child", "title": "CMIP6", "href": f"{BASE}/collections/cmip6"},
        {"rel": "child", "title": "CMIP6Plus", "href": f"{BASE}/collections/cmip6plus"},
        {"rel": "child", "title": "obs4REF", "href": f"{BASE}/collections/obs4ref"},
    ]
}


def _fake_get(routes):
    """Build an ``httpx.get`` stub serving ``{path: (status, json)}``."""

    def get(url, **kwargs):
        status, payload = routes[url[len(BASE) :]]
        request = httpx.Request("GET", url)
        return httpx.Response(status, json=payload, request=request)

    return get


def test_collections_meta_prefers_collections_endpoint(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", _fake_get({"/collections": (200, WEST_COLLECTIONS)})
    )
    assert catalog._collections_meta(BASE) == [
        ("cmip6", "CMIP6"),
        ("cmip6plus", "CMIP6Plus"),
    ]


def test_collections_meta_tolerates_empty_titles(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", _fake_get({"/collections": (200, EAST_COLLECTIONS)})
    )
    assert catalog._collections_meta(BASE) == [("CMIP6", ""), ("CMIP6Test", "")]


def test_collections_meta_falls_back_to_root_children_on_500(monkeypatch):
    """One malformed collection doc 500s the whole listing; root children survive it."""
    monkeypatch.setattr(
        httpx,
        "get",
        _fake_get({"/collections": (500, None), "/": (200, WEST_ROOT)}),
    )
    assert catalog._collections_meta(BASE) == [
        ("cmip6", "CMIP6"),
        ("cmip6plus", "CMIP6Plus"),
        ("obs4ref", "obs4REF"),
    ]


def test_collections_meta_raises_when_root_also_fails(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", _fake_get({"/collections": (500, None), "/": (503, None)})
    )
    with pytest.raises(httpx.HTTPStatusError):
        catalog._collections_meta(BASE)


def test_collection_counts_ors_title_casing_into_search(monkeypatch):
    """``/search`` is case-sensitive, so the canonical title must be a candidate."""
    monkeypatch.setattr(
        httpx, "get", _fake_get({"/collections": (200, WEST_COLLECTIONS)})
    )
    seen = {}

    def fake_post(url, json, **kwargs):
        cols = json.get("collections")
        if cols is None:  # unfiltered total, used by verify
            return httpx.Response(
                200, json={"numMatched": 12}, request=httpx.Request("POST", url)
            )
        seen[cols[0]] = cols
        n = 10 if "CMIP6Plus" in cols else 2
        return httpx.Response(
            200, json={"numMatched": n}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    assert catalog.collection_counts(BASE) == {"cmip6": 2, "cmip6plus": 10}
    # CMIP6Plus is neither the upper- nor the lower-case of cmip6plus.
    assert seen["cmip6plus"] == ["cmip6plus", "CMIP6Plus", "CMIP6PLUS"]
    assert "" not in seen["cmip6"]  # empty titles never reach the request body


# ``is_reference_asset`` backs the live monitor's "has any catalog started serving
# references again?" test. A predicate that silently never matches would let that
# monitor pass forever, so pin both the hits and the misses here, offline.

REFERENCE_ASSETS = [
    # What we emit (references.MEDIA_TYPES + roles).
    {"type": "application/vnd.zarr+icechunk", "roles": ["virtual", "data"]},
    # The kerchunk spelling in references.MEDIA_TYPES ...
    {"type": "application/vnd+zarr+kerchunk", "roles": ["virtual", "data"]},
    # ... versus the one East test actually published until 2026-06.
    {"type": "application/vnd.zarr+kerchunk", "roles": ["data"]},
    # Role alone, media type unhelpful.
    {"type": "application/json", "roles": ["reference"]},
    {"type": "APPLICATION/VND.ZARR+ICECHUNK", "roles": []},
]

DATA_ASSETS = [
    {"type": "application/netcdf", "roles": ["data"]},
    {"type": "text/html", "roles": ["data"]},
    {"type": "application/netcdf"},  # no roles key at all
    {},  # empty asset
]


@pytest.mark.parametrize("asset", REFERENCE_ASSETS)
def test_is_reference_asset_detects_every_observed_spelling(asset):
    assert catalog.is_reference_asset(asset)


@pytest.mark.parametrize("asset", DATA_ASSETS)
def test_is_reference_asset_ignores_plain_data_assets(asset):
    assert not catalog.is_reference_asset(asset)
