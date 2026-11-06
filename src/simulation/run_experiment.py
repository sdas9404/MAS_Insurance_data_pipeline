"""Reproducible SUMO experiment; original inputs are read only."""
import argparse
import copy
import hashlib
import json
import math
import platform
import random
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute
import yaml
import traci
import traci.constants as tc

ROOT = Path(__file__).resolve().parents[2]
PARAMS = 'speedFactor accel decel tau minGap sigma actionStepLength startupDelay lcStrategic lcSpeedGain lcCooperative lcKeepRight lcAssertive lcOvertakeRight'.split()
INPUTS = ['network/london_filtered.net.xml', 'routes/london.rou.xml', 'london.sumocfg', 'data/raw/smoke_tripinfo.xml']
STRINGS = 'run_id vehicle_id persona lane_id edge_id leader_id upcoming_tls_id upcoming_tls_state'.split()
NUMBERS = 'time_s speed_mps accel_mps2 jerk_mps3 lane_position_m x_m y_m distance_m lane_max_speed_mps speed_to_lane_limit_ratio leader_gap_sumo_m leader_gap_physical_m leader_speed_mps time_headway_s longitudinal_ttc_s current_waiting_time_s upcoming_tls_distance_m'.split()
TELEMETRY = pa.schema([(s, pa.string()) for s in STRINGS] + [('lane_index', pa.int32())] + [(s, pa.float64()) for s in NUMBERS])
EVENT_SCHEMA = pa.schema([(s, pa.string()) for s in ['run_id','vehicle_id','persona','event_type','lane_id','edge_id','reason']] + [(s,pa.float64()) for s in ['start_s','end_s','duration_s','peak_value']])
THRESHOLDS = dict(acceleration=3.0, braking=-4.5, network_speed_ratio=1.05, headway_s=1.0, ttc_s=1.5, minimum_episode_s=1.0)

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def seed_for(seed, *parts):
    return int.from_bytes(hashlib.sha256((':'.join(map(str,(seed,)+parts))).encode()).digest()[:4], 'big') % 2147483647

def load_config(path, step):
    cfg = yaml.safe_load(Path(path).read_text())
    if len(cfg['profiles']) != 12 or cfg['simulation']['car_follow_model'] != 'Krauss' or cfg['simulation']['lane_change_model'] != 'LC2013':
        raise ValueError('Expected twelve Krauss/LC2013 profiles')
    if step <= 0: raise ValueError('step must be positive')
    for profile in cfg['profiles'].values():
        ranges = cfg['default_ranges'] | profile
        if set(ranges) != set(PARAMS): raise ValueError('Unknown or missing parameters')
        for key, bounds in ranges.items():
            lo, hi = bounds
            if not all(math.isfinite(x) for x in bounds) or lo > hi or lo < 0: raise ValueError(key)
            if key in ['speedFactor','accel','decel','tau','minGap','actionStepLength','lcAssertive'] and lo <= 0: raise ValueError(key)
            if key in ['sigma','lcCooperative','lcOvertakeRight'] and hi > 1: raise ValueError(key)
        if ranges['decel'][1] > 9: raise ValueError('emergencyDecel below decel')
        if ranges['tau'][0] < math.ceil(ranges['actionStepLength'][1]/step-1e-9)*step: raise ValueError('tau below rounded action step')
    return cfg

def scenario(cfg, run_index, base_seed, step, folder):
    tree = ET.parse(ROOT/'routes/london.rou.xml')
    root = tree.getroot()
    vehicles = root.findall('vehicle')
    if len(vehicles) != 300: raise ValueError('Expected 300 vehicles')
    order = list(range(len(vehicles)))
    random.Random(seed_for(base_seed,'assignment')).shuffle(order)
    ranks = {index:rank for rank,index in enumerate(order)}
    profiles = list(cfg['profiles'])
    rows = []
    for index, veh in enumerate(vehicles):
        persona = profiles[(ranks[index]//25+run_index)%12]
        sampling_seed = seed_for(base_seed,'sampling',run_index,veh.attrib['id'])
        rng = random.Random(sampling_seed)
        values = {k:rng.uniform(*v) for k,v in (cfg['default_ranges']|cfg['profiles'][persona]).items()}
        values['actionStepLength'] = math.ceil(values['actionStepLength']/step-1e-9)*step
        values.update(apparentDecel=values['decel'],emergencyDecel=9.0)
        type_id = 'individual_'+veh.attrib['id']
        root.insert(index, ET.Element('vType',dict(id=type_id,vClass='passenger',carFollowModel='Krauss',laneChangeModel='LC2013',speedDev='0',**{k:format(v,'.17g') for k,v in values.items()})))
        original_type = veh.get('type')
        veh.set('type',type_id)
        veh.set('speedFactor',format(values['speedFactor'],'.17g'))
        route = veh.find('route')
        rows.append(dict(vehicle_id=veh.get('id'),persona=persona,route_id=veh.get('route') or (route.get('id') if route is not None else None),route_edges=route.get('edges') if route is not None else None,original_type=original_type,planned_depart_s=float(veh.get('depart')),sampling_seed=sampling_seed,**values))
    path = folder/'scenario.rou.xml'
    tree.write(path,encoding='utf-8',xml_declaration=True)
    return path, rows

def finite(value):
    return float(value) if value is not None and math.isfinite(value) and value > -1e9 else None

def following(gap, min_gap, speed, leader_speed):
    physical = gap + min_gap
    return physical, physical/speed if speed > 0 else None, physical/(speed-leader_speed) if speed > leader_speed and physical >= 0 else None

class Episodes:
    def __init__(self, minimum): self.active={}; self.rows=[]; self.minimum=minimum
    def update(self, row, dt, conditions):
        for kind,(active,value) in conditions.items():
            key=(row['vehicle_id'],kind)
            if key in self.active and (not active or row['time_s']-self.active[key]['end_s'] > dt*1.01): self.close(key)
            if active:
                if key not in self.active:
                    self.active[key]={k:row.get(k) for k in ['run_id','vehicle_id','persona','lane_id','edge_id']}
                    self.active[key].update(event_type=kind,start_s=row['time_s']-dt,end_s=row['time_s'],duration_s=0.,peak_value=value,reason=None)
                e=self.active[key]; e['end_s']=row['time_s']; e['duration_s']+=dt; e['peak_value']=max(e['peak_value'],value)
    def close(self,key):
        e=self.active.pop(key)
        if e['duration_s']+1e-9 >= self.minimum: self.rows.append(e)
    def finish_vehicle(self,vid):
        for key in list(self.active):
            if key[0]==vid: self.close(key)
    def finish(self):
        for key in list(self.active): self.close(key)
        return self.rows

def write_table(path, rows, schema=None):
    pq.write_table(pa.Table.from_pylist(rows,schema=schema),path,compression='zstd')

def audit(out, status='unverified', evidence=None):
    rows=[]
    for edge in ET.parse(ROOT/INPUTS[0]).getroot().findall('edge'):
        if edge.get('function')=='internal': continue
        for lane in edge.findall('lane'):
            if edge.get('type')=='highway.tertiary' and float(lane.get('speed'))*3.6 >= 79.9:
                rows.append(dict(edge_id=edge.get('id'),lane_id=lane.get('id'),speed_mps=float(lane.get('speed')),speed_kmh=float(lane.get('speed'))*3.6))
    (out/'network_speed_audit.json').write_text(json.dumps(dict(network_speed_status=status,evidence=str(evidence) if evidence else None,tertiary_80_kmh_lanes=rows,limitation='No authoritative road-limit source supplied; exceedance is relative to SUMO network only.'),indent=2))

def run_one(args,cfg,index,out):
    run_id=f'run_{index+1:04d}'
    raw=out/'raw'/run_id; raw.mkdir(parents=True,exist_ok=False)
    telemetry=out/'telemetry'/f'run_id={run_id}'; telemetry.mkdir(parents=True)
    route, params=scenario(cfg,index,args.seed,args.step_length,raw)
    byid={p['vehicle_id']:p for p in params}
    for p in params: p['run_id']=run_id
    sumo_seed=seed_for(args.seed,'sumo',index)
    cmd=[args.sumo,'--net-file',str(ROOT/INPUTS[0]),'--route-files',str(route),'--additional-files',str(ROOT/'network/london.poly.xml'),'--step-length',str(args.step_length),'--seed',str(sumo_seed),'--step-method.ballistic','true','--xml-validation','always','--tripinfo-output',str(raw/'tripinfo.xml'),'--tripinfo-output.write-unfinished','true','--lanechange-output',str(raw/'lanechanges.xml'),'--collision-output',str(raw/'collisions.xml'),'--statistic-output',str(raw/'statistics.xml'),'--log',str(raw/'sumo.log'),'--error-log',str(raw/'warnings.log'),'--no-step-log','true','--precision','10']
    (raw/'command.json').write_text(json.dumps(cmd,indent=2))
    started=time.perf_counter(); traci.start(cmd)
    conn=traci.getConnection(); version=conn.getVersion()[1]
    if '1.28.0' not in version: conn.close(); raise RuntimeError(version)
    variables=[tc.VAR_SPEED,tc.VAR_ACCELERATION,tc.VAR_LANE_ID,tc.VAR_ROAD_ID,tc.VAR_LANE_INDEX,tc.VAR_LANEPOSITION,tc.VAR_POSITION,tc.VAR_DISTANCE,tc.VAR_WAITING_TIME,tc.VAR_NEXT_TLS]
    writer=pq.ParquetWriter(telemetry/'part-0000.parquet',TELEMETRY.remove(0),compression='zstd')
    buffer=[]; previous={}; stats=defaultdict(lambda:dict(speeds=[],exposure_s=0.,distance=0.,ratio_distance=0.,ratio_valid_distance=0.)); seen=set(); arrived=set(); teleports=Counter(); collisions=0; episodes=Episodes(args.minimum_episode_s); lane_speeds={}; actual=[]; now=0
    try:
        while conn.simulation.getMinExpectedNumber()>0 and now < args.max_time:
            conn.simulationStep(); now=conn.simulation.getTime()
            active=set(conn.vehicle.getIDList())
            arrived.update(conn.simulation.getArrivedIDList())
            teleports.update(conn.simulation.getStartingTeleportIDList()); collisions+=conn.simulation.getCollidingVehiclesNumber()
            for vid in conn.simulation.getDepartedIDList():
                seen.add(vid); conn.vehicle.subscribe(vid,variables)
                expected=byid[vid]
                observed={k:getattr(conn.vehicle,'get'+name)(vid) for k,name in [('speedFactor','SpeedFactor'),('accel','Accel'),('decel','Decel'),('tau','Tau'),('minGap','MinGap'),('sigma','Imperfection'),('actionStepLength','ActionStepLength'),('apparentDecel','ApparentDecel'),('emergencyDecel','EmergencyDecel')]}
                for key in ['lcStrategic','lcSpeedGain','lcCooperative','lcKeepRight','lcAssertive','lcOvertakeRight']:
                    observed[key]=float(conn.vehicle.getParameter(vid,'laneChangeModel.'+key))
                startup = conn.vehicle.getParameter(vid,'carFollowModel.startupDelay')
                observed['startupDelay']=float(startup) if startup else None
                for key,value in observed.items():
                    if value is not None and not math.isclose(expected[key],value,abs_tol=1e-6): raise RuntimeError(f'Parameter mismatch {vid} {key}: {expected[key]} != {value}')
                actual.append(dict(run_id=run_id,vehicle_id=vid,**observed))
            for vid in active:
                state=conn.vehicle.getSubscriptionResults(vid)
                if not state: continue
                p=byid[vid]; lane=state[tc.VAR_LANE_ID]; speed=finite(state[tc.VAR_SPEED]); acc=finite(state[tc.VAR_ACCELERATION]); pos=state[tc.VAR_POSITION]; distance=finite(state[tc.VAR_DISTANCE]); prev=previous.get(vid)
                if lane and lane not in lane_speeds: lane_speeds[lane]=conn.lane.getMaxSpeed(lane)
                limit=lane_speeds.get(lane); ratio=speed/limit if speed is not None and limit and limit>0 else None
                row=dict(run_id=run_id,vehicle_id=vid,persona=p['persona'],time_s=now,speed_mps=speed,accel_mps2=acc,jerk_mps3=(acc-prev[1])/args.step_length if prev and now-prev[0] <= args.step_length*1.01 and acc is not None and prev[1] is not None else None,lane_id=lane or None,edge_id=state[tc.VAR_ROAD_ID] or None,lane_index=state[tc.VAR_LANE_INDEX],lane_position_m=finite(state[tc.VAR_LANEPOSITION]),x_m=finite(pos[0]),y_m=finite(pos[1]),distance_m=distance,lane_max_speed_mps=limit,speed_to_lane_limit_ratio=ratio,current_waiting_time_s=finite(state[tc.VAR_WAITING_TIME]))
                leader=conn.vehicle.getLeader(vid,200.)
                if leader and leader[0] and finite(leader[1]) is not None and leader[0] in active:
                    ls=finite(conn.vehicle.getSpeed(leader[0])); gap=leader[1]
                    if ls is not None:
                        physical,headway,ttc=following(gap,p['minGap'],speed,ls)
                        row.update(leader_id=leader[0],leader_gap_sumo_m=gap,leader_gap_physical_m=physical,leader_speed_mps=ls,time_headway_s=headway,longitudinal_ttc_s=ttc)
                tls=state[tc.VAR_NEXT_TLS]
                if tls: row.update(upcoming_tls_id=tls[0][0],upcoming_tls_distance_m=finite(tls[0][2]),upcoming_tls_state=tls[0][3])
                dt=args.step_length if prev and now-prev[0] <= args.step_length*1.01 else 0.
                ds=max(0,distance-prev[2]) if dt and distance is not None and prev[2] is not None else 0.
                s=stats[vid]; s['speeds'].append(speed); s['exposure_s']+=dt; s['distance']+=ds
                if ratio is not None: s['ratio_distance']+=ratio*ds; s['ratio_valid_distance']+=ds
                conditions={'acceleration':(acc is not None and acc>=args.acceleration,acc or 0),'braking':(acc is not None and acc<=args.braking,-acc if acc else 0),'network_speed_exceedance':(ratio is not None and ratio>args.network_speed_ratio,ratio or 0),'short_headway':(row.get('time_headway_s') is not None and row['time_headway_s']<args.headway_s,1/max(row.get('time_headway_s') or 1e-9,1e-9)),'longitudinal_near_miss':(row.get('longitudinal_ttc_s') is not None and row['longitudinal_ttc_s']<args.ttc_s,1/max(row.get('longitudinal_ttc_s') or 1e-9,1e-9))}
                episodes.update(row,dt,conditions); previous[vid]=(now,acc,distance); buffer.append(row)
                if len(buffer)>=10000: writer.write_table(pa.Table.from_pylist(buffer,schema=TELEMETRY).drop_columns(['run_id'])); buffer=[]
            for vid in set(previous)-active: episodes.finish_vehicle(vid); previous.pop(vid)
    finally:
        if buffer: writer.write_table(pa.Table.from_pylist(buffer,schema=TELEMETRY).drop_columns(['run_id']))
        writer.close(); conn.close()
    events=episodes.finish()
    for e in ET.parse(raw/'lanechanges.xml').getroot().findall('change'):
        vid=e.get('id'); t=float(e.get('time'))
        events.append(dict(run_id=run_id,vehicle_id=vid,persona=byid[vid]['persona'],event_type='lane_change',lane_id=e.get('from'),edge_id=None,reason=e.get('reason'),start_s=t,end_s=t,duration_s=0.,peak_value=None))
    trips={t.get('id'):t.attrib for t in ET.parse(raw/'tripinfo.xml').getroot().findall('tripinfo')}
    summaries=[]
    for p in params:
        vid=p['vehicle_id']; t=trips.get(vid,{}); s=stats[vid]; speeds=s['speeds']; selected=[e for e in events if e['vehicle_id']==vid]
        row=dict(run_id=run_id,vehicle_id=vid,persona=p['persona'],completed=vid in arrived,departed=vid in seen,teleported=teleports[vid]>0,teleport_count=teleports[vid],status='completed' if vid in arrived else 'incomplete' if vid in seen else 'not_departed_by_cap',exposed_time_s=s['exposure_s'],exposed_km=s['distance']/1000,distance_weighted_speed_ratio=s['ratio_distance']/s['ratio_valid_distance'] if s['ratio_valid_distance'] else None)
        for key,target in [('depart','depart_s'),('arrival','arrival_s'),('duration','duration_s'),('routeLength','route_distance_m'),('waitingTime','waiting_time_s'),('timeLoss','time_loss_s'),('departDelay','depart_delay_s')]:
            value=float(t[key]) if key in t else None; row[target]=value if value is not None and value>=0 else None
        for name,quantile in [('median',.5),('p95',.95)]: row[name+'_speed_mps']=float(pa.compute.quantile(pa.array(speeds,type=pa.float64()),q=quantile)[0].as_py()) if speeds else None
        row['mean_speed_mps']=sum(speeds)/len(speeds) if speeds else None; row['max_speed_mps']=max(speeds) if speeds else None
        for kind in list(conditions)+['lane_change']:
            relevant=[e for e in selected if e['event_type']==kind]; row[kind+'_count']=len(relevant); row[kind+'_duration_s']=sum(e['duration_s'] for e in relevant); row[kind+'_per_km']=len(relevant)/row['exposed_km'] if row['exposed_km'] else None
        summaries.append(row)
    collision_records=len(ET.parse(raw/'collisions.xml').getroot().findall('collision'))
    run=dict(run_id=run_id,scenario_id=run_id,sumo_version=version,base_seed=args.seed,assignment_seed=seed_for(args.seed,'assignment'),sumo_seed=sumo_seed,step_length_s=args.step_length,expected_count=len(params),departed_count=len(seen),arrived_count=len(arrived),incomplete_count=len(params)-len(arrived),not_departed_count=len(params)-len(seen),failed_departures_count=None,teleported_count=len(teleports),teleport_event_count=sum(teleports.values()),collision_count=collision_records,colliding_vehicle_step_count=collisions,truncated=conn is not None and now>=args.max_time and len(arrived)<len(params),end_time_s=now,runtime_s=time.perf_counter()-started,os=platform.platform(),network_sha256=sha(ROOT/INPUTS[0]),route_sha256=sha(ROOT/INPUTS[1]),network_speed_status=args.network_speed_status,scenario_sha256=sha(route),config_sha256=sha(args.config))
    write_table(raw/'driver_parameters.parquet',params); write_table(raw/'observed_parameters.parquet',actual); write_table(raw/'trip_summary.parquet',summaries); write_table(raw/'driving_events.parquet',events,EVENT_SCHEMA)
    (raw/'run.json').write_text(json.dumps(run,indent=2)); print(json.dumps(run),flush=True)
    return params,summaries,events,run

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs',type=int,default=12); parser.add_argument('--seed',type=int,default=1001); parser.add_argument('--step-length',type=float,default=.5); parser.add_argument('--max-time',type=float,default=3600); parser.add_argument('--sumo',default=shutil.which('sumo')); parser.add_argument('--config',type=Path,default=ROOT/'config/driver_profiles.yaml'); parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--network-speed-status',choices=['unverified','validated','corrected'],default='unverified')
    parser.add_argument('--speed-limit-evidence',type=Path)
    for key,value in THRESHOLDS.items(): parser.add_argument('--'+key.replace('_','-'),type=float,default=value)
    args=parser.parse_args()
    if not 1<=args.runs<=12 or args.max_time<=0: parser.error('runs must be 1..12; cap positive')
    if args.network_speed_status != 'unverified' and (not args.speed_limit_evidence or not args.speed_limit_evidence.is_file()): parser.error('validated/corrected status requires a provenance evidence file')
    if args.minimum_episode_s <= 0 or args.headway_s <= 0 or args.ttc_s <= 0: parser.error('episode and time thresholds must be positive')
    cfg=load_config(args.config,args.step_length); out=args.output.resolve(); out.mkdir(parents=True,exist_ok=False)
    hashes={p:sha(ROOT/p) for p in INPUTS}; audit(out,args.network_speed_status,args.speed_limit_evidence)
    installed=Path(args.sumo).resolve().parent.parent
    schema=(installed/'data/xsd/types/route.xsd').read_text()
    for key in PARAMS+['apparentDecel','emergencyDecel']:
        if f'name="{key}"' not in schema: raise RuntimeError('Installed schema lacks '+key)
    manifest=dict(arguments={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},input_hashes=hashes,installed_schema_sha256=sha(installed/'data/xsd/types/route.xsd'),parameter_validation='installed XSD + SUMO --xml-validation always + departure TraCI getter comparison',ssm='Not enabled; longitudinal TTC is not SSM TTC/DRAC/PET',thresholds={k:getattr(args,k) for k in THRESHOLDS})
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
    all_params=[]; all_trips=[]; all_events=[]; runs=[]
    for index in range(args.runs):
        p,t,e,r=run_one(args,cfg,index,out); all_params+=p; all_trips+=t; all_events+=e; runs.append(r)
        for name,rows,schema in [('driver_parameters',all_params,None),('trip_summary',all_trips,None),('driving_events',all_events,EVENT_SCHEMA),('simulation_runs',runs,None)]: write_table(out/(name+'.parquet'),rows,schema)
    quality=dict(original_inputs_unchanged=hashes=={p:sha(ROOT/p) for p in INPUTS},tables={},warnings=['Road speed limits unverified','No SSM device output; no red-light violation inference','startupDelay getter unavailable in installed SUMO; XML accepted but runtime value unverified','Failed departures cannot be inferred as failures from vehicles pending at cap; field null','Telemetry excludes pre-departure, final arrival step and teleport gaps; authoritative TripInfo distance differs from sampled exposure'])
    for name in ['driver_parameters','trip_summary','driving_events','simulation_runs']:
        table=pq.read_table(out/(name+'.parquet')); keys=['run_id','vehicle_id'] if name in ['driver_parameters','trip_summary'] else ['run_id'] if name=='simulation_runs' else []
        records=table.select(keys).to_pylist() if keys else []
        unique=len({tuple(r.values()) for r in records})==len(records)
        if not unique: raise RuntimeError('Duplicate composite keys')
        quality['tables'][name]=dict(rows=table.num_rows,unique_keys=unique,null_counts={c:table[c].null_count for c in table.column_names})
    telemetry_rows=0; nulls=Counter()
    for file in (out/'telemetry').rglob('*.parquet'):
        table=pq.ParquetFile(file).read(); telemetry_rows+=table.num_rows; nulls.update({c:table[c].null_count for c in table.column_names})
    quality['tables']['telemetry']=dict(rows=telemetry_rows,null_counts=dict(nulls))
    quality['runs']=runs
    (out/'data_quality.json').write_text(json.dumps(quality,indent=2))
    from src.processing.data_dictionary import dictionary
    dictionary(out)
    if not quality['original_inputs_unchanged']: raise RuntimeError('Original inputs modified')

if __name__=='__main__': main()
