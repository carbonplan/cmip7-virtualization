#!/usr/bin/env python
"""A local STAC server that behaves like the live ESGF transaction endpoints.

Why not the ESGF-Playground image
---------------------------------
``ghcr.io/djspstfc/stac-fastapi-es:1.0`` cannot exercise this path at all:

* it returns **HTTP 405 on item PATCH** (base transactions class only — POST and
  PUT), so ``esgadd``'s request never reaches any handler; and
* it performs **no schema validation whatsoever**, so even if PATCH worked, a
  green run against it would say nothing about whether the real federation would
  accept the submission.

A validating reverse proxy in front of it does not help: the origin still 405s,
so the proxy would have to rewrite PATCH into PUT, which changes the very
semantics under test. ESGF-Playground is also unmaintained (last commit
2024-08-16). So this is a purpose-built ~250-line Starlette/FastAPI stub that
mirrors `ESGF/stac-transaction-api <https://github.com/ESGF/stac-transaction-api>`_
(``3b472fe``) instead: same validation entry points, same status codes, same
RFC 9457 error envelope, and the same published JSON Schemas — served from an
offline cache so runs are repeatable without network.

Two validation modes
--------------------
``--mode faithful`` (default)
    Exactly what the production API does today. PATCH validates only the
    *synthetic partial Item* built from the operations, and discards ``oneOf``
    errors — which is where all asset validation lives. Consequence: asset
    errors are invisible on PATCH.

``--mode strict``
    Additionally applies the patch and validates the **resulting** Item with
    POST-strength validation. This is the check the federation does not
    currently run, and it is what exposes ``esgadd``'s output as invalid.

Run
---
    uv run python playground/validating_stac_server.py            # :9020, faithful
    uv run python playground/validating_stac_server.py --mode strict
"""

from __future__ import annotations

import argparse
import copy
import json
import uuid
from pathlib import Path
from typing import Any

import jsonpatch
import jsonpointer
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from cmip7_virtualization.stac_validation import (
    ExpectedExtensionsMissingError,
    ExtensionBelowMinimumError,
    OperationNotPermittedError,
    STACValidationError,
    UnexpectedExtensionError,
    apply_json_patch,
    operation_to_partial_item,
    validate_extensions,
    validate_patch,
    validate_post,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: Exceptions the upstream handlers convert into RFC 9457 400s
#: (``stac-transaction-api/src/client.py`` L124-137, L220-233).
_VALIDATION_ERRORS = (
    ExpectedExtensionsMissingError,
    ExtensionBelowMinimumError,
    OperationNotPermittedError,
    STACValidationError,
    UnexpectedExtensionError,
)


class Catalog:
    """In-memory STAC store plus a request log for the demo to inspect."""

    def __init__(self, mode: str = "faithful") -> None:
        self.mode = mode
        self.collections: dict[str, dict] = {}
        self.items: dict[str, dict[str, dict]] = {}
        self.log: list[dict] = []

    def add_collection(self, collection: dict) -> None:
        self.collections[collection["id"]] = collection
        self.items.setdefault(collection["id"], {})

    def seed_fixture(self, path: Path) -> str:
        item = json.loads(path.read_text())
        collection_id = item.get("collection", "CMIP6")
        if collection_id not in self.collections:
            self.add_collection(
                {
                    "type": "Collection",
                    "id": collection_id,
                    "stac_version": "1.0.0",
                    "description": f"{collection_id} (local mirror)",
                    "license": "proprietary",
                    "extent": {
                        "spatial": {"bbox": [[-180, -90, 180, 90]]},
                        "temporal": {"interval": [[None, None]]},
                    },
                    "links": [],
                }
            )
        self.items[collection_id][item["id"]] = item
        return item["id"]

    def record(self, **kwargs: Any) -> None:
        self.log.append(kwargs)


def problem(status: int, title: str, detail: str, instance: str) -> JSONResponse:
    """RFC 9457 ``application/problem+json`` body, as the real API returns."""
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": instance,
        },
    )


def create_app(catalog: Catalog) -> FastAPI:
    app = FastAPI(title="ESGF transaction-API mirror (local)")
    app.state.catalog = catalog

    @app.get("/")
    async def root() -> dict:
        return {
            "type": "Catalog",
            "stac_version": "1.0.0",
            "id": "esgf-local-mirror",
            "title": f"ESGF transaction-API mirror ({catalog.mode} mode)",
            "description": __doc__.split("\n")[0],
            "conformsTo": [
                "https://api.stacspec.org/v1.0.0/core",
                "https://api.stacspec.org/v1.0.0/collections",
                "https://api.stacspec.org/v1.0.0/ogcapi-features",
                "https://api.stacspec.org/v1.0.0/ogcapi-features/extensions/transaction",
                # The conformance class the Playground image is missing:
                "https://api.stacspec.org/v1.0.0/ogcapi-features/extensions/transaction#patch",
            ],
            "links": [],
        }

    @app.get("/collections")
    async def get_collections() -> dict:
        return {"collections": list(catalog.collections.values()), "links": []}

    @app.post("/collections")
    async def post_collection(collection: dict) -> Response:
        if collection.get("id") in catalog.collections:
            return problem(
                409, "Conflict", "Collection already exists", uuid.uuid4().hex
            )
        catalog.add_collection(collection)
        return JSONResponse(status_code=201, content=collection)

    @app.get("/collections/{collection_id}")
    async def get_collection(collection_id: str) -> Response:
        if collection_id not in catalog.collections:
            return problem(
                404, "Not Found", f"No collection {collection_id}", uuid.uuid4().hex
            )
        return JSONResponse(catalog.collections[collection_id])

    @app.get("/collections/{collection_id}/items")
    async def get_items(collection_id: str, limit: int = 10) -> Response:
        if collection_id not in catalog.items:
            return problem(
                404, "Not Found", f"No collection {collection_id}", uuid.uuid4().hex
            )
        features = list(catalog.items[collection_id].values())[:limit]
        return JSONResponse(
            {"type": "FeatureCollection", "features": features, "links": []}
        )

    @app.get("/collections/{collection_id}/items/{item_id}")
    async def get_item(collection_id: str, item_id: str) -> Response:
        item = catalog.items.get(collection_id, {}).get(item_id)
        if item is None:
            return problem(404, "Not Found", f"No item {item_id}", uuid.uuid4().hex)
        return JSONResponse(item)

    @app.post("/collections/{collection_id}/items")
    async def create_item(collection_id: str, item: dict) -> Response:
        """Mirror ``client.py:create_item`` — validate_extensions + validate_post."""
        instance = uuid.uuid4().hex
        catalog.record(method="POST", collection=collection_id, body=item)
        try:
            extensions = validate_extensions(collection_id, item.get("stac_extensions"))
            validate_post(item.get("id", "?"), item, extensions)
        except _VALIDATION_ERRORS as exc:
            detail = getattr(exc, "detail", "") or str(exc)
            return problem(400, type(exc).__name__, detail, instance)

        catalog.items.setdefault(collection_id, {})[item["id"]] = item
        # Upstream returns 202 with a plain-text body; the Item is NOT echoed.
        return Response(status_code=202, content="Item queued for publication")

    @app.patch("/collections/{collection_id}/items/{item_id}")
    async def patch_item(
        collection_id: str, item_id: str, request: Request
    ) -> Response:
        """Mirror ``client.py:patch_item`` — the path ``esgadd --agg`` drives."""
        instance = uuid.uuid4().hex
        raw = await request.body()
        content_type = request.headers.get("content-type", "")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            return problem(400, "Bad Request", f"Body is not JSON: {exc}", instance)

        catalog.record(
            method="PATCH",
            collection=collection_id,
            item_id=item_id,
            content_type=content_type,
            body=body,
        )

        stored = catalog.items.get(collection_id, {}).get(item_id)
        if stored is None:
            return problem(404, "Not Found", f"No item {item_id}", instance)

        is_json_patch = isinstance(body, list)
        if is_json_patch and "json-patch" not in content_type:
            return problem(
                415,
                "Unsupported Media Type",
                "A JSON-Patch body requires Content-Type: application/json-patch+json",
                instance,
            )

        try:
            # --- what the production API validates: the synthetic partial Item
            partial = (
                operation_to_partial_item(collection_id, body)
                if is_json_patch
                else body
            )
            extensions = validate_extensions(
                collection_id, partial.get("stac_extensions")
            )
            validate_patch(item_id, partial, extensions)
        except _VALIDATION_ERRORS as exc:
            detail = getattr(exc, "detail", "") or str(exc)
            return problem(400, type(exc).__name__, detail, instance)

        # --- apply. The real API never does this: it queues the ops to Kafka
        # and returns 202, so an inapplicable patch is accepted here and fails
        # in the downstream consumer. We surface it, because silently accepting
        # a patch that cannot be applied would make the demo useless.
        try:
            if is_json_patch:
                patched = apply_json_patch(copy.deepcopy(stored), body)
            else:
                patched = jsonpatch.apply_patch(
                    copy.deepcopy(stored),
                    [
                        {"op": "add", "path": f"/{k}", "value": v}
                        for k, v in body.items()
                    ],
                )
        except (jsonpatch.JsonPatchException, jsonpointer.JsonPointerException) as exc:
            return problem(
                422,
                "Unprocessable Patch",
                f"RFC-6902 patch could not be applied to `{item_id}`: {exc}",
                instance,
            )

        # --- strict mode: is the RESULT still schema-valid?
        if catalog.mode == "strict":
            try:
                result_extensions = validate_extensions(
                    collection_id, patched.get("stac_extensions")
                )
                validate_post(item_id, patched, result_extensions)
            except _VALIDATION_ERRORS as exc:
                detail = getattr(exc, "detail", "") or str(exc)
                return problem(
                    400,
                    "STACValidationError",
                    f"[strict mode] the patched Item is not schema-valid: {detail}",
                    instance,
                )

        catalog.items[collection_id][item_id] = patched
        return Response(status_code=202, content="Item queued for publication")

    @app.put("/collections/{collection_id}/items/{item_id}")
    async def put_item(collection_id: str, item_id: str) -> Response:
        # Upstream client.py L176-183 raises NotImplementedError for update_item.
        return problem(
            501, "Not Implemented", "update_item is not implemented", uuid.uuid4().hex
        )

    @app.delete("/collections/{collection_id}/items/{item_id}")
    async def delete_item(collection_id: str, item_id: str) -> Response:
        return problem(
            501, "Not Implemented", "delete_item is not implemented", uuid.uuid4().hex
        )

    @app.get("/_requests")
    async def get_requests() -> dict:
        """Non-STAC introspection: every request body the server received."""
        return {"requests": catalog.log}

    return app


def build_catalog(
    mode: str = "faithful", fixtures: list[Path] | None = None
) -> Catalog:
    catalog = Catalog(mode=mode)
    for path in (
        fixtures if fixtures is not None else sorted(FIXTURES.glob("item_*.json"))
    ):
        catalog.seed_fixture(path)
    return catalog


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9020)
    parser.add_argument(
        "--mode",
        choices=("faithful", "strict"),
        default="faithful",
        help="faithful: reproduce the production API exactly. "
        "strict: also validate the patched Item (what production does NOT do).",
    )
    args = parser.parse_args(argv)

    import uvicorn

    catalog = build_catalog(args.mode)
    seeded = [i for c in catalog.items.values() for i in c]
    print(f"Seeded {len(seeded)} item(s): {seeded}")
    print(f"Validation mode: {args.mode}")
    uvicorn.run(create_app(catalog), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
