from typing import List, NewType, cast

import pandas as pd
from sqlalchemy import Column

DataSchema = List[Column]

# Dataframe with columns (<index_cols ...>)
IndexDF = NewType('IndexDF', pd.DataFrame)

# Dataframe with columns (<index_cols ...>, hash, create_ts, update_ts, process_ts, delete_ts)
MetadataDF = NewType('MetadataDF', pd.DataFrame)

# Dataframe with columns (<index_cols ...>, <data_cols ...>)
# DataDF = NewType('DataDF', pd.DataFrame)
DataDF = pd.DataFrame


def data_to_index(data_df: DataDF, primary_keys: List[str]) -> IndexDF:
    return cast(IndexDF, data_df[primary_keys])


def meta_to_index(meta_df: MetadataDF, primary_keys: List[str]) -> IndexDF:
    return cast(IndexDF, meta_df[primary_keys])


def index_difference(idx1_df: IndexDF, idx2_df: IndexDF) -> IndexDF:
    assert(list(idx1_df.columns) == list(idx2_df.columns))
    cols = idx1_df.columns.to_list()

    idx1_idx = idx1_df.set_index(cols).index
    idx2_idx = idx2_df.set_index(cols).index

    return cast(IndexDF, idx1_idx.difference(idx2_idx).to_frame(index=False))