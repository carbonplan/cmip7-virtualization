#!/usr/bin/env python
# coding: utf-8

# # Proof of concept: Virtualizing CMIP6 netcdf files

# In[1]:


# # install virtualizarr
# !pip install git+https://github.com/jbusecke/VirtualiZarr.git@esgf-cmip-test


# In[ ]:


from tqdm.auto import tqdm
from virtualizarr import open_virtual_dataset
from virtualizarr.kerchunk import FileType


# In[5]:


# data is located on public s3 (more info: https://pangeo-data.github.io/pangeo-cmip6-cloud/overview.html#netcdf-data-overview)
paths = [
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_185001-186012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_187101-188012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_188101-189012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_186101-187012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_189101-190012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_190101-191012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_191101-192012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_192101-193012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_193101-194012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_194101-195012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_195101-196012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_196101-197012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_197101-198012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_198101-199012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_199101-200012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_200101-201012.nc",
    "esgf-world/CMIP6/CMIP/CCCma/CanESM5/historical/r10i1p1f1/Omon/uo/gn/v20190429/uo_Omon_CanESM5_historical_r10i1p1f1_gn_201101-201412.nc",
]


# In[ ]:


# load virtual datasets in serial
vds_list = []
for f in tqdm(files):
    vds = open_virtual_dataset(f, filetype=FileType.netcdf4, indexes={})
    vds_list.append(vds)


# In[ ]:


combined_vds = xr.combine_nested(
    vds_list, concat_dim=["time"], coords="minimal", compat="override"
)


# In[ ]:


combined_vds.virtualize.to_kerchunk("combined_full.json", format="json")


# ## Read from local json
# If you executed all steps above, you should be able to execute this cell.

# In[ ]:


dsv_local = xr.open_dataset(
    "reference://",
    engine="zarr",
    chunks={},
    backend_kwargs={
        "consolidated": False,
        "storage_options": {
            "fo": "combined_full.json",
            "remote_protocol": "s3",
            "remote_options": {"anon": True},
        },
    },
)
dsv_local


# ## Read from Json on public cloud storage
# I moved the resulting json to a public bucket for testing

# In[ ]:


dsv_bucket = xr.open_dataset(
    "reference://",
    engine="zarr",
    chunks={"time": 5, "lev": 45, "j": 291, "i": 360},  # NOTE: I changed these!
    backend_kwargs={
        "consolidated": False,
        "storage_options": {
            "target_protocol": "gs",
            "fo": "gs://cmip6/testing-virtualizarr/proof-of-concept.json",
            "remote_protocol": "s3",
            "remote_options": {"anon": True},
        },
    },
)
dsv_bucket
