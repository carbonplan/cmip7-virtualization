"""Icechunk storage + virtual-chunk-container helpers.

Two hosting backends for the Icechunk *store* itself:
  - ``osn_storage``     — OSN/Ceph (S3-compatible, custom endpoint, static keys)
  - ``aws_s3_storage``  — real AWS S3 (region only; creds from the AWS default
                          chain, so ``AWS_PROFILE`` / SSO works)

Plus helpers to build virtual-chunk containers and read-side authorization from a
virtualizarr ``ObjectStoreRegistry``, covering both anonymous HTTP sources (CEDA
Thredds / dap.ceda.ac.uk) and anonymous S3 sources (esgf-world, ``us-east-2``).
"""

from typing import Dict, Iterable, List

import icechunk as ic
from virtualizarr.registry import ObjectStoreRegistry

# esgf-world (the public West/DOE CMIP6 S3 mirror) lives in us-east-2.
ESGF_WORLD_REGION = "us-east-2"


def osn_storage(bucket, prefix, access_key_id, secret_access_key) -> ic.Storage:
    """Icechunk storage on OSN/Ceph (S3-compatible) with static keys."""
    return ic.s3_storage(
        bucket=bucket,
        prefix=prefix,
        endpoint_url="https://nyu1.osn.mghpcc.org",
        region="us-east-1",  # required even for non-AWS
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        force_path_style=True,  # needed for Ceph
    )


def aws_s3_storage(bucket, prefix, region, *, anonymous: bool = False) -> ic.Storage:
    """Icechunk storage on real AWS S3.

    Credentials come from the AWS default chain (``from_env=True``): environment
    variables, a named profile via ``AWS_PROFILE``, or an SSO session. Pass
    ``anonymous=True`` for public-read buckets.
    """
    return ic.s3_storage(
        bucket=bucket,
        prefix=prefix,
        region=region,
        anonymous=True if anonymous else None,
        from_env=None if anonymous else True,
    )


def _prefixes(registry: ObjectStoreRegistry) -> List[str]:
    return [f"{k.scheme}://{k.netloc}/" for k in registry.map.keys()]


def open_http_repository(
    url: str,
    *,
    virtual_prefixes: Iterable[str] = (),
) -> ic.Repository:
    """Open a read-only Icechunk repository served as static files over HTTPS.

    This is how a data centre can publish an Icechunk store from an ordinary web
    server with no object-store API in front of it — CEDA serves one from a JASMIN
    group workspace this way. Requires icechunk >= 2.0; 1.x has no HTTP repository
    backend, so the only way in was to mirror the store and open it from disk.

    ``virtual_prefixes`` are the hosts the store's chunk manifests point *at* (for
    a CEDA-built store, ``https://dap.ceda.ac.uk/``). Each gets an anonymous HTTP
    virtual-chunk container and matching read authorization; without them the
    metadata opens fine but every chunk read fails.

    The repository is read-only: HTTP storage cannot commit.
    """
    config = ic.RepositoryConfig.default()
    for prefix in virtual_prefixes:
        config.set_virtual_chunk_container(
            ic.VirtualChunkContainer(url_prefix=prefix, store=ic.http_store())
        )
    return ic.Repository.open(
        storage=ic.http_storage(url),
        config=config,
        authorize_virtual_chunk_access={
            p: ic.credentials.HttpAccess for p in virtual_prefixes
        },
    )


def vccs_from_registry(
    registry: ObjectStoreRegistry,
    *,
    s3_region: str = ESGF_WORLD_REGION,
    s3_anonymous: bool = True,
) -> List[ic.VirtualChunkContainer]:
    """Build a VirtualChunkContainer per source host in the registry.

    ``http(s)://`` hosts get an ``http_store`` (anonymous); ``s3://`` hosts get an
    ``s3_store`` (anonymous + ``s3_region`` by default, suited to esgf-world).
    """
    vccs = []
    for url_prefix in _prefixes(registry):
        if url_prefix.startswith("s3://"):
            store = ic.s3_store(region=s3_region, anonymous=s3_anonymous)
        else:
            store = ic.http_store()
        vccs.append(ic.VirtualChunkContainer(url_prefix=url_prefix, store=store))
    return vccs


def http_vccs_from_registry(registry: ObjectStoreRegistry) -> List[ic.VirtualChunkContainer]:
    """Back-compat wrapper — HTTP-only virtual-chunk containers.

    Prefer :func:`vccs_from_registry`, which also handles S3 sources.
    """
    return [
        ic.VirtualChunkContainer(url_prefix=p, store=ic.http_store())
        for p in _prefixes(registry)
    ]


def authorize_prefixes_from_registry(
    registry: ObjectStoreRegistry,
) -> Dict[str, ic.AnyCredential]:
    """Read-side ``authorize_virtual_chunk_access`` map for ``Repository.open``.

    Anonymous HTTP hosts map to ``ic.credentials.HttpAccess``; anonymous S3 hosts
    (esgf-world) map to ``ic.s3_anonymous_credentials()``.

    Icechunk 2 replaced the bare ``None`` sentinel for no-auth containers with
    explicit ones (``HttpAccess``, ``LocalFileSystemAccess``) — ``None`` still
    works but warns, because it could not distinguish "needs no credentials" from
    "credentials omitted by mistake". See earth-mover/icechunk#2194.
    """
    auth: Dict[str, ic.AnyCredential] = {}
    for url_prefix in _prefixes(registry):
        if url_prefix.startswith("s3://"):
            auth[url_prefix] = ic.s3_anonymous_credentials()
        else:
            auth[url_prefix] = ic.credentials.HttpAccess
    return auth
