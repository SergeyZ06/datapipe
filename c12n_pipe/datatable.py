from typing import Callable, Generator, Iterator, List, Dict, Any, Optional, Tuple, Union, TYPE_CHECKING
from abc import ABC

import inspect
import logging
import time

from sqlalchemy import Column, String, Numeric, Float
import pandas as pd


from c12n_pipe.store.types import DataSchema, Index, ChunkMeta
from c12n_pipe.store.table_store import TableStore
from c12n_pipe.store.table_store_sql import TableStoreDB


if TYPE_CHECKING:
    from c12n_pipe.datastore import DataStore


logger = logging.getLogger('c12n_pipe.datatable')


PRIMARY_KEY = [Column('id', String(100), primary_key=True)]


METADATA_SQL_SCHEMA = [
    Column('hash', Numeric),
    Column('create_ts', Float),  # Время создания строки
    Column('update_ts', Float),  # Время последнего изменения
    Column('process_ts', Float), # Время последней успешной обработки
    Column('delete_ts', Float),  # Время удаления
]


class DataTable:
    def __init__(
        self,
        ds: 'DataStore',
        name: str,
        data_sql_schema: DataSchema,
        create_tables: bool = True,
        meta_store: TableStore = None, # Если None - создается по дефолту
        data_store: TableStore = None, # Если None - создается по дефолту
    ):
        self.ds = ds
        self.name = name

        if meta_store is None:
            self.table_meta = TableStoreDB(
                ds,
                f'{name}_meta',
                PRIMARY_KEY + METADATA_SQL_SCHEMA,
                create_tables
            )
        else:
            self.table_meta = meta_store
        
        if data_store is None:
            self.table_data = TableStoreDB(
                ds, 
                f'{name}_data', 
                PRIMARY_KEY + data_sql_schema, 
                create_tables
            )
        else:
            self.table_data = data_store

    def _make_new_metadata_df(self, now, df) -> pd.DataFrame:
        return pd.DataFrame(
            {
                'hash': pd.util.hash_pandas_object(df.apply(lambda x: str(list(x)), axis=1)),
                'create_ts': now,
                'update_ts': now,
                'process_ts': now,
                'delete_ts': None,
            },
            index=df.index
        )

    def _make_deleted_meta_df(self, now, old_meta_df, deleted_idx) -> pd.DataFrame:
        res = old_meta_df.loc[deleted_idx]
        res.loc[:, 'delete_ts'] = now
        return res

    def get_metadata(self, idx: Optional[Index] = None) -> pd.DataFrame:
        return self.table_meta.read_rows(idx)

    def get_data(self, idx: Optional[Index] = None) -> pd.DataFrame:
        return self.table_data.read_rows(idx)

    def store_chunk(self, data_df: pd.DataFrame, now: float = None) -> ChunkMeta:
        if now is None:
            now = time.time()

        logger.info(f'Inserting chunk {len(data_df)} rows into {self.name}')

        # получить meta по чанку
        existing_meta_df = self.get_metadata(data_df.index)

        # найти что изменилось
        new_meta_df = self._make_new_metadata_df(now, data_df)

        common_idx = existing_meta_df.index.intersection(new_meta_df.index)
        changed_idx = common_idx[new_meta_df.loc[common_idx, 'hash'] != existing_meta_df.loc[common_idx, 'hash']]

        # найти что добавилось
        new_idx = new_meta_df.index.difference(existing_meta_df.index)

        if len(new_idx) > 0 or len(changed_idx) > 0:
            self.ds.event_logger.log_event(
                self.name, added_count=len(new_idx), updated_count=len(changed_idx), deleted_count=0
            )

        # обновить данные (удалить только то, что изменилось, записать новое)
        to_write_idx = changed_idx.union(new_idx)

        self.table_data.update_rows(data_df.loc[to_write_idx])

        # обновить метаданные (удалить и записать всю new_meta_df, потому что изменился processed_ts)

        if len(new_meta_df) > 0:
            self.table_meta.delete_rows(new_meta_df.index)

            not_changed_idx = existing_meta_df.index.difference(changed_idx)

            new_meta_df.loc[changed_idx, 'create_ts'] = existing_meta_df.loc[changed_idx, 'create_ts']
            new_meta_df.loc[not_changed_idx, 'update_ts'] = existing_meta_df.loc[not_changed_idx, 'update_ts']

            self.table_meta.insert_rows(new_meta_df)

        return data_df.index

    def sync_meta(self, chunks: List[ChunkMeta], processed_idx: pd.Index = None) -> None:
        ''' Пометить удаленными объекты, которых больше нет '''
        idx = pd.Index([])
        for chunk in chunks:
            idx = idx.union(chunk)

        existing_meta_df = self.get_metadata(idx=processed_idx)

        deleted_idx = existing_meta_df.index.difference(idx)

        if len(deleted_idx) > 0:
            self.ds.event_logger.log_event(self.name, added_count=0, updated_count=0, deleted_count=len(deleted_idx))

            self.table_data.delete_rows(deleted_idx)
            self.table_meta.delete_rows(deleted_idx)

    def store(self, df: pd.DataFrame) -> None:
        now = time.time()

        chunk = self.store_chunk(
            data_df=df,
            now=now
        )
        self.sync_meta(
            chunks=[chunk],
        )

    def get_data_chunked(self, chunksize: int = 1000) -> Generator[pd.DataFrame, None, None]:
        meta_df = self.get_metadata(idx=None)

        for i in range(0, len(meta_df.index), chunksize):
            yield self.get_data(meta_df.index[i:i+chunksize])

    def get_indexes(self, idx: Optional[Index] = None) -> Index:
        return self.get_metadata(idx).index.tolist()


def gen_process_many(
    dts: List[DataTable],
    proc_func: Callable[..., Union[
        Tuple[pd.DataFrame, ...],
        Iterator[Tuple[pd.DataFrame, ...]]]
    ],
    **kwargs
) -> None:
    '''
    Создание новой таблицы из результатов запуска `proc_func`.
    Функция может быть как обычной, так и генерирующейся
    '''

    chunks_k: Dict[int, ChunkMeta] = {
        k: [] for k in range(len(dts))
    }

    if inspect.isgeneratorfunction(proc_func):
        iterable = proc_func(**kwargs)
    else:
        iterable = (proc_func(**kwargs),)

    for chunk_dfs in iterable:
        for k, dt_k in enumerate(dts):
            chunk_df_kth = chunk_dfs[k] if len(dts) > 1 else chunk_dfs
            chunks_k[k].append(dt_k.store_chunk(chunk_df_kth))

    for k, dt_k in enumerate(dts):
        dt_k.sync_meta(chunks=chunks_k[k])


def gen_process(
    dt: DataTable,
    proc_func: Callable[[], Union[
        pd.DataFrame,
        Iterator[pd.DataFrame]]
    ],
    **kwargs
) -> None:
    return gen_process_many(
        dts=[dt],
        proc_func=proc_func,
        **kwargs
    )


def inc_process_many(
    ds: 'DataStore',
    input_dts: List[DataTable],
    res_dts: List[DataTable],
    proc_func: Callable,
    chunksize: int = 1000,
    **kwargs
) -> None:
    '''
    Множественная инкрементальная обработка `input_dts' на основе изменяющихся индексов
    '''

    res_dts_chunks: Dict[int, ChunkMeta] = {k: [] for k,_ in enumerate(res_dts)}

    idx, input_dfs_gen = ds.get_process_chunks(inputs=input_dts, outputs=res_dts, chunksize=chunksize)

    for input_dfs in input_dfs_gen:
        if sum(len(j) for j in input_dfs) > 0:
            chunks_df = proc_func(*input_dfs, **kwargs)

            for k, res_dt in enumerate(res_dts):
                # Берем k-ое значение функции для k-ой таблички
                chunk_df_k = chunks_df[k] if len(res_dts) > 1 else chunks_df

                # Добавляем результат в результирующие чанки
                res_dts_chunks[k].append(res_dt.store_chunk(chunk_df_k))

    # Синхронизируем мета-данные для всех K табличек
    for k, res_dt in enumerate(res_dts):
        res_dt.sync_meta(res_dts_chunks[k], processed_idx=idx)


def inc_process(
    ds: 'DataStore',
    input_dts: List[DataTable],
    res_dt: DataTable,
    proc_func: Callable,
    chunksize: int = 1000,
    **kwargs
) -> None:
    inc_process_many(
        ds=ds,
        input_dts=input_dts,
        res_dts=[res_dt],
        proc_func=proc_func,
        chunksize=chunksize,
        **kwargs
    )
