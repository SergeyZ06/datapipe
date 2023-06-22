import copy
import itertools
import logging
import math
import time
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Protocol,
    Tuple,
    cast,
)

import pandas as pd
from opentelemetry import trace
from sqlalchemy import alias, and_, column, func, literal, or_, select
from tqdm_loggable.auto import tqdm

from datapipe.compute import Catalog, ComputeStep, PipelineStep
from datapipe.datatable import DataStore, DataTable
from datapipe.executor import Executor, ExecutorConfig
from datapipe.metastore import MetaTable, TransformMetaTable
from datapipe.run_config import LabelDict, RunConfig
from datapipe.store.database import sql_apply_runconfig_filter
from datapipe.types import (
    ChangeList,
    DataDF,
    IndexDF,
    Labels,
    MetaSchema,
    TransformResult,
    data_to_index,
)

logger = logging.getLogger("datapipe.core_steps")
tracer = trace.get_tracer("datapipe.core_steps")


class DatatableTransformFunc(Protocol):
    __name__: str

    def __call__(
        self,
        ds: DataStore,
        input_dts: List[DataTable],
        output_dts: List[DataTable],
        run_config: Optional[RunConfig],
        # Возможно, лучше передавать как переменную, а не  **
        **kwargs,
    ) -> None:
        ...


# TODO подумать, может быть мы хотим дать возможность возвращать итератор TransformResult
class DatatableBatchTransformFunc(Protocol):
    __name__: str

    def __call__(
        self,
        ds: DataStore,
        idx: IndexDF,
        input_dts: List[DataTable],
        run_config: Optional[RunConfig] = None,
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> TransformResult:
        ...


# TODO подумать, может быть мы хотим дать возможность возвращать итератор TransformResult
BatchTransformFunc = Callable[..., TransformResult]

BatchGenerateFunc = Callable[..., Iterator[TransformResult]]


@dataclass
class DatatableTransform(PipelineStep):
    func: DatatableTransformFunc
    inputs: List[str]
    outputs: List[str]
    check_for_changes: bool = True
    kwargs: Optional[Dict[str, Any]] = None
    labels: Optional[Labels] = None

    def build_compute(self, ds: DataStore, catalog: Catalog) -> List["ComputeStep"]:
        return [
            DatatableTransformStep(
                name=self.func.__name__,
                input_dts=[catalog.get_datatable(ds, i) for i in self.inputs],
                output_dts=[catalog.get_datatable(ds, i) for i in self.outputs],
                func=self.func,
                kwargs=self.kwargs,
                check_for_changes=self.check_for_changes,
                labels=self.labels,
            )
        ]


class DatatableTransformStep(ComputeStep):
    def __init__(
        self,
        name: str,
        input_dts: List[DataTable],
        output_dts: List[DataTable],
        func: DatatableTransformFunc,
        kwargs: Optional[Dict] = None,
        check_for_changes: bool = True,
        labels: Optional[Labels] = None,
    ) -> None:
        ComputeStep.__init__(self, name, input_dts, output_dts, labels)

        self.func = func
        self.kwargs = kwargs or {}
        self.check_for_changes = check_for_changes

    def run_full(
        self,
        ds: DataStore,
        run_config: Optional[RunConfig] = None,
        executor: Optional[Executor] = None,
    ) -> None:
        logger.info(f"Running: {self.name}")

        # TODO implement "watermark" system for tracking computation status in DatatableTransform
        #
        # if len(self.input_dts) > 0 and self.check_for_changes:
        #     with tracer.start_as_current_span("check for changes"):
        #         changed_idx_count = ds.get_changed_idx_count(
        #             inputs=self.input_dts,
        #             outputs=self.output_dts,
        #             run_config=run_config,
        #         )

        #         if changed_idx_count == 0:
        #             logger.debug(
        #                 f"Skipping {self.get_name()} execution - nothing to compute"
        #             )

        #             return

        run_config = RunConfig.add_labels(run_config, {"step_name": self.get_name()})

        with tracer.start_as_current_span(f"Run {self.func}"):
            try:
                self.func(
                    ds=ds,
                    input_dts=self.input_dts,
                    output_dts=self.output_dts,
                    run_config=run_config,
                    kwargs=self.kwargs,
                )
            except Exception as e:
                logger.error(f"Datatable transform ({self.func}) run failed: {str(e)}")
                ds.event_logger.log_exception(e, run_config=run_config)


def safe_func_name(func: Callable) -> str:
    raw_name = func.__name__
    if raw_name == "<lambda>":
        return "lambda"
    return raw_name


class BaseBatchTransformStep(ComputeStep):
    """
    Abstract class for batch transform steps
    """

    def __init__(
        self,
        ds: DataStore,
        name: str,
        input_dts: List[DataTable],
        output_dts: List[DataTable],
        transform_keys: Optional[List[str]] = None,
        chunk_size: int = 1000,
        labels: Optional[Labels] = None,
        executor_config: Optional[ExecutorConfig] = None,
        filters: Optional[LabelDict] = None
    ) -> None:
        ComputeStep.__init__(
            self,
            name=name,
            input_dts=input_dts,
            output_dts=output_dts,
            labels=labels,
            executor_config=executor_config,
        )

        self.chunk_size = chunk_size

        self.transform_keys, self.transform_schema = self.compute_transform_schema(
            [i.meta_table for i in input_dts],
            [i.meta_table for i in output_dts],
            transform_keys,
        )

        self.meta_table = TransformMetaTable(
            dbconn=ds.meta_dbconn,
            name=f"{self.get_name()}_meta",
            primary_schema=self.transform_schema,
            create_table=ds.create_meta_table,
        )
        self.filters = filters

    @classmethod
    def compute_transform_schema(
        cls,
        input_mts: List[MetaTable],
        output_mts: List[MetaTable],
        transform_keys: Optional[List[str]],
    ) -> Tuple[List[str], MetaSchema]:
        # Hacky way to collect all the primary keys into a single set. Possible
        # problem that is not handled here is that theres a possibility that the
        # same key is defined differently in different input tables.
        all_keys = {
            col.name: col
            for col in itertools.chain(
                *(
                    [dt.primary_schema for dt in input_mts]
                    + [dt.primary_schema for dt in output_mts]
                )
            )
        }

        if transform_keys is not None:
            return (transform_keys, [all_keys[k] for k in transform_keys])

        assert len(input_mts) > 0

        inp_p_keys = set.intersection(*[set(inp.primary_keys) for inp in input_mts])
        assert len(inp_p_keys) > 0

        if len(output_mts) == 0:
            return (list(inp_p_keys), [all_keys[k] for k in inp_p_keys])

        out_p_keys = set.intersection(*[set(out.primary_keys) for out in output_mts])
        assert len(out_p_keys) > 0

        inp_out_p_keys = set.intersection(inp_p_keys, out_p_keys)
        assert len(inp_out_p_keys) > 0

        return (list(inp_out_p_keys), [all_keys[k] for k in inp_out_p_keys])

    def _build_changed_idx_sql(
        self,
        ds: DataStore,
        run_config: Optional[RunConfig] = None,
    ) -> Tuple[Iterable[str], select]:
        if len(self.transform_keys) == 0:
            raise NotImplementedError()

        all_input_keys_counts: Dict[str, int] = {}
        for col in itertools.chain(*[dt.primary_schema for dt in self.input_dts]):
            all_input_keys_counts[col.name] = all_input_keys_counts.get(col.name, 0) + 1

        # Check that all keys are either in one input table or in all input tables
        # Currently we do not support partial primary keys
        output_insersection = set.intersection(*map(set, [
            [col.name for col in dt.primary_schema] for dt in self.output_dts
        ]))
        assert all(
            v == 1 or v == len(self.input_dts) for k, v in all_input_keys_counts.items()
        )

        common_keys = [
            k for k, v in all_input_keys_counts.items() if v == len(self.input_dts)
        ]

        common_transform_keys = [k for k in self.transform_keys if k in common_keys]

        # TODO move to DBConn compatiblity layer
        if ds.meta_dbconn.con.driver in ("sqlite", "pysqlite"):
            greatest_func = func.max
        else:
            greatest_func = func.greatest

        def _make_agg_cte(
            dt: DataTable, agg_fun, agg_col: str
        ) -> Tuple[List[str], Any]:
            tbl = dt.meta_table.sql_table

            keys = [k for k in self.transform_keys if k in dt.primary_keys]
            key_cols = [column(k) for k in keys]

            sql = (
                select(*key_cols + [agg_fun(tbl.c[agg_col]).label(agg_col)])
                .select_from(tbl)
                .group_by(*key_cols)
            )

            sql = sql_apply_runconfig_filter(sql, tbl, dt.primary_keys, run_config)

            return (keys, sql.cte(name=f"{tbl.name}__{agg_col}"))

        def _make_agg_of_agg(ctes, agg_col):
            assert len(ctes) > 0

            if len(ctes) == 1:
                return ctes[0][1]

            coalesce_keys = []

            for key in self.transform_keys:
                ctes_with_key = [subq for (subq_keys, subq) in ctes if key in subq_keys]

                if len(ctes_with_key) == 0:
                    raise ValueError(f"Key {key} not found in any of the input tables")

                if len(ctes_with_key) == 1:
                    coalesce_keys.append(ctes_with_key[0].c[key])
                else:
                    coalesce_keys.append(
                        func.coalesce(*[cte.c[key] for cte in ctes_with_key]).label(key)
                    )

            agg = greatest_func(*[subq.c[agg_col] for (subq_keys, subq) in ctes]).label(
                agg_col
            )

            _, first_cte = ctes[0]

            sql = select(*coalesce_keys + [agg]).select_from(first_cte)

            for _, cte in ctes[1:]:
                if len(common_transform_keys) > 0:
                    sql = sql.outerjoin(
                        cte,
                        onclause=and_(
                            *[
                                first_cte.c[key] == cte.c[key]
                                for key in common_transform_keys
                            ]
                        ),
                        full=True,
                    )
                else:
                    sql = sql.outerjoin(
                        cte,
                        onclause=literal(True),
                        full=True,
                    )

            return sql.cte(name=f"all__{agg_col}")

        inp_ctes = [_make_agg_cte(tbl, func.max, "update_ts") for tbl in self.input_dts]

        inp = _make_agg_of_agg(inp_ctes, "update_ts")

        tr_tbl = self.meta_table.sql_table
        out = (
            select(
                *[column(k) for k in self.transform_keys]
                + [tr_tbl.c.process_ts, tr_tbl.c.priority, tr_tbl.c.is_success]
            )
            .select_from(tr_tbl)
            .group_by(*[column(k) for k in self.transform_keys])
            .cte(name="transform")
        )

        sql = (
            select(
                *[
                    func.coalesce(inp.c[key], out.c[key]).label(key)
                    for key in self.transform_keys
                ]
            )
            .select_from(inp)
            .outerjoin(
                out,
                onclause=and_(
                    *[inp.c[key] == out.c[key] for key in self.transform_keys]
                ),
                full=True,
            )
            .where(
                or_(
                    and_(
                        out.c.is_success == True,  # noqa
                        inp.c.update_ts > out.c.process_ts,
                    ),
                    out.c.is_success != True,  # noqa
                    out.c.process_ts == None,  # noqa
                )
            )
            .order_by(
                out.c.priority.desc().nullslast(),
                *[column(k) for k in self.transform_keys],
            )
        )
        return (self.transform_keys, sql)

    def _apply_filters_to_run_config(self, run_config: Optional[RunConfig] = None) -> Optional[RunConfig]:
        if self.filters is None:
            return run_config
        else:
            if run_config is None:
                return RunConfig(filters=self.filters)
            else:
                run_config = copy.deepcopy(run_config)
                filters = copy.deepcopy(self.filters)
                filters.update(run_config.filters)
                run_config.filters = filters
                return run_config

    def get_changed_idx_count(
        self,
        ds: DataStore,
        run_config: Optional[RunConfig] = None,
    ) -> int:
        run_config = self._apply_filters_to_run_config(run_config)
        _, sql = self._build_changed_idx_sql(ds, run_config=run_config)

        with ds.meta_dbconn.con.begin() as con:
            idx_count = con.execute(
                select([func.count()]).select_from(
                    alias(sql.subquery(), name="union_select")
                )
            ).scalar()

        return idx_count

    def get_full_process_ids(
        self,
        ds: DataStore,
        chunk_size: Optional[int] = None,
        run_config: Optional[RunConfig] = None,
    ) -> Tuple[int, Iterable[IndexDF]]:
        """
        Метод для получения перечня индексов для обработки.

        Returns: (idx_size, iterator<idx_df>)

        - idx_size - количество индексов требующих обработки
        - idx_df - датафрейм без колонок с данными, только индексная колонка
        """
        run_config = self._apply_filters_to_run_config(run_config)
        chunk_size = chunk_size or self.chunk_size

        with tracer.start_as_current_span("compute ids to process"):
            if len(self.input_dts) == 0:
                return (0, iter([]))

            idx_count = self.get_changed_idx_count(
                ds=ds,
                run_config=run_config,
            )

            join_keys, u1 = self._build_changed_idx_sql(
                ds=ds,
                run_config=run_config,
            )

            # Список ключей из фильтров, которые нужно добавить в результат
            extra_filters: LabelDict
            if run_config is not None:
                extra_filters = {
                    k: v for k, v in run_config.filters.items() if k not in join_keys
                }
            else:
                extra_filters = {}

            def alter_res_df():
                with ds.meta_dbconn.con.begin() as con:
                    for df in pd.read_sql_query(u1, con=con, chunksize=chunk_size):
                        for k, v in extra_filters.items():
                            df[k] = v

                        yield df

            return math.ceil(idx_count / chunk_size), alter_res_df()

    def get_change_list_process_ids(
        self,
        ds: DataStore,
        change_list: ChangeList,
        run_config: Optional[RunConfig] = None,
    ) -> Tuple[int, Iterable[IndexDF]]:
        run_config = self._apply_filters_to_run_config(run_config)
        with tracer.start_as_current_span("compute ids to process"):
            changes = [pd.DataFrame(columns=self.transform_keys)]

            for inp in self.input_dts:
                if inp.name in change_list.changes:
                    idx = change_list.changes[inp.name]

                    changes.append(data_to_index(idx, self.transform_keys))

            idx_df = pd.concat(changes).drop_duplicates(subset=self.transform_keys)
            idx = IndexDF(idx_df[self.transform_keys])

            def gen():
                for i in range(math.ceil(len(idx) / self.chunk_size)):
                    yield idx[i * self.chunk_size : (i + 1) * self.chunk_size]

            return len(idx), gen()

    def store_batch_result(
        self,
        ds: DataStore,
        idx: IndexDF,
        output_dfs: Optional[TransformResult],
        process_ts: float,
        run_config: Optional[RunConfig] = None,
    ) -> ChangeList:
        run_config = self._apply_filters_to_run_config(run_config)
        res = super().store_batch_result(ds, idx, output_dfs, process_ts, run_config)

        self.meta_table.mark_rows_processed_success(
            idx, process_ts=process_ts, run_config=run_config
        )

        return res

    def store_batch_err(
        self,
        ds: DataStore,
        idx: IndexDF,
        e: Exception,
        process_ts: float,
        run_config: Optional[RunConfig] = None,
    ) -> None:
        run_config = self._apply_filters_to_run_config(run_config)
        super().store_batch_err(ds, idx, e, process_ts, run_config)

        self.meta_table.mark_rows_processed_error(
            idx,
            process_ts=process_ts,
            error=str(e),
            run_config=run_config,
        )

    def fill_metadata(self, ds: DataStore) -> None:
        idx_len, idx_gen = self.get_full_process_ids(ds=ds, chunk_size=1000)

        for idx in tqdm(idx_gen, total=idx_len):
            self.meta_table.insert_rows(idx)

    def reset_metadata(self, ds: DataStore) -> None:
        self.meta_table.mark_all_rows_unprocessed()


@dataclass
class DatatableBatchTransform(PipelineStep):
    func: DatatableBatchTransformFunc
    inputs: List[str]
    outputs: List[str]
    chunk_size: int = 1000
    kwargs: Optional[Dict] = None
    labels: Optional[Labels] = None

    def build_compute(self, ds: DataStore, catalog: Catalog) -> List[ComputeStep]:
        input_dts = [catalog.get_datatable(ds, name) for name in self.inputs]
        output_dts = [catalog.get_datatable(ds, name) for name in self.outputs]

        return [
            DatatableBatchTransformStep(
                ds=ds,
                name=f"{self.func.__name__}",
                func=self.func,
                input_dts=input_dts,
                output_dts=output_dts,
                kwargs=self.kwargs,
                chunk_size=self.chunk_size,
                labels=self.labels,
            )
        ]


class DatatableBatchTransformStep(BaseBatchTransformStep):
    def __init__(
        self,
        ds: DataStore,
        name: str,
        func: DatatableBatchTransformFunc,
        input_dts: List[DataTable],
        output_dts: List[DataTable],
        kwargs: Optional[Dict] = None,
        transform_keys: Optional[List[str]] = None,
        chunk_size: int = 1000,
        labels: Optional[Labels] = None,
    ) -> None:
        super().__init__(
            ds=ds,
            name=name,
            input_dts=input_dts,
            output_dts=output_dts,
            transform_keys=transform_keys,
            chunk_size=chunk_size,
            labels=labels,
        )

        self.func = func
        self.kwargs = kwargs

    def process_batch_dts(
        self,
        ds: DataStore,
        idx: IndexDF,
        run_config: Optional[RunConfig] = None,
    ) -> Optional[TransformResult]:
        return self.func(
            ds=ds,
            idx=idx,
            input_dts=self.input_dts,
            run_config=run_config,
            kwargs=self.kwargs,
        )


@dataclass
class BatchTransform(PipelineStep):
    func: BatchTransformFunc
    inputs: List[str]
    outputs: List[str]
    chunk_size: int = 1000
    kwargs: Optional[Dict[str, Any]] = None
    transform_keys: Optional[List[str]] = None
    labels: Optional[Labels] = None
    executor_config: Optional[ExecutorConfig] = None
    filters: Optional[LabelDict] = None

    def build_compute(self, ds: DataStore, catalog: Catalog) -> List[ComputeStep]:
        input_dts = [catalog.get_datatable(ds, name) for name in self.inputs]
        output_dts = [catalog.get_datatable(ds, name) for name in self.outputs]

        return [
            BatchTransformStep(
                ds=ds,
                name=f"{self.func.__name__}",  # type: ignore # mypy bug: https://github.com/python/mypy/issues/10976
                input_dts=input_dts,
                output_dts=output_dts,
                func=self.func,
                kwargs=self.kwargs,
                transform_keys=self.transform_keys,
                chunk_size=self.chunk_size,
                labels=self.labels,
                executor_config=self.executor_config,
                filters=self.filters
            )
        ]


class BatchTransformStep(BaseBatchTransformStep):
    def __init__(
        self,
        ds: DataStore,
        name: str,
        func: BatchTransformFunc,
        input_dts: List[DataTable],
        output_dts: List[DataTable],
        kwargs: Optional[Dict[str, Any]] = None,
        transform_keys: Optional[List[str]] = None,
        chunk_size: int = 1000,
        labels: Optional[Labels] = None,
        executor_config: Optional[ExecutorConfig] = None,
        filters: Optional[LabelDict] = None
    ) -> None:
        super().__init__(
            ds=ds,
            name=name,
            input_dts=input_dts,
            output_dts=output_dts,
            transform_keys=transform_keys,
            chunk_size=chunk_size,
            labels=labels,
            executor_config=executor_config,
            filters=filters
        )

        self.func = func
        self.kwargs = kwargs

    def process_batch_dfs(
        self,
        ds: DataStore,
        idx: IndexDF,
        input_dfs: List[DataDF],
        run_config: Optional[RunConfig] = None,
    ) -> TransformResult:
        return self.func(*input_dfs, **self.kwargs or {})


def do_batch_generate(
    func: BatchGenerateFunc,
    ds: DataStore,
    output_dts: List[DataTable],
    run_config: Optional[RunConfig] = None,
    kwargs: Optional[Dict] = None,
) -> None:
    import inspect

    import pandas as pd

    """
    Создание новой таблицы из результатов запуска `proc_func`.
    Функция может быть как обычной, так и генерирующейся
    """

    now = time.time()
    empty_generator = True

    assert inspect.isgeneratorfunction(
        func
    ), "Starting v0.8.0 proc_func should be a generator"

    with tracer.start_as_current_span("init generator"):
        try:
            iterable = func(**kwargs or {})
        except Exception as e:
            # mypy bug: https://github.com/python/mypy/issues/10976
            logger.exception(f"Generating failed ({func.__name__}): {str(e)}")  # type: ignore
            ds.event_logger.log_exception(e, run_config=run_config)

            raise e

    while True:
        with tracer.start_as_current_span("get next batch"):
            try:
                chunk_dfs = next(iterable)

                if isinstance(chunk_dfs, pd.DataFrame):
                    chunk_dfs = (chunk_dfs,)
            except StopIteration:
                break
            except Exception as e:
                logger.exception(f"Generating failed ({func}): {str(e)}")
                ds.event_logger.log_exception(e, run_config=run_config)

                # raise e
                return

        empty_generator = False

        with tracer.start_as_current_span("store results"):
            for k, dt_k in enumerate(output_dts):
                dt_k.store_chunk(chunk_dfs[k], run_config=run_config)

    with tracer.start_as_current_span("delete stale rows"):
        for k, dt_k in enumerate(output_dts):
            dt_k.delete_stale_by_process_ts(now, run_config=run_config)


@dataclass
class BatchGenerate(PipelineStep):
    func: BatchGenerateFunc
    outputs: List[str]
    kwargs: Optional[Dict] = None
    labels: Optional[Labels] = None

    def build_compute(self, ds: DataStore, catalog: Catalog) -> List[ComputeStep]:
        return [
            DatatableTransformStep(
                name=self.func.__name__,
                func=cast(
                    DatatableTransformFunc,
                    lambda ds, input_dts, output_dts, run_config, kwargs: do_batch_generate(
                        func=self.func,
                        ds=ds,
                        output_dts=output_dts,
                        run_config=run_config,
                        kwargs=kwargs,
                    ),
                ),
                input_dts=[],
                output_dts=[catalog.get_datatable(ds, name) for name in self.outputs],
                check_for_changes=False,
                kwargs=self.kwargs,
                labels=self.labels,
            )
        ]


def update_external_table(
    ds: DataStore, table: DataTable, run_config: Optional[RunConfig] = None
) -> None:
    now = time.time()

    for ps_df in tqdm(
        table.table_store.read_rows_meta_pseudo_df(run_config=run_config)
    ):
        (
            new_df,
            changed_df,
            new_meta_df,
            changed_meta_df,
        ) = table.meta_table.get_changes_for_store_chunk(ps_df, now=now)

        # TODO switch to iterative store_chunk and table.sync_meta_by_process_ts

        table.meta_table.insert_meta_for_store_chunk(new_meta_df)
        table.meta_table.update_meta_for_store_chunk(changed_meta_df)

    for stale_idx in table.meta_table.get_stale_idx(now, run_config=run_config):
        logger.debug(f"Deleting {len(stale_idx.index)} rows from {table.name} data")
        table.event_logger.log_state(
            table.name,
            added_count=0,
            updated_count=0,
            deleted_count=len(stale_idx),
            processed_count=len(stale_idx),
            run_config=run_config,
        )

        table.meta_table.mark_rows_deleted(stale_idx, now=now)


class UpdateExternalTable(PipelineStep):
    def __init__(
        self,
        output: str,
        labels: Optional[Labels] = None,
    ) -> None:
        self.output_table_name = output
        self.labels = labels

    def build_compute(self, ds: DataStore, catalog: Catalog) -> List[ComputeStep]:
        def transform_func(
            ds: DataStore,
            input_dts: List[DataTable],
            output_dts: List[DataTable],
            run_config: Optional[RunConfig],
            **kwargs,
        ):
            return update_external_table(ds, output_dts[0], run_config)

        return [
            DatatableTransformStep(
                name=f"update_{self.output_table_name}",
                func=cast(DatatableTransformFunc, transform_func),
                input_dts=[],
                output_dts=[catalog.get_datatable(ds, self.output_table_name)],
                labels=self.labels,
            )
        ]
