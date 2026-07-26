#!/usr/bin/env python
"""End-to-end: ``esgadd --agg kerchunk`` and ``--agg icechunk`` vs a validating catalog.

Runs the **production** publisher tool against a local STAC server that mirrors
the live ESGF transaction endpoints — including the JSON-Schema validation the
stock ESGF-Playground image does not do (see
``playground/validating_stac_server.py`` for why that image cannot be used).

What it does
------------
1. Starts :mod:`playground.validating_stac_server` in a background thread, seeded
   with a real CMIP6 Item mirrored from CEDA East production.
2. Asserts the seeded Item is schema-valid to begin with, so any later error is
   attributable to the patch.
3. Runs ``esgadd --agg kerchunk``, then ``esgadd --agg icechunk`` — the
   **two-aggregation case**, which is where the ``alternate/{site}`` nesting is
   exercised.
4. Prints the exact request bodies the server received, the exact responses, and
   the final state of the Item.
5. Re-runs the whole thing in ``strict`` mode to show what a catalog that
   validated the *result* of a patch would have said.

``esgadd`` must live in a separate virtualenv (its dependencies conflict with the
virtualizarr stack). Point at it with ``--esgadd``. Without it the demo falls
back to :mod:`cmip7_virtualization.esgadd_ops`, an exact replica of esgadd's
request construction, and says so — the findings are identical either way, but
only the real binary proves the replica is faithful.

Run
---
    uv run python playground/agg_demo.py
    uv run python playground/agg_demo.py --esgadd ~/.venvs/esg-publisher/bin/esgadd
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Self

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cmip7_virtualization.esgadd_ops import add_aggregate_ops
from cmip7_virtualization.stac_validation import (
    STACValidationError,
    validate_extensions,
    validate_post,
)
from playground.validating_stac_server import (
    FIXTURES,
    build_catalog,
    create_app,
)

COLLECTION = "CMIP6"
SITE = "esgf-playground.local"

# Where the aggregations "live". The catalog only records the href — it never
# dereferences it — so the demo runs offline. Pass --kerchunk-url/--icechunk-url
# to point at stores you actually built (e.g. with playground/esgadd_playground.py
# build, which writes a real Icechunk store to OSN).
DEFAULT_KERCHUNK_URL = "https://nyu1.osn.mghpcc.org/leap-pangeo-pipeline/cmip7-virtualization/demo/kerchunk.json"
DEFAULT_ICECHUNK_URL = "https://nyu1.osn.mghpcc.org/leap-pangeo-pipeline/cmip7-virtualization/demo/icechunk/"


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def free_port() -> int:
    """Ask the OS for an unused port, so repeated runs never collide."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class BackgroundServer:
    """Run the validating server on a real port so a subprocess can reach it."""

    def __init__(self, mode: str, port: int) -> None:
        self.catalog = build_catalog(
            mode=mode, fixtures=[FIXTURES / "item_cmip6_ceda_east.json"]
        )
        config = uvicorn.Config(
            create_app(self.catalog), host="127.0.0.1", port=port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=5)


def write_config(tmp: Path) -> Path:
    """Minimal esgadd config: no auth, and `data_node` becomes `alternate:name`."""
    path = tmp / "esgadd-demo.yaml"
    path.write_text(
        "# Anything without the substring 'globus' selects the EGI client, which\n"
        "# takes its anonymous branch because we pass --stac-api on the CLI.\n"
        f"data_node: {SITE}\n"
        "stac_config:\n"
        "  stac_client:\n"
        '    redirect_uri: ""\n'
    )
    return path


def run_esgadd(
    esgadd: str | None,
    stac_api: str,
    item_id: str,
    aggtype: str,
    agg_url: str,
    config: Path,
) -> None:
    """Invoke the real ``esgadd``, or fall back to the in-process replica."""
    cmd = [
        esgadd or "esgadd",
        "--stac-api",
        stac_api,
        "--dataset-id",
        item_id,
        "--agg",
        aggtype,
        "--agg-url",
        agg_url,
        "--config",
        str(config),
        "--verbose",
    ]
    print("$ " + " ".join(cmd))

    if esgadd is None:
        print("  [replica] esgadd not provided — using cmip7_virtualization.esgadd_ops")
        current = httpx.get(
            f"{stac_api}/collections/{COLLECTION}/items/{item_id}"
        ).json()
        ops = add_aggregate_ops(current, aggtype, agg_url, SITE)
        resp = httpx.patch(
            f"{stac_api}/collections/{COLLECTION}/items/{item_id}",
            content=json.dumps(ops),
            headers={
                "Content-Type": "application/json-patch+json",
                "User-Agent": "esgf_publisher/5.4.5",
            },
        )
        print(f"  -> HTTP {resp.status_code} {resp.text[:400]}")
        return

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    for line in (proc.stdout + proc.stderr).splitlines():
        print(f"  | {line}")
    print(f"  -> exit {proc.returncode}")


def show_exchange(catalog, since: int) -> None:
    for entry in catalog.log[since:]:
        print(f"\n  {entry['method']} body (verbatim, as the server received it):")
        print("  " + json.dumps(entry["body"], indent=2).replace("\n", "\n  "))


def report_item_state(stac_api: str, item_id: str) -> None:
    item = httpx.get(f"{stac_api}/collections/{COLLECTION}/items/{item_id}").json()
    assets = item.get("assets", {})
    print(f"\n  Asset keys now on the Item: {sorted(assets)}")
    ref = assets.get("reference_file")
    if ref is not None:
        print("  assets.reference_file:")
        print("  " + json.dumps(ref, indent=2).replace("\n", "\n  "))

    print("\n  Would this Item survive re-submission (POST-strength validation)?")
    try:
        extensions = validate_extensions(COLLECTION, item.get("stac_extensions"))
        validate_post(item_id, item, extensions)
        print("    VALID")
    except STACValidationError as exc:
        print(f"    INVALID — {exc}")


def scenario(
    mode: str, port: int, esgadd: str | None, config: Path, urls: dict
) -> None:
    rule(f"MODE: {mode}")
    with BackgroundServer(mode, port) as bg:
        item_id = next(iter(bg.catalog.items[COLLECTION]))
        print(f"Server: {bg.url}   seeded Item: {item_id}")

        print("\n-- baseline: is the seeded Item schema-valid before we touch it?")
        item = httpx.get(f"{bg.url}/collections/{COLLECTION}/items/{item_id}").json()
        try:
            validate_post(
                item_id,
                item,
                validate_extensions(COLLECTION, item.get("stac_extensions")),
            )
            print("   VALID (so any error below is caused by the patch)")
        except STACValidationError as exc:
            print(f"   INVALID already: {exc}")

        for n, aggtype in enumerate(("kerchunk", "icechunk"), start=1):
            rule(f"[{mode}] aggregation #{n}: esgadd --agg {aggtype}")
            before = len(bg.catalog.log)
            run_esgadd(esgadd, bg.url, item_id, aggtype, urls[aggtype], config)
            show_exchange(bg.catalog, before)
            report_item_state(bg.url, item_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--esgadd", default=None, help="Path to a real esgadd executable."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Default: an unused port chosen by the OS.",
    )
    parser.add_argument("--kerchunk-url", default=DEFAULT_KERCHUNK_URL)
    parser.add_argument("--icechunk-url", default=DEFAULT_ICECHUNK_URL)
    parser.add_argument(
        "--mode",
        choices=("faithful", "strict", "both"),
        default="both",
        help="Which validation mode to demonstrate (default: both).",
    )
    args = parser.parse_args(argv)

    tmp = Path(__file__).resolve().parent / ".demo"
    tmp.mkdir(exist_ok=True)
    config = write_config(tmp)
    urls = {"kerchunk": args.kerchunk_url, "icechunk": args.icechunk_url}

    if args.esgadd is None:
        print(
            "NOTE: no --esgadd given; using the in-process replica of esgadd's request\n"
            "      construction (cmip7_virtualization.esgadd_ops). Install esg-publisher\n"
            "      in a separate venv and pass --esgadd to drive the real binary."
        )

    modes = ("faithful", "strict") if args.mode == "both" else (args.mode,)
    for mode in modes:
        scenario(mode, args.port or free_port(), args.esgadd, config, urls)

    rule("SUMMARY")
    print(
        "faithful mode reproduces the production Transaction API: the first PATCH is\n"
        "accepted (202) even though the asset it writes has no `protocol`, because\n"
        "validate_patch discards every error nested under the schema's top-level\n"
        "`oneOf`. The Item left in the catalog no longer validates.\n\n"
        "The second aggregation targets /assets/reference_file/alternate/<site>, which\n"
        "RFC-6902 cannot apply: esgadd never creates the `alternate` object first.\n\n"
        "strict mode validates the patched Item and rejects the first PATCH outright."
    )


if __name__ == "__main__":
    main()
