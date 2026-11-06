from collections import Counter
import math
from pathlib import Path
import json
import pyarrow.parquet as pq
from src.processing.enrich_dataset import interval, ratio, route_group, schema_for, update


def test_interval_excludes_first_observation_and_gaps():
    row=dict(time_s=1.,distance_m=10.)
    assert interval(None,row,.5)==(0.,0.)
    assert interval(dict(time_s=.5,distance_m=6.),row,.5)==(.5,4.)
    assert interval(dict(time_s=0.,distance_m=0.),row,.5)==(0.,0.)
    assert interval(dict(time_s=1.,distance_m=10.),row,.5)==(0.,0.)


def test_observation_denominators_and_rms():
    thresholds=dict(headway_s=1.,ttc_s=1.5,network_speed_ratio=1.05)
    s=Counter()
    base=dict(speed_mps=10.,accel_mps2=2.,jerk_mps3=4.,leader_id=None,speed_to_lane_limit_ratio=1.1)
    update(s,base,.5,5.,thresholds)
    row=base|dict(leader_id='leader',leader_gap_physical_m=5.,time_headway_s=.5,longitudinal_ttc_s=1.)
    update(s,row,.5,5.,thresholds)
    assert s['observed_interval_s']==1.
    assert s['headway_observable_s']==.5
    assert s['short_headway_sample_s']==.5
    assert ratio(s['short_headway_sample_s'],s['headway_observable_s'])==1.
    assert math.sqrt(s['accel_squared_integral']/s['accel_observable_s'])==2.
    assert math.sqrt(s['jerk_squared_integral']/s['jerk_observable_s'])==4.


def test_invalid_gaps_do_not_become_short_headway():
    s=Counter()
    row=dict(speed_mps=10.,leader_id='leader',leader_gap_physical_m=-1.,time_headway_s=-.1,longitudinal_ttc_s=None)
    update(s,row,.5,5.,dict(headway_s=1.,ttc_s=1.5,network_speed_ratio=1.05))
    assert s['leader_observable_s']==.5
    assert s['headway_observable_s']==s['short_headway_sample_s']==0
    assert ratio(0,0) is None


def test_route_group_and_explicit_null_schema():
    assert route_group('a b')==route_group(' a  b ')
    assert route_group('a b')!=route_group('b a')
    assert route_group(None,'shared')=='reference:shared'
    assert route_group(None,None) is None
    assert str(schema_for([dict(route_group_id=None,low_ttc_closing_fraction=None)]).field('low_ttc_closing_fraction').type)=='double'


def test_real_enrichment_readback():
    folder=Path(__file__).resolve().parents[1]/'data/research_enrichment_v1'
    if not (folder/'quality_report.json').exists():
        import pytest
        pytest.skip('Enrichment has not finished')
    report=json.loads((folder/'quality_report.json').read_text())
    assert report['source_artifacts_unchanged']
    table=pq.read_table(folder/'trip_research_features.parquet')
    rows=table.to_pylist()
    assert len(rows)==len({(r['run_id'],r['vehicle_id']) for r in rows})==3600
    for row in rows:
        for field in ['headway_observation_fraction','short_headway_observable_fraction','low_ttc_closing_fraction','network_exceedance_observable_fraction']:
            assert row[field] is None or 0<=row[field]<=1
    context=pq.read_table(folder/'road_context_exposure.parquet').to_pylist()
    assert math.isclose(sum(r['observed_distance_km'] for r in rows),sum(r['observed_distance_km'] for r in context),abs_tol=1e-6)
    assert math.isclose(sum(r['observed_time_s'] for r in rows),sum(r['observed_time_s'] for r in context),abs_tol=1e-6)
