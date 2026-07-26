"""Pure-Python replica of ``esgadd``'s JSON-Patch construction.

``esgadd`` is the console script for ``esgcet.esgstacaddrep`` in
`ESGF/esg-publisher <https://github.com/ESGF/esg-publisher>`_ — literally "STAC
add **replica**". Its ``--agg`` mode attaches an aggregation (zarr / kerchunk /
virtualizarr / icechunk) to an already-published Item by PATCHing it.

Installing ``esgadd`` needs a separate virtualenv (its dependency set conflicts
with the virtualizarr stack) and, on some tags, three packaging-bug workarounds.
That makes it unusable in CI. This module reproduces exactly the request esgadd
builds, so the JSON-Patch construction and its schema consequences stay testable
with no binary present — and so a test can assert that the real binary, when it
*is* available, emits byte-identical operations.

Ported from ``src/python/esgcet/stac_converter.py`` @ ``main`` (``59bb778``)::

    class ESGSTACItem:
        def add_aggregate(self, aggtype, url, site):
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            value = {
                "href": url,
                "type": f"application/{aggtype}",
                "role": ["data", "virtual"],
                "description": "TEST",
                "alternate:name": site,
                "created": now,
                "updated": now,
            }
            if "reference_file" in self.stac_item.get("assets", {}):
                path = f"/assets/reference_file/alternate/{site}"
            else:
                path = f"/assets/reference_file"
            operations = [{"op": "add", "path": path, "value": value}]
            return operations

and ``src/python/esgcet/stac_client.py`` L264-286, which sends them as
``PATCH {stac_api}/collections/{collection}/items/{item_id}`` with
``Content-Type: application/json-patch+json``.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = [
    "AGG_TYPES",
    "add_aggregate_ops",
    "aggregate_asset",
    "collection_for_dataset_id",
    "remove_aggregate_ops",
]

#: The values ``esgadd --agg`` advertises (``esgstacaddrep.py`` L71-75).
#: Nothing validates the string: ``--agg banana`` yields ``application/banana``.
AGG_TYPES = ("zarr", "kerchunk", "virtualizarr", "icechunk")


def _now() -> str:
    """esgadd's timestamp format (``stac_converter.py`` L43)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def aggregate_asset(
    aggtype: str, url: str, site: str, *, now: str | None = None
) -> dict:
    """The asset dict ``esgadd --agg`` writes.

    Reproduced verbatim, quirks included, because the quirks are the finding:

    * ``type`` is ``f"application/{aggtype}"`` — so ``--agg icechunk`` emits the
      unregistered media type ``application/icechunk``, not the
      ``application/vnd.zarr+icechunk`` that ``xpystac`` keys on.
    * ``"role"`` is **singular**; STAC mandates ``"roles"``. Readers that filter
      on ``roles`` (including this package's ``catalog.files_from_stac_item``)
      cannot see the asset at all.
    * ``"description"`` is the hardcoded string ``"TEST"``.
    * There is **no** ``protocol`` key — which every ESGF project schema
      requires of every asset. See :mod:`cmip7_virtualization.stac_validation`.
    """
    stamp = now or _now()
    return {
        "href": url,
        "type": f"application/{aggtype}",
        "role": ["data", "virtual"],
        "description": "TEST",
        "alternate:name": site,
        "created": stamp,
        "updated": stamp,
    }


def add_aggregate_ops(
    item: dict, aggtype: str, url: str, site: str, *, now: str | None = None
) -> list[dict]:
    """Build the RFC-6902 operation list for ``esgadd --agg <aggtype>``.

    The asset key is always ``reference_file`` — esgadd cannot write any other
    key. If the Item already has one, the new aggregation is nested at
    ``/assets/reference_file/alternate/{site}`` instead.

    .. warning::
       That nesting is where a *second* aggregation breaks. RFC-6902 ``add``
       requires the parent object to exist, and esgadd never emits an op to
       create ``alternate: {}`` first. Since its own first-aggregation asset has
       no ``alternate`` key, running ``esgadd --agg`` twice produces a patch that
       cannot be applied. Verify with
       :func:`cmip7_virtualization.stac_validation.apply_json_patch`.

       It is also semantically wrong even when it applies: the `alternate-assets
       <https://github.com/stac-extensions/alternate-assets>`_ extension is for
       the *identical file* reachable by another route (same checksum, same
       size). A kerchunk sidecar and an Icechunk store are different objects and
       belong in separate top-level assets — which is what
       :mod:`cmip7_virtualization.references` builds.
    """
    value = aggregate_asset(aggtype, url, site, now=now)
    if "reference_file" in item.get("assets", {}):
        path = f"/assets/reference_file/alternate/{site}"
    else:
        path = "/assets/reference_file"
    return [{"op": "add", "path": path, "value": value}]


def remove_aggregate_ops(item: dict, site: str) -> list[dict]:
    """Operations for ``esgunpublish --agg`` (``stac_converter.py`` L25-39)."""
    assets = item.get("assets", {})
    reference = assets.get("reference_file")
    if reference is None:
        return []
    if site in reference.get("alternate", {}):
        return [{"op": "remove", "path": f"/assets/reference_file/alternate/{site}"}]
    if reference.get("alternate:name") == site:
        return [{"op": "remove", "path": "/assets/reference_file"}]
    return []


def collection_for_dataset_id(dataset_id: str) -> str:
    """Collection esgadd derives from a dataset id (``search_check.py`` L60-65).

    The first dot-separated component, except that the CMIP7 DRS prefix
    ``MIP-DRS7`` maps to ``CMIP7``.
    """
    head = dataset_id.split(".")[0]
    return "CMIP7" if head == "MIP-DRS7" else head
