"""Build a portable PhD handoff with fully labelled Parquet/CSV and read-back checks."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import yaml

from src.processing.enrich_dataset import digest
from src.simulation.run_experiment import PARAMS, ROOT

LABELS = ['run_id', 'vehicle_id', 'persona']
PARAMETER_DESCRIPTIONS = {
    'speedFactor': ('1', 'Individual desired-speed multiplier relative to the SUMO speed limit.'),
    'accel': ('m/s^2', 'Configured maximum acceleration capability.'),
    'decel': ('m/s^2', 'Configured normal deceleration capability.'),
    'apparentDecel': ('m/s^2', 'Deceleration capability apparent to other vehicles; equal to sampled decel.'),
    'emergencyDecel': ('m/s^2', 'Configured emergency deceleration capability; fixed at 9.'),
    'tau': ('s', 'Desired car-following time-headway parameter, not an observed headway.'),
    'minGap': ('m', 'Configured minimum standstill gap.'),
    'sigma': ('1', 'Krauss driver imperfection setting.'),
    'actionStepLength': ('s', 'Interval between driving decisions, rounded to step multiples.'),
    'startupDelay': ('s', 'Configured extra startup delay after stopping; runtime getter unavailable.'),
    'lcStrategic': ('1', 'LC2013 strategic lane-changing setting.'),
    'lcSpeedGain': ('1', 'LC2013 speed-gain lane-changing setting.'),
    'lcCooperative': ('1', 'LC2013 cooperative lane-changing setting.'),
    'lcKeepRight': ('1', 'LC2013 keep-right setting.'),
    'lcAssertive': ('1', 'LC2013 assertiveness setting.'),
    'lcOvertakeRight': ('1', 'LC2013 right-overtaking setting.'),
}
PERSONA_DESCRIPTIONS = {
    'baseline': 'Reference behaviour using all default parameter ranges.',
    'cautious': 'Lower preferred speed and acceleration; larger gaps and headway.',
    'defensive': 'Longer headway and larger gaps; lower imperfection settings.',
    'smooth_consistent': 'Lower acceleration and imperfection; longer headway.',
    'speed_prone': 'Higher preferred speed and speed-gain lane-changing setting.',
    'aggressive': 'Higher speed and acceleration; shorter headway and smaller gaps.',
    'tailgater': 'Shorter desired headway and smaller standstill gaps.',
    'frequent_lane_changer': 'Higher speed-gain and strategic lane-changing settings.',
    'impatient': 'Higher acceleration and lane-changing motivation; lower cooperation.',
    'erratic': 'High imperfection setting and broad preferred-speed range.',
    'delayed_response': 'Longer decision interval, with desired headway above that interval.',
    'hesitant': 'Lower speed and acceleration, larger gaps, and additional startup delay.',
}
TELEMETRY_DESCRIPTIONS = {
    'run_id': ('not applicable', 'Experiment run identifier; explicitly stored on every row.'),
    'vehicle_id': ('not applicable', 'Original vehicle identifier, stored as string; repeats across runs.'),
    'persona': ('not applicable', 'Assigned synthetic profile for this run/vehicle; experimental design metadata.'),
    'time_s': ('s', 'Simulation time at the post-step observation, not wall-clock time.'),
    'speed_mps': ('m/s', 'Observed longitudinal vehicle speed.'),
    'accel_mps2': ('m/s^2', 'Observed longitudinal acceleration; braking is negative.'),
    'jerk_mps3': ('m/s^3', 'Consecutive acceleration difference divided by step duration; null at insertion/gaps.'),
    'lane_id': ('not applicable', 'Current SUMO lane identifier, including internal junction lanes.'),
    'edge_id': ('not applicable', 'Current SUMO edge identifier, including internal junction edges.'),
    'lane_index': ('index', 'Current zero-based lane index within the SUMO edge.'),
    'lane_position_m': ('m', 'Longitudinal position along the current SUMO lane.'),
    'x_m': ('m', 'SUMO network-local Cartesian x coordinate, not longitude.'),
    'y_m': ('m', 'SUMO network-local Cartesian y coordinate, not latitude.'),
    'distance_m': ('m', 'Cumulative SUMO distance travelled since departure.'),
    'lane_max_speed_mps': ('m/s', 'Current lane speed limit from SUMO; real-world legal limit unverified.'),
    'speed_to_lane_limit_ratio': ('1', 'Observed speed divided by positive SUMO lane speed limit.'),
    'leader_id': ('not applicable', 'Leader on current best lanes from 200 m minimum lookahead; null if unavailable.'),
    'leader_gap_sumo_m': ('m', 'TraCI leader gap excluding follower minGap.'),
    'leader_gap_physical_m': ('m', 'Physical bumper gap = reported SUMO gap + follower minGap.'),
    'leader_speed_mps': ('m/s', 'Observed leader speed at the same simulation step.'),
    'time_headway_s': ('s', 'Physical gap divided by positive ego speed; null when undefined.'),
    'longitudinal_ttc_s': ('s', 'Nonnegative physical gap divided by positive closing speed; null otherwise; not SSM TTC.'),
    'current_waiting_time_s': ('s', 'Current consecutive SUMO waiting time; not total trip waiting time.'),
    'upcoming_tls_id': ('not applicable', 'Next traffic-light identifier; null if none is returned.'),
    'upcoming_tls_distance_m': ('m', 'Observed distance to next traffic light.'),
    'upcoming_tls_state': ('not applicable', 'Observed next signal link-state character; does not establish a violation.'),
}


def ordered_labels(table):
    return table.select([c for c in LABELS if c in table.column_names] + [c for c in table.column_names if c not in LABELS])


def read_csv_typed(path, schema):
    return pacsv.read_csv(path, convert_options=pacsv.ConvertOptions(
        column_types=schema, null_values=[''], strings_can_be_null=True,
        quoted_strings_can_be_null=False))


def export_pair(table, destination, report):
    """Read back and compare every cell, including labels and nulls, in both formats."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    table = ordered_labels(table).combine_chunks()
    pq.write_table(table, destination.with_suffix('.parquet'), compression='zstd')
    pacsv.write_csv(table, destination.with_suffix('.csv'))
    back = pq.ParquetFile(destination.with_suffix('.parquet')).read()
    csv_back = read_csv_typed(destination.with_suffix('.csv'), table.schema)
    if not back.equals(table) or not csv_back.equals(table):
        raise ValueError(f'Round-trip mismatch for {destination}')
    report.append(dict(table=destination.as_posix(), rows=table.num_rows,
                       parquet_exact_roundtrip=True, csv_exact_roundtrip=True))
    return table


def labelled_telemetry(table, run_id, assignments):
    if 'run_id' not in table.column_names:
        table = table.append_column('run_id', pa.array([run_id]*table.num_rows, type=pa.string()))
    table = ordered_labels(table)
    previous = {}
    for row in table.select(LABELS+['time_s']).to_pylist():
        if row['run_id'] != run_id or any(row[k] is None for k in LABELS):
            raise ValueError('Missing or incorrect telemetry identifier')
        key = (row['run_id'], row['vehicle_id'])
        if assignments.get(key) != row['persona']:
            raise ValueError('Telemetry persona differs from individual assignment')
        prior = previous.get(key)
        if prior is not None and row['time_s'] <= prior:
            raise ValueError('Duplicate or nonmonotonic driver/time key')
        previous[key] = row['time_s']
    return table


def range_rows(config, params):
    rows = []
    for persona, overrides in config['profiles'].items():
        group = [p for p in params if p['persona']==persona]
        effective = config['default_ranges'] | overrides
        for key in PARAMS+['apparentDecel','emergencyDecel']:
            low, high = effective['decel'] if key=='apparentDecel' else [9.,9.] if key=='emergencyDecel' else effective[key]
            unit, description = PARAMETER_DESCRIPTIONS[key]
            rows.append(dict(persona=persona,parameter=key,unit=unit,
                             configured_min=float(low),configured_max=float(high),
                             actual_sampled_min=min(p[key] for p in group),
                             actual_sampled_max=max(p[key] for p in group),
                             actual_sampled_mean=sum(p[key] for p in group)/len(group),
                             sampling='equal to sampled decel' if key=='apparentDecel' else 'fixed' if low==high else 'uniform individual',
                             runtime_verification='unavailable getter; XML accepted' if key=='startupDelay' else 'matched individual TraCI getter within 1e-6',
                             description=description))
    return rows


def numeric_stats(table, run_id):
    result = []
    for field in table.schema:
        if not (pa.types.is_floating(field.type) or pa.types.is_integer(field.type)):
            continue
        column = table[field.name]
        valid = len(column)-column.null_count
        result.append(dict(run_id=run_id,column=field.name,unit=TELEMETRY_DESCRIPTIONS[field.name][0],
                           rows=len(column),valid_count=valid,null_count=column.null_count,
                           minimum=pc.min(column).as_py(),maximum=pc.max(column).as_py(),
                           mean=pc.mean(column).as_py(),sample_stddev=pc.stddev(column,ddof=1).as_py() if valid>1 else None))
    return result


def build(source, enrichment, output):
    source, enrichment, output = map(lambda p:Path(p).resolve(),(source,enrichment,output))
    if output.exists(): raise FileExistsError('Handoff already exists; choose a new output path')
    source_files = list(source.rglob('*'))+list(enrichment.rglob('*'))
    before = {str(p):digest(p) for p in source_files if p.is_file()}
    original_manifest = json.loads((source/'manifest.json').read_text())
    for name, checksum in original_manifest['input_hashes'].items():
        if digest(ROOT/name)!=checksum: raise ValueError('Original input differs: '+name)
    config_path = ROOT/'config/driver_profiles.yaml'
    runs = pq.read_table(source/'simulation_runs.parquet')
    run_rows = runs.to_pylist()
    if any(r['config_sha256']!=digest(config_path) for r in run_rows):
        raise ValueError('Persona configuration differs from actual experiment')
    config = yaml.safe_load(config_path.read_text())
    params = pq.read_table(source/'driver_parameters.parquet')
    parameter_rows = params.to_pylist()
    assignments = {(p['run_id'],p['vehicle_id']):p['persona'] for p in parameter_rows}
    if len(assignments)!=params.num_rows: raise ValueError('Duplicate assignment keys')
    output.mkdir(parents=True)
    checks = []; statistics = []; counts = []; telemetry_files = []
    for path in sorted((source/'telemetry').rglob('*.parquet')):
        run_id = path.parent.name.split('=',1)[1]
        table = labelled_telemetry(pq.ParquetFile(path).read(),run_id,assignments)
        dest = output/'telemetry'/run_id/'telemetry'
        table = export_pair(table,dest,checks)
        telemetry_files.append(dest.with_suffix('.parquet'))
        statistics.extend(numeric_stats(table,run_id))
        per_persona = Counter(table['persona'].to_pylist())
        for persona,count in sorted(per_persona.items()):
            counts.append(dict(run_id=run_id,persona=persona,telemetry_rows=count,
                               assigned_vehicle_count=sum(p['run_id']==run_id and p['persona']==persona for p in parameter_rows)))
        print(f'{run_id}: {table.num_rows:,} telemetry rows; Parquet and CSV exact read-back passed',flush=True)
    expected_runs = {r['run_id'] for r in run_rows}
    if {p.parent.name for p in telemetry_files}!=expected_runs: raise ValueError('Missing/extra telemetry run')
    for name in ['driver_parameters','trip_summary','driving_events','simulation_runs']:
        export_pair(pq.read_table(source/(name+'.parquet')),output/'tables'/name,checks)
    observed = []
    for file in sorted((source/'raw').glob('*/observed_parameters.parquet')):
        for row in pq.read_table(file).to_pylist():
            row['persona'] = assignments[(row['run_id'],row['vehicle_id'])]
            observed.append(row)
    if len(observed)!=sum(r['departed_count'] for r in run_rows): raise ValueError('Missing observed parameters')
    export_pair(pa.Table.from_pylist(observed),output/'personas'/'observed_vehicle_parameters',checks)
    export_pair(params,output/'personas'/'individual_sampled_parameters',checks)
    ranges = range_rows(config,parameter_rows)
    export_pair(pa.Table.from_pylist(ranges),output/'personas'/'persona_parameter_ranges',checks)
    catalog = [dict(persona=p,description=PERSONA_DESCRIPTIONS[p],empirical_calibration_status='not empirically calibrated',car_follow_model='Krauss',lane_change_model='LC2013',vehicle_count_per_run=25,trip_count=sum(r['persona']==p for r in parameter_rows)) for p in config['profiles']]
    export_pair(pa.Table.from_pylist(catalog),output/'personas'/'persona_catalog',checks)
    shutil.copyfile(config_path,output/'personas'/'driver_profiles.yaml')
    for file in sorted(enrichment.glob('*.parquet')):
        export_pair(pq.read_table(file),output/'research_features'/file.stem,checks)
    export_pair(pa.Table.from_pylist(statistics),output/'statistics'/'telemetry_numeric_statistics',checks)
    export_pair(pa.Table.from_pylist(counts),output/'statistics'/'run_persona_counts',checks)
    trips = pq.read_table(source/'trip_summary.parquet')
    events = pq.read_table(source/'driving_events.parquet')
    event_counts = Counter(events['event_type'].to_pylist())
    export_pair(pa.Table.from_pylist([dict(event_type=k,event_count=v) for k,v in sorted(event_counts.items())]),output/'statistics'/'event_counts',checks)
    for directory in ['documentation','evidence/experiment','evidence/enrichment','reproducibility']:
        (output/directory).mkdir(parents=True,exist_ok=True)
    for file in source.glob('*.json'): shutil.copyfile(file,output/'evidence/experiment'/file.name)
    for file in enrichment.glob('*.json'): shutil.copyfile(file,output/'evidence/enrichment'/file.name)
    shutil.copytree(source/'raw',output/'evidence'/'raw_balanced_runs')
    shutil.copytree(ROOT/'data/experiments/smoke_verified/raw',output/'evidence'/'successful_smoke_raw')
    for name in ['methodology.md','experiment_report.md','dataset_enrichment.md','data_interpretation.md']:
        shutil.copyfile(ROOT/'docs'/name,output/'documentation'/name)
    repro = output/'reproducibility'
    for name in ['network','routes','config','src','tests']:
        shutil.copytree(ROOT/name,repro/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ['london.sumocfg','requirements.txt']: shutil.copyfile(ROOT/name,repro/name)
    (repro/'data/raw').mkdir(parents=True)
    shutil.copyfile(ROOT/'data/raw/smoke_tripinfo.xml',repro/'data/raw/smoke_tripinfo.xml')
    dictionary = json.loads((source/'data_dictionary.json').read_text())
    dictionary.update(json.loads((enrichment/'data_dictionary.json').read_text()))
    for row in dictionary['telemetry']:
        row['unit'],row['description'] = TELEMETRY_DESCRIPTIONS[row['name']]
    dictionary['telemetry'] = sorted(dictionary['telemetry'],key=lambda r:table_order(r['name']))
    dictionary['persona_parameter_ranges'] = [dict(name=key,description=key.replace('_',' '),arrow_type=str(field.type),unit='parameter-specific; see unit column') for key,field in [(f.name,f) for f in pq.read_table(output/'personas/persona_parameter_ranges.parquet').schema]]
    (output/'documentation/data_dictionary.json').write_text(json.dumps(dictionary,indent=2))
    flattened = []
    for name,fields in dictionary.items():
        for field in fields:
            flattened.append(dict(table=name,column=field['name'],arrow_type=field.get('arrow_type',''),unit=field.get('unit',''),description=field.get('description',''),missingness=field.get('missingness','See README; blank CSV cell represents null.'),provenance=field.get('provenance','Packaged experiment configuration and observed values.')))
    export_pair(pa.Table.from_pylist(flattened),output/'documentation'/'data_dictionary',checks)
    enrichment_quality = json.loads((enrichment/'quality_report.json').read_text())
    summary = dict(run_count=len(run_rows),persona_count=len(config['profiles']),expected_trips=sum(r['expected_count'] for r in run_rows),completed_trips=sum(r['arrived_count'] for r in run_rows),incomplete_trips=sum(r['incomplete_count'] for r in run_rows),teleported_vehicles=sum(r['teleported_count'] for r in run_rows),teleport_events=sum(r['teleport_event_count'] for r in run_rows),collision_records=sum(r['collision_count'] for r in run_rows),telemetry_rows=sum(c['telemetry_rows'] for c in counts),driving_events=events.num_rows,total_trip_distance_km=pc.sum(trips['route_distance_m']).as_py()/1000,total_trip_hours=pc.sum(trips['duration_s']).as_py()/3600,median_trip_distance_km=pc.quantile(trips['route_distance_m'],q=.5)[0].as_py()/1000,median_trip_duration_s=pc.quantile(trips['duration_s'],q=.5)[0].as_py(),event_counts=dict(event_counts),short_trip_under_60s_count=enrichment_quality['short_trip_under_60s_count'],short_trip_under_1km_count=enrichment_quality['short_trip_under_1km_count'],no_headway_observation_trip_count=enrichment_quality['no_headway_observation_trip_count'],no_closing_ttc_observation_trip_count=enrichment_quality['no_closing_ttc_observation_trip_count'])
    (output/'statistics/dataset_summary.json').write_text(json.dumps(summary,indent=2))
    persona_lines = ['# Personas and configured settings','', 'These are exploratory SUMO profiles, not empirically calibrated human-driver categories. All supported parameters are sampled independently and uniformly within the effective ranges below, except fixed/dependent settings. Actual per-driver values are in individual_sampled_parameters. Runtime retrieval is in observed_vehicle_parameters; startupDelay is null because its getter was unavailable.','']
    for p in config['profiles']:
        persona_lines += [f'## {p}', '',PERSONA_DESCRIPTIONS[p],'','| Parameter | Unit | Configured range | Actual sampled range |','|---|---|---|---|']
        for row in ranges:
            if row['persona']==p: persona_lines.append(f"| {row['parameter']} | {row['unit']} | {row['configured_min']:.6g}–{row['configured_max']:.6g} | {row['actual_sampled_min']:.8g}–{row['actual_sampled_max']:.8g} |")
        persona_lines += ['']
    (output/'personas/PERSONAS_AND_SETTINGS.md').write_text('\n'.join(persona_lines),encoding='utf-8')
    write_readme(output,summary)
    unchanged = all(digest(path)==checksum for path,checksum in before.items())
    if not unchanged: raise ValueError('Source dataset changed during packaging')
    # Record portable paths in the validation report.
    for item in checks: item['table']=str(Path(item['table']).relative_to(output)).replace('\\','/')
    validation = dict(passed=True,source_artifacts_unchanged=unchanged,fully_labelled_telemetry_rows=summary['telemetry_rows'],null_identifier_count=0,duplicate_driver_time_keys=0,persona_assignment_mismatches=0,all_csv_and_parquet_cells_exactly_match_sources=True,table_pairs_checked=len(checks),tables=checks)
    (output/'VALIDATION.json').write_text(json.dumps(validation,indent=2))
    manifest = dict(created_utc=datetime.now(timezone.utc).isoformat(),source_experiment='balanced_12',smoke_in_main_telemetry=False,csv_null_encoding='unquoted empty field',telemetry_key=['run_id','vehicle_id','time_s'],assignment_key=['run_id','vehicle_id'],source_sha256={str(Path(path).relative_to(ROOT)).replace('\\','/'):value for path,value in before.items()})
    (output/'MANIFEST.json').write_text(json.dumps(manifest,indent=2))
    checksums = {str(p.relative_to(output)).replace('\\','/'):digest(p) for p in sorted(output.rglob('*')) if p.is_file()}
    (output/'SHA256SUMS.json').write_text(json.dumps(checksums,indent=2))
    print(json.dumps(dict(output=str(output),summary=summary,validation_passed=True,file_count=len(checksums)+1,bytes=sum(p.stat().st_size for p in output.rglob('*') if p.is_file())),indent=2),flush=True)
    return validation


def table_order(name):
    return LABELS.index(name) if name in LABELS else 3 + list(TELEMETRY_DESCRIPTIONS).index(name)


def write_readme(output,summary):
    text = f'''# SUMO multi-persona research handoff

Start here. This folder is self-contained and contains the twelve balanced SUMO 1.28.0 experiments and supporting evidence. No classifier, risk score, premiums or ML model is included.

## Main data

- `telemetry/run_0001/telemetry.parquet` and `telemetry.csv`, through `run_0012`: all **{summary['telemetry_rows']:,} observations**, in both formats. Each physical file explicitly contains **run_id, vehicle_id, persona** as its first three columns. Every numeric observation is on the same row as its identifiers. No directory parsing or assignment join is needed to identify a telemetry row.
- `personas/PERSONAS_AND_SETTINGS.md`: all twelve persona descriptions, full effective parameter ranges, and actual sampled ranges.
- `personas/persona_parameter_ranges.csv` / `.parquet`: machine-readable full persona settings, including defaults and overrides.
- `personas/individual_sampled_parameters.csv` / `.parquet`: exact settings for every one of the 3,600 run/vehicle assignments, including persona, departure plan, route edges and sampling seed.
- `personas/observed_vehicle_parameters.csv` / `.parquet`: departure-time values actually retrieved from SUMO, labelled with run, vehicle and persona. startupDelay is unavailable/null; it must not be described as runtime-verified.
- `tables/`: trip summaries, driving events, simulation runs and individual driver parameters in both formats.
- `research_features/`: exposure-aware trip features, road context, missingness, threshold sensitivity and descriptive persona summaries in both formats.
- `documentation/data_dictionary.csv` / `.parquet` / `.json`: field types, units, definitions, provenance and missingness for telemetry and original/research tables. See methodology and enrichment guide for formulas.
- `statistics/`: machine-readable dataset totals, per-run/persona row counts, numeric telemetry statistics and event counts.
- `evidence/`: original quality reports, manifests, raw XML/logs/generated scenarios for all twelve runs and the successful smoke. Raw commands contain original machine paths; use the included relative-input CLI to reproduce on another machine.
- `reproducibility/`: source code, tests, pinned dependency list, original network, routes, configuration and baseline TripInfo. These are copies; source files were not modified.

## Labels and reading data

Vehicle IDs are strings. The same vehicle ID appears in different runs and under different personas. Use **(run_id, vehicle_id)** for trip/parameter joins and **(run_id, vehicle_id, time_s)** for telemetry keys. A persona is experimental metadata, not an observed outcome or validated human category. There are 25 vehicles per persona per run; each of 300 original routes is simulated under all twelve personas once. Vehicles from a run interact with each other, so their records are not independent experiments.

```python
from pathlib import Path
import pyarrow.parquet as pq
import pandas as pd

root = Path('.')  # this handoff folder
one_run = pq.read_table(root / 'telemetry/run_0001/telemetry.parquet')
all_runs = pq.read_table(sorted((root / 'telemetry').glob('run_*/telemetry.parquet')))
# CSV alternative; dtype prevents vehicle IDs becoming numeric.
one_run_csv = pd.read_csv(root / 'telemetry/run_0001/telemetry.csv',
                          dtype={{'run_id': 'string', 'vehicle_id': 'string', 'persona': 'string'}})
```

CSV has a header, UTF-8 encoding, quoted text, and empty unquoted fields for nulls. Null means unavailable/undefined, not zero. Each run is separate to avoid one huge file. Across the full dataset there are more rows than a single Excel worksheet can hold; use Parquet/Python/R for full analysis. Values remain in the original SI units. Export validation reads both formats back with explicit Arrow types and compares every cell, including text, numerical values and nulls.

## Actual totals

- Runs: {summary['run_count']}; personas: {summary['persona_count']}; time step: 0.5 s; base seed: 1001.
- Completed trips: {summary['completed_trips']:,}; incomplete: {summary['incomplete_trips']}; teleported vehicles: {summary['teleported_vehicles']}; recorded collisions: {summary['collision_records']}.
- Authoritative TripInfo distance: {summary['total_trip_distance_km']:,.2f} km; duration: {summary['total_trip_hours']:,.2f} vehicle-hours.
- Driving-event episodes/official lane changes: {summary['driving_events']:,}.
- Zero headway-observation trips: {summary['no_headway_observation_trip_count']}; zero defined closing-TTC-observation trips: {summary['no_closing_ttc_observation_trip_count']}.

The separate successful smoke ran before the twelve-run experiment and completed 300 trips. Smoke observations are **not** added to main telemetry or totals. Smoke raw evidence is included separately.

## Collision-count interpretation

Zero collisions were recorded under configured checks. The saved experiment commands did not enable `--collision.check-junctions`, whose installed SUMO 1.28.0 default was false; optional junction shape-overlap checks were not enabled. Zero records therefore do not establish that all possible intersection collisions were checked or that real-world crash risk is zero. Safety-oriented Krauss settings remained active. See documentation/data_interpretation.md for the evidence, event definitions, missingness and limitations. No new simulation was run for this explanation.

## Interpretation limits

The persona settings are exploratory and **not empirically calibrated** to measured human driving. Road legal limits remain unverified; the audit lists 153 tertiary lanes near 80 km/h, and exceedance metrics refer only to SUMO lane values. Absence of headway/TTC observations is not evidence of safe following. TTC is a longitudinal closing calculation and is not an SSM crash probability. No SSM device measurements or red-light violation labels are supplied. The installed startupDelay getter did not return a value. First/arrival intervals and observation gaps limit sampled exposure; use TripInfo for whole-trip distance/duration. No measured weather, road-surface, claim, crash-outcome or demographic data were collected. Dataset engineering checks do not establish external validity.

`VALIDATION.json` records packaging checks; `SHA256SUMS.json` lists every other packaged file's checksum. Original reports are historical evidence, and their repository-relative paths refer to the original workspace. Use the paths in this README for the handoff.

## Reproduction

From `reproducibility/`, use an environment with SUMO 1.28.0 and the listed Python dependencies. The original runner accepts:

```shell
python -m src.simulation.run_experiment --runs 1 --seed 1001 --step-length 0.5 --output data/new_smoke
# Inspect smoke outputs before launching the full experiment.
python -m src.simulation.run_experiment --runs 12 --seed 1001 --step-length 0.5 --output data/new_balanced
```

Output directories must be new. The handoff packager is `src/processing/package_handoff.py` in the original workspace; it preserves full-precision labels and values and refuses to overwrite an existing handoff folder.
'''
    (output/'README.md').write_text(text,encoding='utf-8')
    lines = ['# Dataset statistics','', '| Measure | Actual value |','|---|---:|']
    for key in ['run_count','persona_count','expected_trips','completed_trips','incomplete_trips','teleported_vehicles','collision_records','telemetry_rows','driving_events','total_trip_distance_km','total_trip_hours','median_trip_distance_km','median_trip_duration_s']:
        lines.append(f'| {key} | {summary[key]:,.4f} |' if isinstance(summary[key],float) else f'| {key} | {summary[key]:,} |')
    lines += ['', '## Events', '', '| Detected event type | Count |','|---|---:|']
    lines += [f'| {k} | {v:,} |' for k,v in sorted(summary['event_counts'].items())]
    lines += ['', 'Event thresholds and minimum duration are in evidence/experiment/manifest.json. Counts combine threshold-qualified episodes with official lane changes. They are not verified crash or risk labels.', '', 'Telemetry numeric statistics are observation-weighted within each run: valid count, null count, minimum, maximum, mean and sample standard deviation (ddof=1). Consecutive observations are correlated; these are descriptive statistics, not independent-sample inferential estimates.']
    (output/'statistics/STATISTICS.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT/'data/experiments/balanced_12')
    parser.add_argument('--enrichment',type=Path,default=ROOT/'data/research_enrichment_v1')
    parser.add_argument('--output',type=Path,default=ROOT/'PhD_SUMO_Handoff')
    args=parser.parse_args()
    build(args.source,args.enrichment,args.output)


if __name__=='__main__': main()
