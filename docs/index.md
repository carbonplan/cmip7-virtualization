# ESGF Virtual Zarr Data Access

An ESGF working group effort to enable streaming access to CMIP6 climate data via [virtual Zarr stores](https://zarr.dev), served through official ESGF channels alongside the existing NetCDF download pathway.

## Why?

Traditional workflows for ESGF data like CMIP6 requires downloading large NetCDF files before analysis. This leads to large overhead in time and cost for anyone who wants to work with the data, preventing full utilization of these unique datasets.

The work here builds upon the experience of the [Pangeo / ESGF Cloud Data Working Group](https://pangeo-data.github.io/pangeo-cmip6-cloud/) which pioneered different ways of accessing CMIP6 data in a cloud native ways. Advances in technology, particularly around virtualizing legacy file formats (which by themselves perform poorly in cloud workflows) have eliminated the need for costly and work intensive conversion workflows, while retaining high performance if the underlaying storage allows for it.

We are eager to replicate the highly liked user experience of Analyis-Ready Cloud-Optimized (ARCO) CMIP data to the latest CMIP7 data.

## Goals

- Enable the ARCO user experience (lazy load full datacubes instead of files, streaming data access instead of batch downloads) to CMIP7 data
- Access virtual reference via the [ESGF-NG](https://github.com/ESGF/esgf-roadmap/blob/main/core_architecture/design.md) infrastructure (specifically the STAC API)
- Explore higher level aggregations (e.g. combine variables) via virtual zarr stores

## Approach

We use [VirtualiZarr](https://github.com/zarr-developers/VirtualiZarr) to create lightweight reference files that point to byte ranges inside existing NetCDF files on ESGF servers. These references are stored in [Icechunk](https://github.com/earth-mover/icechunk) repositories, enabling Zarr V3 access without data duplication.

### Why not Kerchunk?

We will build virtual zarr references using icechunk due to the increased performance, and especially the ability to update existing stores without interrupting simultaneous reads.

## Milestones

- [x] First working [proof of concept](notebooks/virtualizarr_V2_example.ipynb) for virtual Zarr stores pointing to ESGF servers for CMIP6 data
- [ ] Prototype integration of reference file generation into the [ESGF publishing pipeline](https://github.com/ESGF/esg-publisher)

## References

- [ESGF Roadmap Issue #5](https://github.com/ESGF/esgf-roadmap/issues/5) — motivation for streaming access
- [Tenth ESGF Hybrid Conference](https://drive.google.com/file/d/1A43T3iz_49y5xta4ssBaacyqfiwNMNtO/view) — context for this work

```{toctree}
:hidden:
:maxdepth: 2

getting-started
contributing
```

```{toctree}
:hidden:
:maxdepth: 2
:caption: Notebooks

notebooks/virtualizarr_V2_example.ipynb
```
