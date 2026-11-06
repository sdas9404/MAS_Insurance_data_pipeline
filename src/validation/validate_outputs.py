"""Read-back validation of completed experiment artifacts; no simulation required."""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
import math
import pyarrow.parquet as pq


def validate(folder):
    folder=Path(folder)
    params=pq.read_table(folder/'driver_parameters.parquet').to_pylist()
    trips=pq.read_table(folder/'trip_summary.parquet').to_pylist()
    runs=pq.read_table(folder/'simulation_runs.parquet').to_pylist()
    expected={(p['run_id'],p['vehicle_id']):p for p in params}
    assert len(expected)==len(params)==300*len(runs)
    assert {(t['run_id'],t['vehicle_id']) for t in trips}==set(expected)
    for run in runs:
        group=[p for p in params if p['run_id']==run['run_id']]
        assert set(Counter(p['persona'] for p in group).values())=={25}
        assert sum(t['completed'] for t in trips if t['run_id']==run['run_id'])==run['arrived_count']
    if len(runs)==12:
        appearances=defaultdict(set)
        for p in params: appearances[p['vehicle_id']].add(p['persona'])
        assert len(appearances)==300 and all(len(v)==12 for v in appearances.values())
    paths=list((folder/'telemetry').rglob('*.parquet'))
    assert len(paths)==len(runs) and len(set(p.resolve() for p in paths))==len(paths)
    count=0; validated_jerks=0; observations=0
    for path in paths:
        seen=set(); previous={}
        for batch in pq.ParquetFile(path).iter_batches(batch_size=10000):
            for row in batch.to_pylist():
                row.setdefault('run_id',path.parent.name.split('=',1)[1])
                key=(row['run_id'],row['vehicle_id'],row['time_s'])
                assert key not in seen; seen.add(key)
                assert (row['run_id'],row['vehicle_id']) in expected
                assert expected[(row['run_id'],row['vehicle_id'])]['persona']==row['persona']
                prev=previous.get(row['vehicle_id'])
                if row['jerk_mps3'] is not None:
                    assert prev is not None
                    assert math.isclose(row['jerk_mps3'],(row['accel_mps2']-prev['accel_mps2'])/(row['time_s']-prev['time_s']),abs_tol=1e-8)
                    validated_jerks+=1
                previous[row['vehicle_id']]=row
                count+=1
    for path in (folder/'raw').glob('*/observed_parameters.parquet'):
        for row in pq.read_table(path).to_pylist():
            expected_row=expected[(row['run_id'],row['vehicle_id'])]
            for key,value in row.items():
                if key not in ['run_id','vehicle_id'] and value is not None: assert math.isclose(expected_row[key],value,abs_tol=1e-6)
            observations+=1
    result=dict(passed=True,assignment_rows=len(params),trip_rows=len(trips),telemetry_rows=count,jerk_values_checked=validated_jerks,observed_driver_parameters_checked=observations,checks=['unique telemetry composite keys','trip joins','per-run persona balance','cyclic coverage when 12 runs','runtime parameter comparisons','numerical jerk','unique partition paths'])
    (folder/'readback_validation.json').write_text(json.dumps(result,indent=2))
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('folder'); print(json.dumps(validate(p.parse_args().folder),indent=2))
