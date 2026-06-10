"""STAC asset model + selection policy for *multiple* virtual references.

A single dataset can have several virtual-reference stores that are genuinely
*different objects* — e.g. an Icechunk store on OSN and another on AWS S3, plus a
legacy kerchunk reference at CEDA. These are NOT alternate-assets of one another:
the `alternate-assets` extension requires the alternate URLs to point to the
**identical file** ("same checksum and file size"). Distinct virtual stores fail
that test, so each (engine x storage x source) is modelled as a **separate
top-level STAC asset**.

This module builds those asset dicts and provides a pure ``select_reference``
policy so a client can choose, e.g., icechunk over kerchunk and S3 over OSN.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Tuple

# Media types. icechunk uses the type xpystac keys on; kerchunk keeps CEDA's
# legacy spelling (note the ``vnd+zarr`` rather than ``vnd.zarr``).
MEDIA_TYPES: Dict[str, str] = {
    "icechunk": "application/vnd.zarr+icechunk",
    "kerchunk": "application/vnd+zarr+kerchunk",
}
_TYPE_TO_ENGINE = {v: k for k, v in MEDIA_TYPES.items()}

DEFAULT_PREFER_ENGINE: Tuple[str, ...] = ("icechunk", "kerchunk")
DEFAULT_PREFER_STORAGE: Tuple[str, ...] = ("s3", "osn", "http")


def reference_asset_key(engine: str, storage: str) -> str:
    """Asset key for a virtual reference, e.g. ``reference_icechunk_s3``."""
    return f"reference_{engine}_{storage}"


def build_reference_asset(
    engine: str,
    storage: str,
    href: str,
    *,
    source_node: str,
    region: Optional[str] = None,
    anonymous: Optional[bool] = None,
    endpoint_url: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Build a STAC asset dict for one virtual-reference store.

    Carries the media type, ``["virtual", "data"]`` roles, namespaced selection
    metadata (``cmip7:engine`` / ``cmip7:storage`` / ``cmip7:source_node``), and
    xarray-assets ``xarray:storage_options`` so a generic reader can open it.
    """
    if engine not in MEDIA_TYPES:
        raise ValueError(f"Unknown engine {engine!r}; known: {sorted(MEDIA_TYPES)}")

    asset = {
        "href": href,
        "type": MEDIA_TYPES[engine],
        "roles": ["virtual", "data"],
        "title": description or f"{engine} virtual reference on {storage}",
        "cmip7:engine": engine,
        "cmip7:storage": storage,
        "cmip7:source_node": source_node,
    }

    storage_options: Dict[str, object] = {}
    if region is not None:
        storage_options["region"] = region
    if anonymous is not None:
        storage_options["anonymous"] = anonymous
    if endpoint_url is not None:
        storage_options["endpoint_url"] = endpoint_url
    if storage_options:
        asset["xarray:storage_options"] = storage_options

    return asset


def _classify(asset: dict) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(engine, storage)`` for an asset, prop-first then best-effort."""
    engine = asset.get("cmip7:engine") or _TYPE_TO_ENGINE.get(asset.get("type", ""))
    storage = asset.get("cmip7:storage")
    if storage is None:
        href = asset.get("href", "")
        if "osn" in href:
            storage = "osn"
        elif href.startswith("s3://"):
            storage = "s3"
        elif href.startswith(("http://", "https://")):
            storage = "http"
    return engine, storage


def is_reference_asset(asset: dict) -> bool:
    """True if the asset is a virtual reference (vs a plain data asset)."""
    if "virtual" in asset.get("roles", []):
        return True
    engine, _ = _classify(asset)
    return engine is not None


def select_reference(
    assets: Dict[str, dict],
    *,
    prefer_engine: Iterable[str] = DEFAULT_PREFER_ENGINE,
    prefer_storage: Iterable[str] = DEFAULT_PREFER_STORAGE,
) -> Tuple[str, dict]:
    """Pick the best reference asset by engine then storage preference.

    Returns ``(asset_key, asset)``. Non-reference assets (e.g. NetCDF ``data``)
    are ignored. Raises ``ValueError`` if there are no reference assets.
    """
    prefer_engine = tuple(prefer_engine)
    prefer_storage = tuple(prefer_storage)

    def rank(item: Tuple[str, dict]) -> Tuple[int, int, str]:
        _, asset = item
        engine, storage = _classify(asset)
        e = prefer_engine.index(engine) if engine in prefer_engine else len(prefer_engine)
        s = prefer_storage.index(storage) if storage in prefer_storage else len(prefer_storage)
        return (e, s, item[0])  # key as final, stable tie-break

    candidates = [(k, a) for k, a in assets.items() if is_reference_asset(a)]
    if not candidates:
        raise ValueError("No virtual-reference assets found to select from.")
    return min(candidates, key=rank)
