from pathlib import Path
import copy
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
import pyarrow as pa
import pytest
from src.simulation.run_experiment import ROOT, PARAMS, TELEMETRY, Episodes, following, load_config, scenario, seed_for

@pytest.fixture
def cfg(): return load_config(ROOT/'config/driver_profiles.yaml',.5)

def test_validation(cfg,tmp_path):
    import yaml
    cfg['profiles']['baseline']['tau']=[.1,.2]
    path=tmp_path/'bad.yaml'; path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError): load_config(path,.5)

def test_assignment_sampling_xml(cfg,tmp_path):
    original=ET.parse(ROOT/'routes/london.rou.xml').getroot()
    all_personas=defaultdict(set)
    for run in range(12):
        folder=tmp_path/str(run); folder.mkdir()
        path,rows=scenario(cfg,run,1001,.5,folder)
        assert Counter(r['persona'] for r in rows)=={p:25 for p in cfg['profiles']}
        assert len({r['vehicle_id'] for r in rows})==300
        generated=ET.parse(path).getroot()
        for old,new,row in zip(original.findall('vehicle'),generated.findall('vehicle'),rows):
            all_personas[row['vehicle_id']].add(row['persona'])
            assert old.get('id')==new.get('id') and old.get('depart')==new.get('depart')
            assert [ET.tostring(e) for e in old]==[ET.tostring(e) for e in new]
            assert generated.find("vType[@id='%s']"%new.get('type')) is not None
            assert float(new.get('speedFactor'))==row['speedFactor']
            assert row['tau']>=row['actionStepLength']
            assert row['apparentDecel']==row['decel'] and row['emergencyDecel']==9
            for key,(lo,hi) in (cfg['default_ranges']|cfg['profiles'][row['persona']]).items(): assert lo<=row[key]<=hi
        _,repeat=scenario(cfg,run,1001,.5,folder)
        assert rows==repeat
    assert all(len(v)==12 for v in all_personas.values())

def test_individual_variation(cfg,tmp_path):
    _,rows=scenario(cfg,0,1001,.5,tmp_path)
    assert len({r['speedFactor'] for r in rows})==300
    assert seed_for(1001,'sumo',0)!=seed_for(1001,'assignment')

def test_following():
    assert following(8,2,10,5)==(10,1,2)
    assert following(8,2,0,5)==(10,None,None)
    assert following(8,2,5,5)==(10,2,None)

def test_episodes():
    e=Episodes(1)
    row=dict(run_id='r',vehicle_id='v',persona='p',time_s=.5)
    e.update(row,.5,{'braking':(True,5)})
    row['time_s']=1.; e.update(row,.5,{'braking':(True,6)})
    row['time_s']=1.5; e.update(row,.5,{'braking':(False,0)})
    row['time_s']=2.; e.update(row,.5,{'braking':(True,7)})
    rows=e.finish()
    assert len(rows)==1 and rows[0]['duration_s']==1 and rows[0]['peak_value']==6

def test_arrow_nulls():
    table=pa.Table.from_pylist([dict(run_id='r',vehicle_id='v')],schema=TELEMETRY)
    assert table['longitudinal_ttc_s'].null_count==1
    assert table.schema.field('lane_index').type==pa.int32()

def test_actual_smoke_outputs():
    from src.validation.validate_outputs import validate
    folder=ROOT/'data/experiments/smoke_verified'
    if not folder.exists(): pytest.skip('Smoke experiment has not run')
    result=validate(folder)
    assert result['assignment_rows']==300
    assert result['telemetry_rows']>0 and result['jerk_values_checked']>0

def test_named_route_and_children(cfg,tmp_path,monkeypatch):
    import src.simulation.run_experiment as module
    (tmp_path/'routes').mkdir()
    root=ET.Element('routes'); ET.SubElement(root,'vType',id='original'); ET.SubElement(root,'route',id='shared',edges='a b')
    for i in range(300):
        v=ET.SubElement(root,'vehicle',id=str(i),depart=str(i),route='shared',type='original')
        ET.SubElement(v,'param',key='research',value='preserve')
    ET.ElementTree(root).write(tmp_path/'routes/london.rou.xml')
    monkeypatch.setattr(module,'ROOT',tmp_path)
    path,rows=scenario(cfg,0,1001,.5,tmp_path)
    generated=ET.parse(path).getroot()
    assert generated.find("route[@id='shared']").get('edges')=='a b'
    assert all(v.get('route')=='shared' and v.find('param').get('value')=='preserve' for v in generated.findall('vehicle'))
    assert all(row['original_type']=='original' and row['route_id']=='shared' for row in rows)
