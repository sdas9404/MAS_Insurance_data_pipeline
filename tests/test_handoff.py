"""Checks for portable telemetry labels and lossless CSV handoff."""
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import pytest
import yaml

from src.processing.package_handoff import LABELS, labelled_telemetry, read_csv_typed, range_rows
from src.processing.enrich_dataset import digest
from src.simulation.run_experiment import ROOT


def telemetry(persona='baseline', times=(.5,1.)):
    return pa.table(dict(vehicle_id=['001']*len(times),persona=[persona]*len(times),time_s=list(times),speed_mps=[0.]*len(times)))


def test_explicit_labels_and_leading_zero_id():
    result=labelled_telemetry(telemetry(),'run_0001',{('run_0001','001'):'baseline'})
    assert result.column_names[:3]==LABELS
    assert result['run_id'].to_pylist()==['run_0001','run_0001']
    assert result['vehicle_id'].to_pylist()==['001','001']


def test_incorrect_persona_rejected():
    with pytest.raises(ValueError,match='persona'):
        labelled_telemetry(telemetry('cautious'),'run_0001',{('run_0001','001'):'baseline'})


def test_duplicate_time_key_rejected():
    with pytest.raises(ValueError,match='Duplicate'):
        labelled_telemetry(telemetry(times=(.5,.5)),'run_0001',{('run_0001','001'):'baseline'})


def test_csv_precision_nulls_and_text_roundtrip():
    table=pa.table(dict(vehicle_id=['001','NA',''],value=[1.2345678901234567,None,-0.12345678901234567],leader_id=[None,'','leader'],empty_column=pa.nulls(3)))
    sink=pa.BufferOutputStream()
    pacsv.write_csv(table,sink)
    result=read_csv_typed(pa.BufferReader(sink.getvalue()),table.schema)
    assert result.equals(table)


def test_persona_ranges_resolve_defaults_overrides_and_dependencies():
    cfg=yaml.safe_load((ROOT/'config/driver_profiles.yaml').read_text())
    file=ROOT/'data/experiments/balanced_12/driver_parameters.parquet'
    if not file.exists(): pytest.skip('Original experiment not available')
    rows=range_rows(cfg,pq.read_table(file).to_pylist())
    assert len(rows)==12*16
    indexed={(r['persona'],r['parameter']):r for r in rows}
    assert indexed[('cautious','speedFactor')]['configured_min']==.82
    assert indexed[('cautious','decel')]['configured_min']==4.2
    for persona in cfg['profiles']:
        assert indexed[(persona,'emergencyDecel')]['configured_min']==9.
        assert indexed[(persona,'apparentDecel')]['actual_sampled_min']==indexed[(persona,'decel')]['actual_sampled_min']
        assert indexed[(persona,'startupDelay')]['runtime_verification'].startswith('unavailable')


def test_real_handoff_inventory_labels_and_checksums():
    folder=ROOT/'PhD_SUMO_Handoff'
    if not (folder/'VALIDATION.json').exists(): pytest.skip('Handoff not built')
    validation=json.loads((folder/'VALIDATION.json').read_text())
    assert validation['passed'] and validation['all_csv_and_parquet_cells_exactly_match_sources']
    assert validation['fully_labelled_telemetry_rows']==1671909
    files=list((folder/'telemetry').glob('run_*/telemetry.parquet'))
    assert len(files)==len(list((folder/'telemetry').glob('run_*/telemetry.csv')))==12
    for path in files:
        assert pq.ParquetFile(path).schema_arrow.names[:3]==LABELS
    for relative,checksum in json.loads((folder/'SHA256SUMS.json').read_text()).items():
        assert digest(folder/relative)==checksum,relative
