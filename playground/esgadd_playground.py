#!/usr/bin/env python
"""Track C — end-to-end ``esgadd`` demo against the local ESGF-Playground.

Proves we can attach an Icechunk virtual-reference asset to a STAC Item using the
*production* publisher tool (``esgadd`` from ESGF/esg-publisher), talking to the
local ESGF-Playground — no production auth required.

Flow (subcommands, or ``all`` to run them in order):

    seed    Query a source STAC catalog (live ESGF-West discovery, since CEDA East
            prod is empty) for a few Items with reachable-host NetCDF and mirror
            them into the Playground (playground.prepopulate). No kerchunk
            ``reference_file`` is kept, so esgadd lands the icechunk reference at
            ``/assets/reference_file``.
    build   For each seeded Item, virtualize its NetCDF URLs and write an Icechunk
            store to **OSN** (``s3://leap-pangeo-pipeline/cmip7-virtualization/``).
            An AWS-S3 hosting option is included but commented out.
    submit  Invoke ``esgadd --agg icechunk`` (subprocess) with the public OSN
            store URL as ``--agg-url`` to PATCH the reference asset onto the Item.
    verify  Read the Item back, assert the icechunk asset is present, and open the
            OSN Icechunk store directly (xpystac can't read anonymous-HTTP virtual
            chunks yet — see ESGF-INTEL.md).

``esgadd`` lives in ESGF/esg-publisher (esgf-ng branch). It has heavy deps that
conflict with the virtualizarr stack, so install it in a SEPARATE environment and
point at it with ``--esgadd /path/to/esgadd`` (default: ``esgadd`` on PATH).
See README.md.

OSN access keys are read from the environment (``AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY``); load them from 1Password first, e.g.::

    export AWS_ACCESS_KEY_ID=$(op read "op://Work/.../Access_Key")
    export AWS_SECRET_ACCESS_KEY=$(op read "op://Work/.../Secret_Access_Key")

Example
-------
    # 0. bring up the Playground (separate checkout):
    #    cd ~/Code/ESGF-Playground && docker compose up -d
    python playground/esgadd_playground.py all --n 2 \
        --esgadd ~/.venvs/esg-publisher/bin/esgadd
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import httpx
import icechunk as ic
import xarray as xr

from cmip7_virtualization.references import build_reference_asset, reference_asset_key
from cmip7_virtualization.storage import osn_storage, vccs_from_registry
from cmip7_virtualization.store import repo_exists
from cmip7_virtualization.virtualize import virtualize_from_urls

# prepopulate is a sibling module (run from the repo root, or `python -m`).
try:
    from playground.prepopulate import (
        WEST_DISCOVERY,
        fetch_source_items,
        prepopulate,
        put_item,
        reachable_data_urls,
    )
except ImportError:  # invoked as a plain script from inside playground/
    from prepopulate import (
        WEST_DISCOVERY,
        fetch_source_items,
        prepopulate,
        put_item,
        reachable_data_urls,
    )

# --- Playground topology (from ESGF-Playground/docker-compose.yml) -----------
# East node stac-fastapi-es == discovery API *and* (transactions ext) our PATCH
# target. The bundled esgf-transaction-api on :9050 is create-only (no PATCH).
LOCAL_STAC = "http://localhost:9010"
COLLECTION = "CMIP6"
CONFIG_DEFAULT = Path(__file__).resolve().parent / "esg-playground.yaml"

# --- OSN hosting for the Icechunk stores we build ----------------------------
OSN_BUCKET = "leap-pangeo-pipeline"
OSN_ENDPOINT = "https://nyu1.osn.mghpcc.org"
OSN_ROOT_PREFIX = "cmip7-virtualization"
OSN_PUBLIC_BASE = f"{OSN_ENDPOINT}/{OSN_BUCKET}"

# --- AWS S3 hosting (optional second target; uncomment in build_store) --------
# S3_BUCKET = "carbonplan-cmip7"
# S3_REGION = "us-east-1"
# S3_PREFIX_ROOT = "cmip7-virtualization"


def osn_store_href(item_id: str) -> str:
    """Public-read URL of a dataset's OSN Icechunk store (for ``--agg-url``)."""
    return f"{OSN_PUBLIC_BASE}/{OSN_ROOT_PREFIX}/{item_id}/"


# --- helpers -----------------------------------------------------------------
def check_playground() -> None:
    """Fail fast with an actionable message if the Playground isn't reachable."""
    try:
        r = httpx.get(LOCAL_STAC, timeout=5)
        r.raise_for_status()
    except httpx.ConnectError as exc:
        raise SystemExit(
            f"Cannot reach the Playground at {LOCAL_STAC}.\n"
            "Start it:  cd ~/Code/ESGF-Playground && docker compose up -d"
        ) from exc
    except httpx.TimeoutException as exc:
        raise SystemExit(
            "Playground reachable but not ready — stac-fastapi-es needs ~30-60s "
            "after 'docker compose up -d'. Wait and retry."
        ) from exc
    print(f"✓ Playground up at {LOCAL_STAC} ({r.json().get('title', 'n/a')})")


def get_item(item_id: str) -> dict:
    r = httpx.get(f"{LOCAL_STAC}/collections/{COLLECTION}/items/{item_id}", timeout=30)
    r.raise_for_status()
    return r.json()


def _osn_keys() -> tuple[str, str]:
    key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not (key and secret):
        raise SystemExit(
            "OSN keys not in env. Export them first, e.g.:\n"
            '  export AWS_ACCESS_KEY_ID=$(op read "op://Work/.../Access_Key")\n'
            '  export AWS_SECRET_ACCESS_KEY=$(op read "op://Work/.../Secret_Access_Key")'
        )
    return key, secret


# --- seed --------------------------------------------------------------------
def seed(n: int) -> List[str]:
    """Mirror ``n`` suitable Items from the source catalog into the Playground."""
    check_playground()
    return prepopulate(LOCAL_STAC, collection=COLLECTION, n=n)


# --- build -------------------------------------------------------------------
def build(item_id: str) -> None:
    """Virtualize a seeded Item's NetCDF URLs into an Icechunk store on OSN."""
    check_playground()
    item = get_item(item_id)
    urls = reachable_data_urls(item)
    if not urls:
        raise SystemExit(f"Item {item_id} has no reachable NetCDF data assets to virtualize.")
    print(f"Virtualizing {len(urls)} file(s) for {item_id}")

    key, secret = _osn_keys()
    prefix = f"{OSN_ROOT_PREFIX}/{item_id}/"
    storage = osn_storage(OSN_BUCKET, prefix, key, secret)
    if repo_exists(storage):
        print(f"  store already exists on OSN — skipping ({osn_store_href(item_id)})")
        return

    vds, registry = virtualize_from_urls(urls)
    config = ic.RepositoryConfig.default()
    for vcc in vccs_from_registry(registry):
        config.set_virtual_chunk_container(vcc)
    repo = ic.Repository.open_or_create(storage=storage, config=config)
    session = repo.writable_session("main")
    vds.vz.to_icechunk(session.store)
    snapshot = session.commit("track-c icechunk reference")
    repo.save_config()
    print(f"  ✓ Built on OSN: {osn_store_href(item_id)}  (snapshot {snapshot})")

    # --- ALSO host on AWS S3 (optional) --------------------------------------
    # Uncomment to build a second Icechunk store on AWS S3. Requires the AWS
    # default credential chain (AWS_PROFILE / SSO) and the S3_* constants above.
    # from cmip7_virtualization.storage import aws_s3_storage
    # s3_storage = aws_s3_storage(S3_BUCKET, f"{S3_PREFIX_ROOT}/{item_id}/", S3_REGION)
    # if not repo_exists(s3_storage):
    #     s3_config = ic.RepositoryConfig.default()
    #     for vcc in vccs_from_registry(registry):
    #         s3_config.set_virtual_chunk_container(vcc)
    #     s3_repo = ic.Repository.open_or_create(storage=s3_storage, config=s3_config)
    #     s3_session = s3_repo.writable_session("main")
    #     vds.vz.to_icechunk(s3_session.store)
    #     s3_session.commit("track-c icechunk reference")
    #     s3_repo.save_config()
    #     print(f"  ✓ Built on S3: s3://{S3_BUCKET}/{S3_PREFIX_ROOT}/{item_id}/")


# --- submit ------------------------------------------------------------------
def submit(
    item_id: str,
    config_path: Path,
    esgadd_bin: str,
    dry_run: bool,
    agg_url: Optional[str] = None,
) -> None:
    """Run ``esgadd --agg icechunk`` to PATCH the reference asset onto the Item.

    ``agg_url`` defaults to the public OSN URL of the dataset's Icechunk store.

    NOTE — this step is **kept deliberately as a demonstration that PATCH does not
    work against the local Playground**. esgadd builds and sends the correct
    JSON-Patch request, but ``ghcr.io/djspstfc/stac-fastapi-es:1.0`` does not
    implement item-level PATCH and returns **HTTP 405** (it supports only POST/PUT;
    see README "How it actually works"). We surface that 405 as the *expected*
    outcome rather than crashing, and point at ``post-full`` as the working
    catalog-side workaround. (Production East reportedly supports PATCH; landing
    the reference for real needs a transaction API that does — see
    ``internal/todos/todos.md``.)
    """
    if agg_url is None:
        agg_url = osn_store_href(item_id)

    cmd = [
        esgadd_bin,
        "--stac-api", LOCAL_STAC,
        "--dataset-id", item_id,
        "--agg", "icechunk",
        "--agg-url", agg_url,
        "--config", str(config_path),
        "--verbose",
    ]
    print("Running:\n  " + " ".join(cmd))
    if dry_run:
        print("(dry-run — not executing)")
        return
    check_playground()
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"'{esgadd_bin}' not found. Install ESGF/esg-publisher (esgf-ng branch) "
            "in a separate env and pass --esgadd /path/to/esgadd. See README.md."
        ) from exc
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        combined = (proc.stdout or "") + (proc.stderr or "")
        if "405" in combined:
            print(
                "\n⚠️  DEMONSTRATED: esgadd's PATCH was rejected with HTTP 405 — the "
                "local Playground image does not support item-level PATCH (POST/PUT "
                "only). esgadd itself ran correctly and sent the right request.\n"
                "   → use `post-full` to land the reference via POST/PUT, or point "
                "esgadd at a transaction API that supports PATCH (see README)."
            )
            return
        raise SystemExit(f"esgadd exited {proc.returncode}")
    print("✓ esgadd completed")


# --- post-full (esgadd-PATCH workaround) -------------------------------------
def post_full(item_id: str, *, source_stac: str = WEST_DISCOVERY) -> None:
    """Generate the *complete* Item (incl. the virtual reference) and POST it whole.

    This is a workaround for the Playground's missing PATCH support (HTTP 405 — see
    README). Instead of esgadd *patching* a reference onto an existing Item, we:

      1. (re)build the full Item from the source catalog,
      2. inject the icechunk reference asset (``reference_icechunk_osn``) ourselves
         (spec-correct: ``application/vnd.zarr+icechunk``, ``roles: [virtual, data]``
         — unlike esgadd's ``application/icechunk`` + singular ``role``),
      3. ``POST`` it to create (or ``PUT`` to replace) the Item in one shot.

    RESULT (verified 2026-06-09): this **works** — the reference lands in the
    catalog and `verify` reads it back — because POST/PUT *are* supported where
    PATCH is not.

    WHY IT'S LESS USEFUL (the tradeoff worth communicating):
      * It **bypasses the production tool** (`esgadd`) and the incremental-update
        path. The real value of esgadd is PATCHing an *existing* Item you don't own
        (a node publishes the data; we only add an aggregation later). Recreating
        the whole Item is not something an external reference-builder can do in
        production — you can't POST over someone else's published record.
      * It only demonstrates that the *representation* (an Item carrying a virtual
        reference asset) is valid, not that the *submission mechanism* works.
        Landing the reference for real still needs PATCH support on the catalog.
    """
    check_playground()

    # (re)generate the full Item from the source catalog (no reliance on a prior seed).
    matches = [i for i in fetch_source_items(source_stac, COLLECTION, n=100) if i["id"] == item_id]
    if not matches:
        raise SystemExit(f"Source catalog has no Item {item_id} with reachable NetCDF.")
    item = dict(matches[0])
    item.pop("links", None)
    item["collection"] = COLLECTION
    item.get("assets", {}).pop("reference_file", None)

    # Inject the icechunk virtual reference (store must already exist on OSN: run `build`).
    key = reference_asset_key("icechunk", "osn")  # "reference_icechunk_osn"
    item["assets"][key] = build_reference_asset(
        "icechunk", "osn", osn_store_href(item_id),
        source_node="ceda.ac.uk", region="us-east-1",
        anonymous=True, endpoint_url=OSN_ENDPOINT,
    )

    put_item(LOCAL_STAC, COLLECTION, item)
    print(f"✓ POSTed full Item with reference asset '{key}' (bypassing esgadd/PATCH)")
    back = get_item(item_id)
    print(f"  catalog assets now: {sorted(back.get('assets', {}))}")


# --- verify ------------------------------------------------------------------
def _find_icechunk_asset(item: dict, site: str) -> Optional[dict]:
    """Locate the icechunk reference asset.

    Covers both paths: esgadd's ``reference_file`` (top-level or nested
    ``alternate/<site>``) and our ``post_full`` key ``reference_icechunk_osn``
    (or any asset whose media type mentions icechunk).
    """
    assets = item.get("assets", {})
    for a in assets.values():
        if "icechunk" in (a.get("type") or ""):
            return a
    ref = assets.get("reference_file")
    if ref is None:
        return None
    return (ref.get("alternate") or {}).get(site)


def verify(item_id: str, site: str) -> None:
    """Confirm the asset is in the catalog, then open the OSN Icechunk store."""
    check_playground()
    item = get_item(item_id)
    asset = _find_icechunk_asset(item, site)
    if asset is None:
        raise SystemExit(
            f"No icechunk reference asset found on {item_id}. assets="
            f"{json.dumps(item.get('assets', {}), indent=2)}"
        )
    print("✓ Icechunk reference asset present in catalog:")
    print(json.dumps(asset, indent=2))

    # Open the OSN store directly. xpystac engine='stac' can't read anonymous-HTTP
    # virtual chunks yet, so authorize each source host anonymously (None).
    prefixes = {"/".join(u.split("/")[:3]) + "/": None for u in reachable_data_urls(item)}
    key, secret = _osn_keys()
    storage = osn_storage(OSN_BUCKET, f"{OSN_ROOT_PREFIX}/{item_id}/", key, secret)
    repo = ic.Repository.open(storage=storage, authorize_virtual_chunk_access=prefixes)
    ds = xr.open_zarr(repo.readonly_session("main").store)
    print(f"\n✓ Opened OSN store via direct Icechunk read:\n{ds}")


# --- cli ---------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--item-id", default=None, help="Act on one Item (build/submit/verify).")
    common.add_argument("--config", type=Path, default=CONFIG_DEFAULT, help="esgadd YAML config.")
    common.add_argument("--esgadd", default="esgadd", help="Path to the esgadd executable.")
    common.add_argument("--agg-url", default=None, help="Override the asset href (default: public OSN store URL).")
    common.add_argument("--site", default="ceda.ac.uk", help="data_node / alternate:name (must match config).")
    common.add_argument("--dry-run", action="store_true", help="Print the esgadd command without running it.")

    sp = sub.add_parser("seed", parents=[common]); sp.add_argument("--n", type=int, default=2)
    sub.add_parser("build", parents=[common])
    sub.add_parser("submit", parents=[common])
    sub.add_parser("post-full", parents=[common])  # esgadd/PATCH workaround (see post_full)
    sub.add_parser("verify", parents=[common])
    sa = sub.add_parser("all", parents=[common]); sa.add_argument("--n", type=int, default=2)

    a = p.parse_args(argv)

    seeded: List[str] = []
    if a.cmd in ("seed", "all"):
        seeded = seed(a.n)
    if a.cmd == "seed":
        return

    # Which Items to act on for build/submit/verify.
    targets = [a.item_id] if a.item_id else seeded
    if not targets:
        raise SystemExit("--item-id is required for build/submit/verify (or run 'all').")

    for item_id in targets:
        print(f"\n=== {item_id} ===")
        if a.cmd in ("build", "all"):
            build(item_id)
        if a.cmd in ("submit", "all"):
            # Demonstrates the PATCH 405 against the local Playground (see submit()).
            submit(item_id, a.config, a.esgadd, a.dry_run, a.agg_url)
        if a.cmd in ("post-full", "all"):
            # Land the reference for real via POST/PUT (workaround for the 405).
            post_full(item_id)
        if a.cmd in ("verify", "all"):
            verify(item_id, a.site)


if __name__ == "__main__":
    main()
