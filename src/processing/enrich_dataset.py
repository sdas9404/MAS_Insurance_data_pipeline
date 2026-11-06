"""Enrich an existing synthetic dataset without rerunning or relabelling drivers."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def ratio(numerator, denominator):
    return numerator / denominator if denominator > 0 else None


def route_group(edges, route_id=None):
    if edges:
        return hashlib.sha256(' '.join(edges.split()).encode()).hexdigest()
    # Referenced routes without recorded edges cannot be matched to edge sequences.
    return 'reference:' + route_id if route_id else None


def interval(previous, row, step):
    if previous is None:
        return 0., 0.
    elapsed = row['time_s'] - previous['time_s']
    if elapsed <= 0 or elapsed > step * 1.01:
        return 0., 0.
    current, prior = row.get('distance_m'), previous.get('distance_m')
    distance = max(0., current - prior) if current is not None and prior is not None else 0.
    return elapsed, distance


def update(state, row, dt, ds, thresholds):
    state['sample_count'] += 1
    state['observed_interval_s'] += dt
    state['observed_distance_m'] += ds
    speed = row.get('speed_mps')
    if speed is not None:
        state['speed_observable_s'] += dt
        state['moving_s' if speed > .1 else 'stopped_s'] += dt
    for field, total, duration in [('accel_mps2','accel_squared_integral','accel_observable_s'), ('jerk_mps3','jerk_squared_integral','jerk_observable_s')]:
        value = row.get(field)
        if value is not None:
            state[total] += value * value * dt
            state[duration] += dt
    leader = row.get('leader_id')
    if leader:
        state['leader_observable_s'] += dt
    gap = row.get('leader_gap_physical_m')
    headway = row.get('time_headway_s')
    if leader and gap is not None and gap >= 0 and headway is not None and headway >= 0:
        state['headway_observable_s'] += dt
        if headway < thresholds['headway_s']:
            state['short_headway_sample_s'] += dt
    ttc = row.get('longitudinal_ttc_s')
    if leader and gap is not None and gap >= 0 and ttc is not None and ttc >= 0:
        state['closing_ttc_observable_s'] += dt
        if ttc < thresholds['ttc_s']:
            state['low_ttc_sample_s'] += dt
    speed_ratio = row.get('speed_to_lane_limit_ratio')
    if speed_ratio is not None:
        state['network_ratio_observable_s'] += dt
        if speed_ratio > thresholds['network_speed_ratio']:
            state['network_exceedance_sample_s'] += dt


def schema_for(rows):
    # Types are explicit and remain stable for all-null columns.
    string_columns = {'run_id','vehicle_id','persona','route_group_id','road_class','lane_id','edge_id','column','event_type'}
    bool_columns = {'short_trip_under_60s','short_trip_under_1km','completed'}
    fields = []
    for name in rows[0]:
        dtype = pa.string() if name in string_columns else pa.bool_() if name in bool_columns else pa.int64() if name.endswith('_count') or name in {'rows','null_count'} else pa.float64()
        fields.append((name, dtype))
    return pa.schema(fields)


def write(path, rows):
    table = pa.Table.from_pylist(rows, schema=schema_for(rows))
    pq.write_table(table, path, compression='zstd')
    return table


def enrich(source, output, network=ROOT/'network/london_filtered.net.xml'):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError('Use a new enrichment output directory')
    # Hash every original artifact; additions live outside the original experiment.
    source_hashes = {str(p.relative_to(source)): digest(p) for p in source.rglob('*') if p.is_file()}
    manifest = json.loads((source/'manifest.json').read_text())
    expected_network = manifest['input_hashes'].get('network/london_filtered.net.xml')
    if expected_network != digest(network):
        raise ValueError('Network differs from experiment; contextual joins would be invalid')
    thresholds = manifest['thresholds']
    runs = pq.read_table(source/'simulation_runs.parquet').to_pylist()
    steps = {r['run_id']: r['step_length_s'] for r in runs}
    params = pq.read_table(source/'driver_parameters.parquet').to_pylist()
    trips = pq.read_table(source/'trip_summary.parquet').to_pylist()
    expected = {(p['run_id'],p['vehicle_id']):p for p in params}
    if len(expected) != len(params):
        raise ValueError('Duplicate driver keys')
    road_classes = {}
    for edge in ET.parse(network).getroot().findall('edge'):
        road_classes[edge.get('id')] = 'internal' if edge.get('function') == 'internal' else edge.get('type') or 'unknown'
    stats = defaultdict(Counter)
    contexts = defaultdict(Counter)
    missing = defaultdict(Counter)
    anomalies = Counter()
    sensitivity = defaultdict(Counter)
    for file in sorted((source/'telemetry').rglob('*.parquet')):
        run = file.parent.name.split('=',1)[1]
        previous = {}
        for batch in pq.ParquetFile(file).iter_batches(batch_size=10000):
            for row in batch.to_pylist():
                key = (run,row['vehicle_id'])
                if key not in expected or row['persona'] != expected[key]['persona']:
                    raise ValueError('Unknown or inconsistent driver')
                dt, ds = interval(previous.get(key),row,steps[run])
                update(stats[key],row,dt,ds,thresholds)
                context = road_classes.get(row['edge_id'],'unknown')
                c = contexts[(run,row['vehicle_id'],context)]
                c['observed_time_s'] += dt; c['observed_distance_m'] += ds; c['sample_count'] += 1
                for name,value in row.items():
                    missing[(run,name)]['rows'] += 1
                    missing[(run,name)]['null_count'] += int(value is None)
                    if isinstance(value,float) and not math.isfinite(value): anomalies['nonfinite_values'] += 1
                if row.get('leader_gap_physical_m') is not None and row['leader_gap_physical_m'] < 0: anomalies['negative_physical_gap_samples'] += 1
                if row.get('speed_mps') is not None and row['speed_mps'] < 0: anomalies['negative_speed_samples'] += 1
                if row.get('time_headway_s') is not None and row['time_headway_s'] < 0: anomalies['negative_headway_samples'] += 1
                for measure, limits in [('time_headway_s',[.5,1.,1.5]),('longitudinal_ttc_s',[.5,1.,1.5,2.])]:
                    value = row.get(measure)
                    if value is None or value < 0 or row.get('leader_gap_physical_m') is None or row['leader_gap_physical_m'] < 0: continue
                    for threshold in limits:
                        s = sensitivity[(measure,threshold)]
                        s['observable_time_s'] += dt
                        if value < threshold: s['below_threshold_time_s'] += dt
                previous[key] = row
    features = []
    for trip in trips:
        key = (trip['run_id'],trip['vehicle_id']); s = stats[key]; p = expected[key]
        row = {k:trip[k] for k in ['run_id','vehicle_id','persona','completed']}
        row.update(route_group_id=route_group(p.get('route_edges'),p.get('route_id')),sample_count=int(s['sample_count']),observed_time_s=s['observed_interval_s'],observed_distance_km=s['observed_distance_m']/1000,
                   distance_coverage_fraction=ratio(s['observed_distance_m'],trip.get('route_distance_m') or 0),time_coverage_fraction=ratio(s['observed_interval_s'],trip.get('duration_s') or 0),
                   short_trip_under_60s=trip['duration_s']<60 if trip['duration_s'] is not None else None,short_trip_under_1km=trip['route_distance_m']<1000 if trip['route_distance_m'] is not None else None)
        for name in ['speed_observable_s','moving_s','stopped_s','leader_observable_s','headway_observable_s','closing_ttc_observable_s','network_ratio_observable_s','short_headway_sample_s','low_ttc_sample_s','network_exceedance_sample_s']:
            row[name] = s[name]
        row['headway_observation_fraction'] = ratio(s['headway_observable_s'],s['observed_interval_s'])
        row['short_headway_observable_fraction'] = ratio(s['short_headway_sample_s'],s['headway_observable_s'])
        row['low_ttc_closing_fraction'] = ratio(s['low_ttc_sample_s'],s['closing_ttc_observable_s'])
        row['network_exceedance_observable_fraction'] = ratio(s['network_exceedance_sample_s'],s['network_ratio_observable_s'])
        for name in ['accel','jerk']:
            value = ratio(s[name+'_squared_integral'],s[name+'_observable_s'])
            row[name+'_rms_mps'+('2' if name=='accel' else '3')] = math.sqrt(value) if value is not None else None
        for name,value in trip.items():
            if name.endswith('_count') and name != 'teleport_count':
                row[name+'_per_100km'] = ratio(value*100,row['observed_distance_km'])
                row[name+'_per_observed_hour'] = ratio(value*3600,row['observed_time_s'])
        # Verify independently reconstructed exposure against the prior summaries.
        if not math.isclose(row['observed_time_s'],trip['exposed_time_s'],abs_tol=1e-6) or not math.isclose(row['observed_distance_km'],trip['exposed_km'],abs_tol=1e-6):
            raise ValueError('Reconstructed exposure differs from original summaries')
        features.append(row)
    if len(features) != len(expected) or len({(r['run_id'],r['vehicle_id']) for r in features}) != len(features):
        raise ValueError('Trip join is not one-to-one')
    context_rows = [dict(run_id=run,vehicle_id=vid,persona=expected[(run,vid)]['persona'],road_class=kind,sample_count=int(c['sample_count']),observed_time_s=c['observed_time_s'],observed_distance_km=c['observed_distance_m']/1000) for (run,vid,kind),c in sorted(contexts.items())]
    missing_rows = [dict(run_id=run,column=column,rows=int(c['rows']),null_count=int(c['null_count']),null_fraction=ratio(c['null_count'],c['rows'])) for (run,column),c in sorted(missing.items())]
    sensitivity_rows = [dict(column=measure,threshold_s=threshold,observable_time_s=c['observable_time_s'],below_threshold_time_s=c['below_threshold_time_s'],below_threshold_fraction=ratio(c['below_threshold_time_s'],c['observable_time_s'])) for (measure,threshold),c in sorted(sensitivity.items())]
    persona_rows = []
    for persona in sorted({r['persona'] for r in features}):
        group = [r for r in features if r['persona']==persona]
        kilometers = sum(r['observed_distance_km'] for r in group)
        row = dict(persona=persona,trip_count=len(group),observed_distance_km=kilometers,observed_time_s=sum(r['observed_time_s'] for r in group))
        for field in ['headway_observable_s','short_headway_sample_s','closing_ttc_observable_s','low_ttc_sample_s','network_ratio_observable_s','network_exceedance_sample_s']:
            row[field] = sum(r[field] for r in group)
        for numerator,denominator,label in [('short_headway_sample_s','headway_observable_s','short_headway_observable_fraction'),('low_ttc_sample_s','closing_ttc_observable_s','low_ttc_closing_fraction'),('network_exceedance_sample_s','network_ratio_observable_s','network_exceedance_observable_fraction')]:
            row[label] = ratio(row[numerator],row[denominator])
        persona_rows.append(row)
    output.mkdir(parents=True)
    tables = {'trip_research_features':features,'road_context_exposure':context_rows,'telemetry_missingness':missing_rows,'threshold_sensitivity':sensitivity_rows,'persona_exposure_summary':persona_rows}
    dictionaries = {}
    for name,rows in tables.items():
        table = write(output/(name+'.parquet'),rows)
        readback = pq.read_table(output/(name+'.parquet'))
        if readback.num_rows != len(rows): raise ValueError('Parquet row count mismatch')
        dictionaries[name] = [dict(name=f.name,type=str(f.type),nullable=f.nullable) for f in table.schema]
    report = dict(source=str(source),row_counts={k:len(v) for k,v in tables.items()},telemetry_rows=sum(s['sample_count'] for s in stats.values()),anomalies=dict(anomalies),unique_route_groups=len({r['route_group_id'] for r in features}),short_trip_under_60s_count=sum(bool(r['short_trip_under_60s']) for r in features),short_trip_under_1km_count=sum(bool(r['short_trip_under_1km']) for r in features),no_headway_observation_trip_count=sum(r['headway_observable_s']==0 for r in features),no_closing_ttc_observation_trip_count=sum(r['closing_ttc_observable_s']==0 for r in features),checks=['one-to-one expected trip joins','independent reconstruction matches original exposure','Parquet row count readback'],thresholds=thresholds)
    report['source_artifacts_unchanged'] = all(digest(source/name)==value for name,value in source_hashes.items())
    if not report['source_artifacts_unchanged']: raise ValueError('Source artifacts changed')
    (output/'quality_report.json').write_text(json.dumps(report,indent=2))
    (output/'source_manifest.json').write_text(json.dumps(dict(source_files_sha256=source_hashes,network_sha256=digest(network),script_sha256=digest(__file__),step_lengths=steps),indent=2))
    (output/'schemas.json').write_text(json.dumps(dictionaries,indent=2))
    print(json.dumps(report,indent=2),flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--network',type=Path,default=ROOT/'network/london_filtered.net.xml')
    args = parser.parse_args()
    enrich(args.source,args.output,args.network)


if __name__=='__main__': main()
