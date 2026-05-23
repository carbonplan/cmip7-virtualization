# dask distributed icechunk s3fs git+https://github.com/mpiannucci/kerchunk@v3 https://github.com/zarr-developers/numcodecs@zarr3-codecs
# TMP BRANCH https://github.com/zarr-developers/VirtualiZarr/pull/278


# To do: We can generalize all these funcs and just have input filelists OR subsets of intake catalogs
# https://pangeo-data.github.io/pangeo-cmip6-cloud/accessing_data.html#manually-searching-the-catalog

# To Do:
# For now, we are just doing a time concat, but we can merge across vars later

import xarray as xr
from virtualizarr import open_virtual_dataset
import dask
from dask.distributed import Client


client = Client(n_workers=16)
client

urls = [
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_185001-186012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_187101-188012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_188101-189012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_186101-187012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_189101-190012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_190101-191012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_191101-192012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_192101-193012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_193101-194012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_194101-195012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_195101-196012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_196101-197012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_197101-198012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_198101-199012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_199101-200012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_200101-201012.nc",
    "s3://esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_201101-201412.nc",
]


vds = open_virtual_dataset(urls[0], indexes={})


def process(filename):
    vds = open_virtual_dataset(filename, indexes={})
    return vds


delayed_results = [dask.delayed(process)(filename) for filename in urls]

# compute delayed obs
results = dask.compute(*delayed_results)

# concat virtual datasets
combined_vds = xr.concat(list(results), dim="time", coords="minimal", compat="override")

# once icechunk PR is in, we can write to Zarr v3
# for now, we write to parquet, then will RT to icechunk with 'icechunk' env
ref_parquet_path = "../refs/netcdf4_s3_icechunk.parquet"
combined_vds.virtualize.to_kerchunk(ref_parquet_path, format="parquet")


###### WRITE ICECHUNK - THIS REQUIRES envs/icechunk_env.yaml #####

# 1. read existing ref
import xarray as xr
from virtualizarr import open_virtual_dataset
from icechunk import (
    IcechunkStore,
    StorageConfig,
    StoreConfig,
    VirtualRefConfig,
    S3Credentials,
)

vds = open_virtual_dataset(
    "netcdf4_s3_icechunk.parquet", filetype="kerchunk", indexes={}
)
vds = vds.set_coords([v for v in vds.variables if v != vds.attrs["variable_id"]])


storage = StorageConfig.s3_from_config(
    bucket="leap-m2lines-test",
    prefix="cmip6_virtualizarr/netcdf4_s3_icechunk",
    endpoint_url="https://nyu1.osn.mghpcc.org",
    region="dummy",
    allow_http=True,
    credentials=S3Credentials(
        access_key_id="0DJ5POIMB9T498Y4QXP6",
        secret_access_key="p47QB7sSqbykTi3pZVr7I8SOxsJ1VPCDahA6ALYT",
    ),
)


store = IcechunkStore.create(
    storage=storage,
    config=StoreConfig(
        virtual_ref_config=VirtualRefConfig.s3_anonymous(region="us-east-2"),
    ),
)


vds.virtualize.to_icechunk(store)


# Virtualizarr issue!
# issue -virtualizarr seems to demote coords
# vds = vds.set_coords([v for v in vds.variables if v != vds.attrs['variable_id']])
"""
[vds]
<xarray.Dataset> Size: 54GB
Dimensions:             (time: 1980, j: 291, i: 360, vertices: 4, bnds: 2,
                         lev: 45)
Coordinates:
    vertices_longitude  (time, j, i, vertices) float64 7GB ManifestArray<shap...
    time_bnds           (time, bnds) float64 32kB ManifestArray<shape=(1980, ...
    vertices_latitude   (time, j, i, vertices) float64 7GB ManifestArray<shap...
    longitude           (time, j, i) float64 2GB ManifestArray<shape=(1980, 2...
    latitude            (time, j, i) float64 2GB ManifestArray<shape=(1980, 2...
    lev_bnds            (time, lev, bnds) float64 1MB ManifestArray<shape=(19...
    j                   (j) int32 1kB ManifestArray<shape=(291,), dtype=int32...
    lev                 (lev) float64 360B ManifestArray<shape=(45,), dtype=f...
    time                (time) float64 16kB ManifestArray<shape=(1980,), dtyp...
    i                   (i) int32 1kB ManifestArray<shape=(360,), dtype=int32...
Dimensions without coordinates: vertices, bnds
Data variables:
    uo                  (time, lev, j, i) float32 37GB ManifestArray<shape=(1...

    
[RT icechunk ds]
    <xarray.Dataset> Size: 54GB
Dimensions:             (time: 1980, j: 291, i: 360, vertices: 4, lev: 45,
                         bnds: 2)
Coordinates:
  * lev                 (lev) float64 360B 3.047 9.454 ... 5.375e+03 5.625e+03
  * j                   (j) int32 1kB 0 1 2 3 4 5 6 ... 285 286 287 288 289 290
  * time                (time) float64 16kB 15.5 45.0 ... 6.018e+04 6.021e+04
  * i                   (i) int32 1kB 0 1 2 3 4 5 6 ... 354 355 356 357 358 359
Dimensions without coordinates: vertices, bnds
Data variables:
    vertices_latitude   (time, j, i, vertices) float64 7GB ...
    vertices_longitude  (time, j, i, vertices) float64 7GB ...
    longitude           (time, j, i) float64 2GB ...
    time_bnds           (time, bnds) float64 32kB ...
    latitude            (time, j, i) float64 2GB ...
    lev_bnds            (time, lev, bnds) float64 1MB ...
    uo                  (time, lev, j, i) float32 37GB ...
    """

store.commit("init")
ds = xr.open_zarr(store, consolidated=False)


#############################

import xarray as xr
from virtualizarr import open_virtual_dataset
from icechunk import (
    IcechunkStore,
    StorageConfig,
    StoreConfig,
    VirtualRefConfig,
    S3Credentials,
)


# storage = StorageConfig.s3_anonymous(
#     bucket='leap-m2lines-test',
#     prefix='cmip6_virtualizarr/netcdf4_s3_icechunk',
#     endpoint_url="https://nyu1.osn.mghpcc.org",
#     allow_http=True, # what exactly does this do? It does not work without this line!
#     region='some-bonkers-region',
# )

storage = StorageConfig.s3_anonymous(
    bucket="leap-m2lines-test",
    prefix="cmip6_virtualizarr/netcdf4_s3_icechunk",
    endpoint_url="https://nyu1.osn.mghpcc.org",
    allow_http=True,  # what exactly does this do? It does not work without this line!
    region="some-bonkers-region",
)
config = StoreConfig(
    virtual_ref_config=VirtualRefConfig.s3_anonymous(region="us-east-2"),
)

store = IcechunkStore.open_existing(storage, config=config, mode="r")

ds = xr.open_zarr(store, decode_cf=False, consolidated=False)
