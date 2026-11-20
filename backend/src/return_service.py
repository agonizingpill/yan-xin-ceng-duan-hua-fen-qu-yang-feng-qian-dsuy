import statistics
from sqlalchemy import select
from fastapi import HTTPException
from .models import Borehole,Stratum,Reading,MissingInterval,SampleInterval,Seal,Custody,SampleReturn
from .common import hole,get,pack,audit,now,overlaps

PROPS=('density','susceptibility','velocity')
STEP=.25

def _latest_custody(s,sample_id):
    seals=s.scalars(select(Seal).where(Seal.sample_id==sample_id).order_by(Seal.id)).all()
    for z in reversed(seals):
        c=s.scalar(select(Custody).where(Custody.seal_id==z.id))
        if c:return c

def create(s,p,u):
    ref=get(s,SampleInterval,p.sample_id);h=hole(s,ref.borehole_id);x=get(s,SampleInterval,p.sample_id,True)
    if x.state!='transferred':raise HTTPException(409,'仅化验室已签收的样袋可登记退样')
    if s.scalar(select(SampleReturn.id).where(SampleReturn.sample_id==x.id,SampleReturn.state!='cancelled')):raise HTTPException(409,'该样袋已有未办结的退单')
    c=_latest_custody(s,x.id)
    r=SampleReturn(borehole_id=h.id,sample_id=x.id,custody_id=c.id if c else None,reason=p.reason,created_by=u['email'],created_at=now())
    s.add(r);s.flush();audit(s,u,'登记化验室退样 '+x.code);return pack(r)

def _mean(rs,p):return statistics.fmean(getattr(r,p)for r in rs)
def _std(rs,p):
    v=[getattr(r,p)for r in rs]
    return statistics.pstdev(v)if len(v)>1 else 0.0

def _evaluate(s,h,r):
    # Candidate windows of the original bag's length inside the original stratum,
    # ranked by distribution distance of the three physical properties.
    orig=get(s,SampleInterval,r.sample_id);st=get(s,Stratum,orig.stratum_id)
    L=round(orig.end_m-orig.start_m,6)
    readings=s.scalars(select(Reading).where(Reading.borehole_id==h.id).order_by(Reading.depth_m)).all()
    missing=s.scalars(select(MissingInterval).where(MissingInterval.borehole_id==h.id)).all()
    active=[x for x in s.scalars(select(SampleInterval).where(SampleInterval.borehole_id==h.id,SampleInterval.state!='cancelled'))if x.destructive]
    orig_rs=[x for x in readings if orig.start_m<=x.depth_m<orig.end_m]
    if not orig_rs:raise HTTPException(409,'原袋区间缺少可比读数，无法推荐补取段')
    layer=[x for x in readings if st.start_m<=x.depth_m<st.end_m]or list(readings)
    scale={p:(statistics.pstdev([getattr(x,p)for x in layer])if len(layer)>1 else 0.0)for p in PROPS}
    scale={p:(v if v>1e-8 else 1.0)for p,v in scale.items()}
    def dist(ws):
        d=sum(abs(_mean(orig_rs,p)-_mean(ws,p))/scale[p]for p in PROPS)/3
        if len(orig_rs)>1 and len(ws)>1:d+=sum(abs(_std(orig_rs,p)-_std(ws,p))/scale[p]for p in PROPS)/3
        return d
    quota_left=h.cut_quota-sum(x.end_m-x.start_m for x in active)
    windows=[];a=st.start_m
    while a<=st.end_m-L+1e-8:
        a=round(a,6);b=round(a+L,6);ws=[x for x in readings if a<=x.depth_m<b]
        miss=any(overlaps(a,b,m.start_m,m.end_m)for m in missing)
        occ=[x.code for x in active if overlaps(a,b,x.start_m,x.end_m)]
        reason='原袋区间'if overlaps(a,b,orig.start_m,orig.end_m)else('缺芯'if miss else('缺少可比读数'if not ws else''))
        windows.append({'start_m':a,'end_m':b,'readings':len(ws),'occupied':occ,'recommendable':not reason,'reason':reason,'distance':round(dist(ws),6)if not reason else None})
        a+=STEP
    ranked=sorted([w for w in windows if w['recommendable']],key=lambda w:(w['distance'],w['start_m']))
    return orig,L,quota_left,windows,ranked,scale,orig_rs

def candidates(s,id):
    r=get(s,SampleReturn,id);h=get(s,Borehole,r.borehole_id)
    orig,L,quota_left,windows,ranked,scale,orig_rs=_evaluate(s,h,r)
    ordered=ranked+sorted((w for w in windows if not w['recommendable']),key=lambda w:w['start_m'])
    return {'return':pack(r),'original':{'code':orig.code,'start_m':orig.start_m,'end_m':orig.end_m,'readings':len(orig_rs)},'length_m':L,'quota_left':round(quota_left,6),'candidates':ordered}

def resample(s,id,p,u):
    ref=get(s,SampleReturn,id);h=hole(s,ref.borehole_id);r=get(s,SampleReturn,id,True)
    if r.state=='fulfilled':return pack(get(s,SampleInterval,r.replacement_id))
    if r.state!='pending':raise HTTPException(409,'退单已撤销，补取未执行')
    orig,L,quota_left,windows,ranked,scale,orig_rs=_evaluate(s,h,r)
    if quota_left+1e-8<L:raise HTTPException(409,'切样配额不足，退单留待补取；原袋已耗额度照计')
    if p.start_m is not None:
        chosen=next((w for w in windows if abs(w['start_m']-p.start_m)<1e-6),None)
        if not chosen:raise HTTPException(409,'所选起点不在原层候选段内')
        if not chosen['recommendable']:raise HTTPException(409,'所选段'+chosen['reason']+'，不推荐补取')
        if chosen['occupied']:raise HTTPException(409,'备选段被占，退单留待补取')
    else:
        chosen=next((w for w in ranked if not w['occupied']),None)
        if not chosen:raise HTTPException(409,'候选段被占或缺少可比读数，退单留待补取')
    code=orig.code+'-BX'+str(r.id)
    x=s.scalar(select(SampleInterval).where(SampleInterval.code==code))
    if not x:
        x=SampleInterval(borehole_id=h.id,stratum_id=orig.stratum_id,code=code,start_m=chosen['start_m'],end_m=chosen['end_m'],purpose='化验室退样原层等长补取 / 原袋 '+orig.code,destructive=True,state='planned')
        s.add(x);s.flush()
    ws=[z for z in s.scalars(select(Reading).where(Reading.borehole_id==h.id,Reading.depth_m>=chosen['start_m'],Reading.depth_m<chosen['end_m']))]
    r.replacement_id=x.id;r.state='fulfilled';r.closed_at=now()
    r.evidence={'original':{'code':orig.code,'start_m':orig.start_m,'end_m':orig.end_m,'readings':len(orig_rs)},'chosen':{'start_m':chosen['start_m'],'end_m':chosen['end_m'],'readings':len(ws)},'distance':chosen['distance'],'components':{p:round(abs(_mean(orig_rs,p)-_mean(ws,p))/scale[p],6)for p in PROPS},'scale':{p:round(v,6)for p,v in scale.items()},'quota_left_before':round(quota_left,6),'candidates_evaluated':len(windows),'recommendable':len(ranked)}
    audit(s,u,'退单补取 '+orig.code+' → '+x.code);return pack(x)

def cancel(s,id,u):
    ref=get(s,SampleReturn,id);hole(s,ref.borehole_id);r=get(s,SampleReturn,id,True)
    if r.state!='pending':raise HTTPException(409,'退单已补取或已撤销，以先办成为准')
    r.state='cancelled';r.closed_at=now();audit(s,u,'撤销退单 #'+str(r.id));return pack(r)
