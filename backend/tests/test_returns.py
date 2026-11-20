import pytest
from sqlalchemy import select
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.database import Base,engine,Session
from src.models import User,Borehole,CoreRun,Reading,MissingInterval,Interpretation,Stratum,SampleInterval,Seal,Custody,SampleReturn
from src.security import password_hash,token_for
from src.routes import router

app=FastAPI();app.include_router(router)
REASON='化验室开袋发现岩芯受潮散碎，无法完成试验'

class Ctx:
    pass

@pytest.fixture()
def ctx():
    Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    c=Ctx()
    with Session.begin()as s:
        for role in['operator','supervisor','viewer']:
            s.add(User(email=role+'@t.local',password=password_hash('x'),role=role))
        h=Borehole(code='BH-T',site='测试场地',total_depth=36,cut_quota=6);s.add(h);s.flush()
        run=CoreRun(borehole_id=h.id,code='R01',start_m=0,end_m=36,recovered_m=36);s.add(run);s.flush()
        for i in range(36):
            d=i+.5
            if d in(12.5,15.5,16.5,17.5):continue
            s.add(Reading(borehole_id=h.id,run_id=run.id,depth_m=d,density=2.1 if d in(3.5,5.5)else 2.0,susceptibility=10,velocity=2000))
        s.add(MissingInterval(borehole_id=h.id,start_m=12,end_m=13,reason=''))
        it=Interpretation(borehole_id=h.id,state='confirmed');s.add(it);s.flush()
        st=Stratum(interpretation_id=it.id,start_m=0,end_m=36,lithology='砂岩',contact_note='测试层段连续覆盖全孔');s.add(st);s.flush()
        def bag(code,a,b,state):
            x=SampleInterval(borehole_id=h.id,stratum_id=st.id,code=code,start_m=a,end_m=b,purpose='测试取样',destructive=True,state=state);s.add(x);s.flush()
            if state=='transferred':
                z=Seal(sample_id=x.id,code='SL-'+code,state='intact',reason='',sealed_by='operator@t.local',snapshot={});s.add(z);s.flush()
                s.add(Custody(seal_id=z.id,receiver='周岩',destination='砾衡岩土试验室',condition='双方核对样号铅封完整',at='2026-11-18T09:30:00+00:00'))
            return x
        c.a=bag('CS-A',3,4,'transferred');c.b=bag('CS-B',8,9,'planned');c.c=bag('CS-C',20,21,'transferred')
        c.hole_id=h.id;c.stratum_id=st.id
    with Session()as s:
        c.tokens={r:token_for(s.scalar(select(User).where(User.email==r+'@t.local')))for r in['operator','supervisor','viewer']}
    c.client=TestClient(app)
    return c

def hdr(c,role='operator'):return{'Authorization':'Bearer '+c.tokens[role]}
def new_return(c,sample,role='operator'):return c.client.post('/returns',json={'sample_id':sample,'reason':REASON},headers=hdr(c,role))
def make_return(c,sample):
    r=new_return(c,sample);assert r.status_code==200,r.text
    return r.json()['id']
def get_return(c,rid):
    return next(t for t in c.client.get('/register',headers=hdr(c)).json()['returns']if t['id']==rid)

def test_return_requires_transferred(ctx):
    r=new_return(ctx,ctx.b.id)
    assert r.status_code==409 and'已签收'in r.json()['detail']

def test_return_create_and_unique(ctx):
    rid=make_return(ctx,ctx.a.id)
    t=get_return(ctx,rid)
    assert t['state']=='pending'and t['custody_id']and t['reason']==REASON
    assert new_return(ctx,ctx.a.id).status_code==409

def test_candidates_ranking_and_exclusions(ctx):
    rid=make_return(ctx,ctx.a.id)
    d=ctx.client.get(f'/returns/{rid}/candidates',headers=hdr(ctx)).json()
    assert d['length_m']==1 and d['original']['code']=='CS-A'and d['original']['readings']==1
    best=d['candidates'][0]
    assert best['recommendable']and best['start_m']==4.75 and best['distance']==0
    by={round(w['start_m'],2):w for w in d['candidates']}
    assert by[12.0]['reason']=='缺芯'and not by[12.0]['recommendable']
    assert by[15.5]['reason']=='缺少可比读数'and not by[15.5]['recommendable']
    assert by[3.0]['reason']=='原袋区间'and not by[3.0]['recommendable']
    assert by[8.0]['recommendable']and by[8.0]['occupied']==['CS-B']

def test_resample_closest_equal_length_same_stratum(ctx):
    rid=make_return(ctx,ctx.a.id)
    r=ctx.client.post(f'/returns/{rid}/resample',json={},headers=hdr(ctx))
    assert r.status_code==200,r.text
    x=r.json()
    assert(x['start_m'],x['end_m'])==(4.75,5.75)and x['end_m']-x['start_m']==1
    assert x['stratum_id']==ctx.stratum_id and x['state']=='planned'and x['destructive']
    t=get_return(ctx,rid)
    assert t['state']=='fulfilled'and t['replacement_id']==x['id']
    ev=t['evidence']
    assert ev['chosen']['start_m']==4.75 and ev['original']['code']=='CS-A'
    assert set(ev['components'])=={'density','susceptibility','velocity'}

def test_resample_retry_no_duplicate_no_double_quota(ctx):
    rid=make_return(ctx,ctx.a.id)
    x=ctx.client.post(f'/returns/{rid}/resample',json={},headers=hdr(ctx)).json()
    again=ctx.client.post(f'/returns/{rid}/resample',json={},headers=hdr(ctx))
    assert again.status_code==200 and again.json()['id']==x['id']
    reg=ctx.client.get('/register',headers=hdr(ctx)).json()
    assert len(reg['samples'])==4
    used=sum(v['end_m']-v['start_m']for v in reg['samples']if v['state']!='cancelled'and v['destructive'])
    assert abs(used-4)<1e-8

def test_cancel_after_fulfilled_rejected(ctx):
    rid=make_return(ctx,ctx.a.id)
    ctx.client.post(f'/returns/{rid}/resample',json={},headers=hdr(ctx))
    assert ctx.client.post(f'/returns/{rid}/cancel',headers=hdr(ctx)).status_code==409

def test_resample_after_cancel_rejected(ctx):
    rid=make_return(ctx,ctx.c.id)
    assert ctx.client.post(f'/returns/{rid}/cancel',headers=hdr(ctx)).status_code==200
    assert ctx.client.post(f'/returns/{rid}/resample',json={},headers=hdr(ctx)).status_code==409

def test_recreate_return_after_cancel(ctx):
    rid=make_return(ctx,ctx.c.id)
    ctx.client.post(f'/returns/{rid}/cancel',headers=hdr(ctx))
    assert new_return(ctx,ctx.c.id).status_code==200

def test_quota_insufficient_stays_pending(ctx):
    with Session.begin()as s:
        h=s.get(Borehole,ctx.hole_id);h.cut_quota=3
    rid=make_return(ctx,ctx.a.id)
    r=ctx.client.post(f'/returns/{rid}/resample',json={},headers=hdr(ctx))
    assert r.status_code==409 and'配额不足'in r.json()['detail']
    assert get_return(ctx,rid)['state']=='pending'

def test_occupied_pick_stays_pending_then_auto(ctx):
    rid=make_return(ctx,ctx.a.id)
    r=ctx.client.post(f'/returns/{rid}/resample',json={'start_m':8.0},headers=hdr(ctx))
    assert r.status_code==409 and'被占'in r.json()['detail']
    assert get_return(ctx,rid)['state']=='pending'
    ok=ctx.client.post(f'/returns/{rid}/resample',json={},headers=hdr(ctx))
    assert ok.status_code==200 and ok.json()['start_m']==4.75

def test_missing_or_no_reading_pick_rejected(ctx):
    rid=make_return(ctx,ctx.a.id)
    r1=ctx.client.post(f'/returns/{rid}/resample',json={'start_m':12.0},headers=hdr(ctx))
    r2=ctx.client.post(f'/returns/{rid}/resample',json={'start_m':15.5},headers=hdr(ctx))
    assert r1.status_code==409 and'缺芯'in r1.json()['detail']
    assert r2.status_code==409 and'缺少可比读数'in r2.json()['detail']
    assert get_return(ctx,rid)['state']=='pending'

def test_viewer_readonly(ctx):
    rid=make_return(ctx,ctx.a.id)
    assert ctx.client.post(f'/returns/{rid}/cancel',headers=hdr(ctx,'viewer')).status_code==403
    assert ctx.client.post(f'/returns/{rid}/resample',json={},headers=hdr(ctx,'viewer')).status_code==403
    assert ctx.client.get(f'/returns/{rid}/candidates',headers=hdr(ctx,'viewer')).status_code==200
