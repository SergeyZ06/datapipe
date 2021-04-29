from datapipe.compute import build_compute, run_steps
from datapipe.metastore import MetaStore
from datapipe.dsl import Catalog, ExternalTable, Pipeline, BatchTransform, Table
from datapipe.store.pandas import TableStoreJsonLine

from .util import dbconn, tmp_dir


def make_file1(file):
    with open(file, 'w') as out:
        out.write('{"id": "0", "text": "text0"}\n')
        out.write('{"id": "1", "text": "text1"}\n')
        out.write('{"id": "2", "text": "text2"}\n')


def make_file2(file):
    with open(file, 'w') as out:
        out.write('{"id": "0", "text": "text0"}\n')
        out.write('{"id": "2", "text": "text2"}\n')


def test_table_store_json_line_with_deleting(dbconn, tmp_dir):
    input_file = tmp_dir / "data.json"

    ms = MetaStore(dbconn)
    catalog = Catalog({
        "input_data": ExternalTable(
            store=TableStoreJsonLine(tmp_dir / "data.json"),
        ),
        "transfomed_data": Table(
            store=TableStoreJsonLine(tmp_dir / "data_transformed.json"),
        )
    })
    pipeline = Pipeline([
        BatchTransform(
            lambda df: df,
            inputs=["input_data"],
            outputs=["transfomed_data"]
        )
    ])

    # Create data, pipeline it
    make_file1(input_file)

    steps = build_compute(ms, catalog, pipeline)
    run_steps(ms, steps)

    assert len(catalog.get_datatable(ms, 'input_data').get_data()) == 3
    assert len(catalog.get_datatable(ms, 'transfomed_data').get_data()) == 3

    # Remove {"id": "0"} from file, pipeline it
    make_file2(input_file)
    run_steps(ms, steps)

    # TODO: uncomment follow when we make files deletion
    # assert len(list(tmp_dir.glob('tbl2/*.png'))) == 2
    assert len(catalog.get_datatable(ms, 'input_data').get_data()) == 2
    assert len(catalog.get_datatable(ms, 'transfomed_data').get_data()) == 2
