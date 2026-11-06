"""Dataset dictionary generated from actual Parquet schemas."""
import json
from pathlib import Path
import pyarrow.parquet as pq

DESCRIPTIONS={
 'leader_gap_sumo_m':'TraCI getLeader distance excluding ego minGap; leader search minimum lookahead 200 m.',
 'leader_gap_physical_m':'SUMO leader gap plus individually sampled ego minGap; physical bumper gap.',
 'longitudinal_ttc_s':'Physical gap / positive closing speed; null for no leader, nonclosing leader or negative physical gap.',
 'time_headway_s':'Physical bumper gap / positive ego speed; null at zero speed or without leader.',
 'jerk_mps3':'Consecutive acceleration difference / simulation step; null after insertion or sampling gap.',
 'distance_weighted_speed_ratio':'Sum(observed distance increment * speed/lane limit) / valid observed distance.',
 'current_waiting_time_s':'TraCI current consecutive waiting time; not cumulative TripInfo waitingTime.',
 'duration_s':'TripInfo duration for trips; accumulated qualifying sample time for events.',
 'peak_value':'Maximum acceleration, braking magnitude, speed ratio, inverse headway (1/s) or inverse TTC (1/s), conditional on event_type; null for lane changes.',
 'failed_departures_count':'Null: pending vehicles at cap do not establish failed departure.',
 'route_id':'Original route reference/inline ID; null for anonymous inline route; never synthesized.',
 'route_edges':'Original inline route edge sequence; null when vehicle uses a named route reference.',
 'startupDelay':'Configured delay before starting after a stop; runtime getter unavailable in installed SUMO 1.28.',
 'status':'completed, incomplete (departed but not arrived), or not_departed_by_cap.',
 'truncated':'True if cap reached with expected trips incomplete.',
 'exposed_time_s':'Time between consecutive observed states; excludes insertion, arrival and teleport gaps.',
 'exposed_km':'Nonnegative distance increments between consecutive observed states / 1000.',
 'network_speed_status':'unverified for supplied network: road legal limits not independently established.',
 'upcoming_tls_state':'Observed next signal link state; does not establish an infraction.',
 'collision_count':'Number of official SUMO collision XML records.',
 'colliding_vehicle_step_count':'Sum of colliding vehicles per step; may count a vehicle more than once.',
 'teleported_count':'Distinct vehicles with at least one observed starting teleport.',
 'incomplete_count':'Expected count minus arrived count, including vehicles not departed.',
 'start_s':'Beginning of first qualifying sample interval; lane changes use official timestamp.',
 'end_s':'End of last qualifying sample interval; lane changes use official timestamp.'}
PARAM_UNITS={'accel':'m/s^2','decel':'m/s^2','apparentDecel':'m/s^2','emergencyDecel':'m/s^2','tau':'s','minGap':'m','actionStepLength':'s','startupDelay':'s'}

def dictionary(folder):
    folder=Path(folder); result={}
    files=list(folder.glob('*.parquet'))+[next((folder/'telemetry').rglob('*.parquet'))]
    for path in files:
        name=path.stem if path.stem!='part-0000' else 'telemetry'
        fields=[]
        schema=pq.ParquetFile(path).schema_arrow
        if name=='telemetry':
            from src.simulation.run_experiment import TELEMETRY
            schema=TELEMETRY
        for field in schema:
            key=field.name
            unit=PARAM_UNITS.get(key,'1')
            if key.endswith('_s'): unit='s'
            if key.endswith('_m'): unit='m'
            if key.endswith('_km'): unit='km'
            if key.endswith('_mps'): unit='m/s'
            if key.endswith('_mps2'): unit='m/s^2'
            if key.endswith('_mps3'): unit='m/s^3'
            if key.endswith('_per_km'): unit='1/km'
            if str(field.type) in ['string','bool','null']: unit='not applicable'
            provenance={'telemetry':'TraCI post-step subscription/API; derived fields per methodology','driver_parameters':'Seeded per-driver configuration and original route XML','trip_summary':'Authoritative TripInfo left join with sampled exposure and episodes','driving_events':'Threshold episode detector or official lanechange XML','simulation_runs':'Execution manifest, source hashes and observed simulation counters'}[name]
            fields.append(dict(name=key,arrow_type=str(field.type),nullable=field.nullable,unit=unit,description=DESCRIPTIONS.get(key,key.replace('_',' ')),provenance=provenance,missingness='Null when unavailable or undefined; see data_quality.json for actual counts. Zero denotes an observed value/count, never a missing-value replacement.'))
        result[name]=fields
    (folder/'data_dictionary.json').write_text(json.dumps(result,indent=2))
    return result
