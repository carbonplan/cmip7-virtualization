"""Faithful mirror of the ESGF Transaction API's STAC submission validation.

The production ESGF transaction endpoints validate every submission against the
project's published JSON Schema before accepting it. The stock ESGF-Playground
image does **no** validation at all (and rejects ``PATCH`` outright with HTTP
405), so a local demo run against the Playground tells you nothing about whether
a submission would survive the real catalog.

This module ports the validation logic of `ESGF/stac-transaction-api
<https://github.com/ESGF/stac-transaction-api>`_ (``src/utils.py`` @ ``3b472fe``)
so that it can be exercised offline, against cached copies of the real published
schemas. Every deviation from upstream is called out in a ``DEVIATION`` note, and
every upstream bug that changes the observable outcome is called out in a
``BUG`` note — those bugs are load-bearing: they are the reason ``esgadd``'s
output is accepted on ``PATCH`` despite being schema-invalid.

Upstream call graph being mirrored (``stac-transaction-api/src/client.py``)::

    patch_item()   L185 -> operation_to_partial_item()  utils.py L31
                        -> validate_extensions()        utils.py L106
                        -> validate_patch()             utils.py L251
    create_item()  L90  -> validate_extensions()        utils.py L106
                        -> validate_post()              utils.py L302

The two paths are **not** equivalent, which is the central finding this module
exists to make testable: see :func:`validate_patch` versus :func:`validate_post`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from functools import cache
from pathlib import Path
from typing import Any

import jsonpatch
import jsonschema
from jsonschema.exceptions import relevance
from jsonschema.protocols import Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

__all__ = [
    "DEFAULT_EXTENSIONS",
    "SCHEMA_CACHE",
    "ExpectedExtensionsMissingError",
    "ExtensionBelowMinimumError",
    "OperationNotPermittedError",
    "STACValidationError",
    "UnexpectedExtensionError",
    "apply_json_patch",
    "cached_schema_uris",
    "get_extension_validator",
    "operation_to_partial_item",
    "schema_cache_path",
    "validate_bbox",
    "validate_extensions",
    "validate_patch",
    "validate_post",
    "validate_resulting_item",
]

#: Directory holding offline copies of the real published JSON Schemas.
#: Refresh with ``python playground/refresh_schemas.py``.
SCHEMA_CACHE = Path(__file__).parent / "schema_cache"

# --- upstream: stac-transaction-api/src/settings/__init__.py ------------------
# Verbatim copy of DEFAULT_EXTENSIONS. This is what decides *which* schemas a
# submission is validated against: NOT the extensions the Item declares, but the
# per-collection defaults, with anything the Item declares checked against the
# `regex` and required to be >= the `default` version.
DEFAULT_EXTENSIONS: dict[str, dict[str, dict[str, Any]]] = {
    "CMIP6": {
        "CMIP6": {
            "regex": [
                r"https:\/\/esgf\.github\.io\/stac-transaction-api\/cmip6\/v[0-9]\.[0-9]\.[0-9]/schema\.json"
            ],
            "default": "https://esgf.github.io/stac-transaction-api/cmip6/v2.0.0/schema.json",
        },
        "alternate_assets": {
            "regex": [
                r"https:\/\/stac-extensions\.github\.io\/alternate-assets\/v[0-9]\.[0-9]\.[0-9]\/schema\.json"
            ],
            "default": "https://stac-extensions.github.io/alternate-assets/v1.2.0/schema.json",
        },
        "file": {
            "regex": [
                r"https:\/\/stac-extensions\.github\.io\/file\/v[0-9]\.[0-9]\.[0-9]/schema\.json"
            ],
            "default": "https://stac-extensions.github.io/file/v2.1.0/schema.json",
        },
    },
    "CMIP7": {
        "CMIP7": {
            "regex": [
                r"https:\/\/esgf\.github\.io\/stac-transaction-api\/cmip7\/v[0-9]\.[0-9]\.[0-9]\/schema\.json"
            ],
            "default": "https://esgf.github.io/stac-transaction-api/cmip7/v1.2.1/schema.json",
        },
        "alternate_assets": {
            "regex": [
                r"https:\/\/stac-extensions\.github\.io\/alternate-assets\/v[0-9]\.[0-9]\.[0-9]\/schema\.json"
            ],
            "default": "https://stac-extensions.github.io/alternate-assets/v1.2.0/schema.json",
        },
        "file": {
            "regex": [
                r"https:\/\/stac-extensions\.github\.io\/file\/v[0-9]\.[0-9]\.[0-9]/schema\.json"
            ],
            "default": "https://stac-extensions.github.io/file/v2.1.0/schema.json",
        },
    },
}

#: Upstream ``settings/__init__.py`` VERSION_REGEX, verbatim.
VERSION_REGEX = re.compile(
    r"/v("
    r"(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r")/"
)


# --- exceptions (mirror esgf_core_utils.models.exceptions) --------------------
class STACValidationError(Exception):
    """Submission failed JSON-Schema validation. Upstream ``STACValidationException``."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or "STAC validation error")
        self.detail = detail


class UnexpectedExtensionError(Exception):
    """Item declares an extension the collection does not expect."""


class ExpectedExtensionsMissingError(Exception):
    """Item omits an extension the collection requires (strict mode only)."""


class ExtensionBelowMinimumError(Exception):
    """Item declares an extension older than the collection's minimum."""


class OperationNotPermittedError(Exception):
    """RFC-6902 ``move``/``copy`` are rejected by the transaction API."""


# --- schema cache -------------------------------------------------------------
def schema_cache_path(uri: str, cache_dir: Path | None = None) -> Path:
    """Local cache filename for a schema URI.

    ``https://esgf.github.io/stac-transaction-api/cmip6/v2.0.0/schema.json``
    becomes ``esgf.github.io__stac-transaction-api__cmip6__v2.0.0__schema.json``,
    so the cache is flat, readable, and collision-free.
    """
    stripped = re.sub(r"^https?://", "", uri).rstrip("#")
    return (cache_dir or SCHEMA_CACHE) / stripped.replace("/", "__")


def cached_schema_uris(cache_dir: Path | None = None) -> list[str]:
    """URIs of every schema currently in the offline cache."""
    directory = cache_dir or SCHEMA_CACHE
    if not directory.is_dir():
        return []
    return sorted(
        "https://" + p.name.replace("__", "/") for p in directory.glob("*__schema.json")
    )


def _load_schema(uri: str, cache_dir: Path | None, allow_network: bool) -> dict:
    path = schema_cache_path(uri, cache_dir)
    if path.is_file():
        return json.loads(path.read_text())
    if not allow_network:
        raise UnexpectedExtensionError(
            f"Schema {uri} is not in the offline cache ({path}). "
            "Run `python playground/refresh_schemas.py` to fetch it, or pass "
            "allow_network=True."
        )
    # DEVIATION: upstream always fetches with httpx and never caches
    # (utils.py L193 `response = httpx.get(extension)`), so every single
    # validation is a live HTTP round-trip against esgf.github.io.
    import httpx

    resp = httpx.get(uri, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    schema = resp.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=1))
    return schema


def _offline_registry(cache_dir: Path | None, allow_network: bool) -> Registry:
    """A ``referencing`` registry that resolves ``$ref`` from the local cache.

    DEVIATION (deliberate): upstream builds a bare ``cls(schema)`` and lets
    ``jsonschema`` resolve remote ``$ref``\\ s by fetching them at validation
    time. The ``alternate-assets`` schema does contain one — a ``$ref`` to
    ``https://schemas.stacspec.org/v1.0.0/item-spec/json-schema/basics.json`` —
    so upstream silently makes a second network call per validation, and would
    fail closed if schemas.stacspec.org were down.

    We resolve the same refs from the cache instead, which is what makes the
    test suite genuinely offline. ``python playground/refresh_schemas.py``
    follows and caches transitive refs so nothing is missing.
    """

    def retrieve(uri: str) -> Resource:
        return Resource.from_contents(
            _load_schema(uri, cache_dir, allow_network),
            default_specification=DRAFT7,
        )

    return Registry(retrieve=retrieve)


@cache
def _validator_cached(
    uri: str, cache_dir: str | None, allow_network: bool
) -> Validator:
    directory = Path(cache_dir) if cache_dir else None
    schema = _load_schema(uri, directory, allow_network)
    # Upstream utils.py L211-213, plus the offline registry (see above).
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema, registry=_offline_registry(directory, allow_network))


def get_extension_validator(
    extension: str,
    *,
    cache_dir: Path | None = None,
    allow_network: bool = False,
) -> Validator:
    """Build a ``jsonschema`` validator for an extension URI (upstream L180)."""
    return _validator_cached(
        extension, str(cache_dir) if cache_dir else None, allow_network
    )


def get_asset_validator(
    extension: str,
    *,
    cache_dir: Path | None = None,
    allow_network: bool = False,
) -> Validator:
    """Validator for the subschema an ESGF project schema applies to *each asset*.

    NOT upstream — a lens on the upstream schema. The project schemas apply
    ``oneOf[0].properties.assets.additionalProperties`` to every asset, i.e.
    ``allOf: [require_asset_fields, asset_fields]``. Pulling that subschema out
    (carrying ``definitions`` along so the ``$ref``\\ s still resolve) lets a
    single asset be judged on its own merits, without the surrounding Item's
    ``collection``/``id``/``properties`` errors drowning out the signal.

    Useful because it answers the question that actually matters directly: is
    *this asset dict* acceptable to the federation?
    """
    schema = get_extension_validator(
        extension, cache_dir=cache_dir, allow_network=allow_network
    ).schema
    asset_schema = schema["oneOf"][0]["properties"]["assets"]["additionalProperties"]
    composed = {
        "$schema": schema.get("$schema", "http://json-schema.org/draft-07/schema#"),
        "definitions": schema["definitions"],
        **asset_schema,
    }
    cls = jsonschema.validators.validator_for(composed)
    cls.check_schema(composed)
    return cls(composed)


# --- patch -> partial item ----------------------------------------------------
def operation_to_partial_item(
    collection_id: str,
    operations: Sequence[dict],
    *,
    cache_dir: Path | None = None,
    allow_network: bool = False,
) -> dict:
    """Collapse a list of RFC-6902 operations into a *partial* Item.

    Mirrors upstream ``utils.py`` L31-85. This is the single most consequential
    piece of the transaction API to understand, because the object that gets
    schema-validated on ``PATCH`` is **this synthetic partial Item** — not the
    stored Item, and not the result of applying the patch to it.

    So for ``esgadd``'s single op::

        {"op": "add", "path": "/assets/reference_file", "value": {...}}

    the validated object is just ``{"assets": {"reference_file": {...}}}``. It
    has no ``type``, ``id``, ``collection``, ``geometry`` or ``properties``, and
    therefore cannot possibly satisfy the Item branch of the schema's top-level
    ``oneOf`` — which is exactly why :func:`validate_patch` has to discard
    ``oneOf`` errors, and why it ends up discarding everything else with them.

    DEVIATION: upstream returns a pydantic ``PartialItem``; we return the plain
    dict that ``PartialItem.model_dump_json()`` would produce. Verified
    equivalent for these inputs: ``PartialItem`` excludes unset fields, so a
    partial built only from ``/assets/...`` ops dumps to exactly this dict.
    """
    item: dict[str, Any] = {}

    for operation in operations:
        op = operation["op"]
        path = operation["path"]

        if op in ("move", "copy"):
            raise OperationNotPermittedError(f"Operation {op!r} is not permitted")

        # Upstream L48-49: a `remove` is rewritten as an `add` of None, so the
        # removed key is still *present* in the partial item and can be caught
        # by the null-key check in validate_patch.
        value = None if op == "remove" else operation.get("value")
        if op not in ("add", "replace", "remove"):
            continue

        if path.lstrip("/") == "stac_extensions":
            validate_extensions(
                collection_id=collection_id,
                item_extensions=value,
                strict=True,
                cache_dir=cache_dir,
                allow_network=allow_network,
            )

        path_parts = path.lstrip("/").split("/")

        # Upstream L61-64 is dead code: `path_parts` elements are always `str`
        # (they come from `str.split`), so `isinstance(path_parts[-1], int)` is
        # never True. Array-index paths like `/assets/x/roles/0` therefore nest
        # under the literal key "0" rather than building a list.
        nest: Any = value
        if isinstance(nest, list):
            existing: Any = dict(item)
            for path_part in path_parts:
                existing = (
                    existing.get(path_part, {}) if isinstance(existing, dict) else {}
                )
            if existing:
                nest = list(nest)
                nest.extend(existing)

        for path_part in reversed(path_parts):
            nest = {path_part: nest}

        # Upstream L79 `item |= nest` — a SHALLOW merge. Two ops touching
        # different keys under /assets therefore clobber each other: only the
        # last one survives into the validated partial item.
        item |= nest

    return item


def apply_json_patch(item: dict, operations: Sequence[dict]) -> dict:
    """Apply RFC-6902 operations to an Item, returning the new Item.

    This is what a *consumer* of the transaction API's Kafka event ultimately
    has to do. The transaction API itself never does it: ``client.py`` L242-247
    forwards the raw op list onward and returns ``202 Accepted``, so an op that
    cannot actually be applied is accepted at the API and fails downstream.

    Raises ``jsonpatch.JsonPatchException`` / ``jsonpointer.JsonPointerException``
    if the patch does not apply.
    """
    return jsonpatch.apply_patch(item, list(operations))


# --- extensions ---------------------------------------------------------------
def validate_extension_version(minimum: str, extension: str) -> None:
    """Raise if ``extension`` is older than ``minimum`` (upstream L88)."""
    minimum_match = VERSION_REGEX.search(minimum)
    extension_match = VERSION_REGEX.search(extension)
    if minimum_match is None or extension_match is None:
        raise UnexpectedExtensionError(f"Cannot read a version out of {extension!r}")

    from packaging.version import Version

    if Version(extension_match.group(1)) < Version(minimum_match.group(1)):
        raise ExtensionBelowMinimumError(
            f"{extension} is below the minimum v{minimum_match.group(1)}"
        )


def validate_extensions(
    collection_id: str,
    item_extensions: list[str] | None,
    strict: bool = False,
    *,
    cache_dir: Path | None = None,
    allow_network: bool = False,
) -> list[str]:
    """Resolve the schema list a submission is validated against (upstream L106).

    Returns the declared extensions plus any collection defaults that were
    missing. An extension that matches no expected ``regex`` is rejected.

    BUG (upstream L141) — ``if strict & len(missing_extensions) > 0:``. ``&``
    binds tighter than ``>``, so this parses as ``(strict & len(missing)) > 0``,
    i.e. a *bitwise* and of ``True`` (==1) with the count. With 2 missing
    extensions that is ``1 & 2 == 0`` → falsy, and strict mode silently passes.
    It only fires when the missing count is odd. We reproduce the behaviour
    exactly so tests observe what the server observes.
    """
    expected_extensions = {
        k: dict(v) for k, v in DEFAULT_EXTENSIONS.get(collection_id, {}).items()
    }
    item_extensions = list(item_extensions or [])

    for item_extension in item_extensions:
        expected = False
        for key, expected_extension in list(expected_extensions.items()):
            if any(
                re.compile(rx).match(str(item_extension))
                for rx in expected_extension["regex"]
            ):
                expected_extensions.pop(key)
                expected = True
                validate_extension_version(
                    minimum=expected_extension["default"], extension=str(item_extension)
                )
        if not expected:
            raise UnexpectedExtensionError(f"Unexpected extension {item_extension}")

    missing_extensions = [e["default"] for e in expected_extensions.values()]

    if strict & len(missing_extensions) > 0:
        raise ExpectedExtensionsMissingError(
            f"Missing extensions: {missing_extensions}"
        )

    item_extensions.extend(missing_extensions)
    return item_extensions


# --- geometry / bbox ----------------------------------------------------------
def validate_bbox(bbox: Sequence[float]) -> None:
    """Raise unless the bbox is within WGS84 bounds (upstream L216)."""
    minx, miny, maxx, maxy = bbox[:4]
    if not (
        -180.0 <= minx <= 180.0
        and -180.0 <= maxx <= 180.0
        and -90.0 <= miny <= 90.0
        and -90.0 <= maxy <= 90.0
    ):
        raise STACValidationError(f"Bbox is invalid: {list(bbox)}")


def validate_geometry(geometry: dict) -> None:
    """Raise unless the GeoJSON geometry is valid and WGS84 (upstream L232).

    DEVIATION: upstream always uses ``shapely``. We use it when it is importable
    and fall back to validating the ring's own bounds otherwise, so the offline
    test suite does not need a geospatial stack.
    """
    try:
        from shapely.geometry import shape
    except ImportError:
        coords: list[tuple[float, float]] = []

        def _walk(node: Any) -> None:
            if (
                isinstance(node, (list, tuple))
                and len(node) >= 2
                and all(isinstance(v, (int, float)) for v in node[:2])
            ):
                coords.append((float(node[0]), float(node[1])))
            elif isinstance(node, (list, tuple)):
                for child in node:
                    _walk(child)

        _walk(geometry.get("coordinates", []))
        if coords:
            xs = [c[0] for c in coords]
            ys = [c[1] for c in coords]
            validate_bbox([min(xs), min(ys), max(xs), max(ys)])
        return

    geometry_shape = shape(geometry)
    if not geometry_shape.is_valid:
        raise STACValidationError(f"Geometry is invalid: {geometry}")
    validate_bbox(geometry_shape.bounds)


# --- the two validation paths -------------------------------------------------
def validate_patch(
    item_id: str,
    item: dict,
    extensions: Iterable[str],
    *,
    cache_dir: Path | None = None,
    allow_network: bool = False,
) -> None:
    """Validate a PATCH submission the way the production API does (upstream L251).

    .. warning::
       Reproducing this faithfully shows it is **very nearly a no-op** for asset
       content. Three upstream behaviours compound:

       1. ``if error.validator in ["oneOf"]: continue`` (L284-285) discards the
          root ``oneOf`` error. Every ESGF project schema wraps its whole Item
          definition in a top-level ``oneOf`` (Item branch vs Collection
          branch), so *all* substantive errors — missing ``protocol``, a
          ``protocol`` value outside the enum, a malformed asset — arrive as
          nested ``.context`` entries of that one discarded error and are never
          seen.
       2. ``required`` errors are diverted into ``required_keys`` (L287-288)
          rather than raised.
       3. The rescue at L293, ``required_keys & null_keys``, cannot ever match:
          ``required_keys`` holds ``json.dumps(error.validator_value)`` — e.g.
          the string ``'["protocol"]'`` — while ``null_keys`` holds bare key
          names such as ``'protocol'``. The intersection of the two is always
          empty, so the branch is dead.

       Net effect: a PATCH can write an asset that makes the stored Item fail
       :func:`validate_post`, and the API returns ``202 Accepted``.

    Raises :class:`STACValidationError` if anything survives all three filters.
    """
    if item.get("geometry"):
        validate_geometry(item["geometry"])
    if item.get("bbox"):
        validate_bbox(item["bbox"])

    # Upstream get_null_keys (L149): strip None values, remember their names.
    def _strip_nulls(node: dict) -> tuple[dict, set]:
        null_keys: set = set()
        out: dict[str, Any] = {}
        for key, value in node.items():
            if value is None:
                null_keys.add(key)
                continue
            if isinstance(value, dict):
                sub, sub_nulls = _strip_nulls(value)
                out[key] = sub
                null_keys |= sub_nulls
            else:
                out[key] = value
        return out, null_keys

    instance, null_keys = _strip_nulls(item)

    for extension in extensions:
        validator = get_extension_validator(
            str(extension), cache_dir=cache_dir, allow_network=allow_network
        )

        required_keys: set = set()
        raise_errors: list[Any] = []
        for error in validator.iter_errors(instance):
            if error.validator in ["oneOf"]:
                continue
            elif error.validator == "required":
                required_keys.add(json.dumps(error.validator_value))
            else:
                raise_errors.append(error)

        # Upstream L293. Dead code — see the warning above — but reproduced.
        for null_key_error in required_keys & null_keys:
            raise_errors.append(
                f"Variable {null_key_error} is required and cannot be removed"
            )

        if raise_errors:
            raise STACValidationError(
                f"Item `{item_id}` failed PATCH validation against `{extension}`: "
                + "; ".join(
                    e if isinstance(e, str) else e.message for e in raise_errors
                )
            )


def validate_post(
    item_id: str,
    item: dict,
    extensions: Iterable[str],
    *,
    cache_dir: Path | None = None,
    allow_network: bool = False,
) -> None:
    """Validate a POST (item creation) the way the production API does (upstream L302).

    Unlike :func:`validate_patch` this raises on **every** error, and unwraps
    ``oneOf``/``anyOf`` failures via ``error.context[0]`` so the message names
    the real cause. This is the strict path, and it is the one that reveals
    ``esgadd``'s aggregation asset as invalid.
    """
    if item.get("geometry"):
        validate_geometry(item["geometry"])
    if item.get("bbox"):
        validate_bbox(item["bbox"])

    for extension in extensions:
        validator = get_extension_validator(
            str(extension), cache_dir=cache_dir, allow_network=allow_network
        )
        raise_errors = sorted(validator.iter_errors(item), key=relevance)
        if raise_errors:
            parts = [
                e.context[0].message if e.context else e.message for e in raise_errors
            ]
            raise STACValidationError(
                "Your request is invalid -- please ensure your request is valid and try again. "
                f"Item `{item_id}` failed validation against `{extension}`: "
                + "; ".join(parts)
            )


def validate_resulting_item(
    item_id: str,
    collection_id: str,
    item: dict,
    operations: Sequence[dict],
    *,
    cache_dir: Path | None = None,
    allow_network: bool = False,
) -> dict:
    """Apply a patch and validate the **result** with POST-strength validation.

    NOT upstream. The production API validates only the synthetic partial Item
    (:func:`validate_patch`) and forwards the ops to Kafka, so nothing in the
    request path ever checks that the patched Item is still schema-valid. This
    is the check the federation arguably *should* run; the local server exposes
    it as an opt-in strict mode so a demo can show both answers side by side.

    Returns the patched Item. Raises on an inapplicable patch or an invalid result.
    """
    patched = apply_json_patch(item, operations)
    extensions = validate_extensions(
        collection_id,
        patched.get("stac_extensions"),
        cache_dir=cache_dir,
        allow_network=allow_network,
    )
    validate_post(
        item_id, patched, extensions, cache_dir=cache_dir, allow_network=allow_network
    )
    return patched
