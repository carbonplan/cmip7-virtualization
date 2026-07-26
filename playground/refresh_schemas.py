#!/usr/bin/env python
"""Fetch the real published ESGF/STAC JSON Schemas into the offline cache.

The production Transaction API validates every submission by doing a live
``httpx.get`` of each extension URL (``stac-transaction-api/src/utils.py`` L193)
and running stock ``jsonschema`` against it. To make our local mirror
*behaviourally identical but offline-repeatable*, we fetch the very same public
gh-pages URLs once and cache them under
``src/cmip7_virtualization/schema_cache/``.

Run this only when you want to re-pin against upstream; the cached copies are
committed so the test suite needs no network.

    uv run python playground/refresh_schemas.py
    uv run python playground/refresh_schemas.py --check   # report drift, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from cmip7_virtualization.stac_validation import SCHEMA_CACHE, schema_cache_path

# The schemas the Transaction API actually reaches for. The two ESGF project
# schemas are hand-committed to the gh-pages branch of ESGF/stac-transaction-api.
SCHEMAS = [
    # --- what settings.DEFAULT_EXTENSIONS pins for CMIP6 (all published) ---
    "https://esgf.github.io/stac-transaction-api/cmip6/v2.0.0/schema.json",
    "https://stac-extensions.github.io/alternate-assets/v1.2.0/schema.json",
    "https://stac-extensions.github.io/file/v2.1.0/schema.json",
    # --- CMIP7 project schemas ---
    # NB v1.2.1 is what DEFAULT_EXTENSIONS pins but it is NOT PUBLISHED (404).
    # v1.2.8 is what real CMIP7 Items cite; v1.2.12 is the newest published.
    "https://esgf.github.io/stac-transaction-api/cmip7/v1.2.8/schema.json",
    "https://esgf.github.io/stac-transaction-api/cmip7/v1.2.12/schema.json",
]


def remote_refs(node: object) -> set[str]:
    """Absolute ``$ref`` URIs anywhere in a schema document.

    ``alternate-assets`` refs ``schemas.stacspec.org``, so the cache has to
    follow transitive references or validation silently reaches the network.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(("http://", "https://")):
            found.add(ref.split("#")[0])
        for value in node.values():
            found |= remote_refs(value)
    elif isinstance(node, list):
        for value in node:
            found |= remote_refs(value)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report whether the cache differs from upstream; write nothing.",
    )
    args = parser.parse_args(argv)

    SCHEMA_CACHE.mkdir(parents=True, exist_ok=True)
    drift = 0
    pending = list(SCHEMAS)
    seen: set[str] = set()

    while pending:
        uri = pending.pop(0)
        if uri in seen:
            continue
        seen.add(uri)

        path = schema_cache_path(uri)
        try:
            resp = httpx.get(uri, timeout=60, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            print(f"  !! {uri}\n     HTTP {exc.response.status_code} — NOT PUBLISHED")
            drift += 1
            continue

        schema = resp.json()
        pending.extend(sorted(remote_refs(schema) - seen))

        new = json.dumps(schema, indent=1, sort_keys=True) + "\n"
        old = path.read_text() if path.is_file() else None

        if old == new:
            print(f"  ok {uri}")
        elif args.check:
            print(
                f"  !! {uri}\n     cache is STALE ({'absent' if old is None else 'differs'})"
            )
            drift += 1
        else:
            path.write_text(new)
            print(
                f"  -> {uri}\n     {'created' if old is None else 'updated'} {path.name}"
            )

    if args.check and drift:
        print(f"\n{drift} schema(s) drifted or unavailable.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
