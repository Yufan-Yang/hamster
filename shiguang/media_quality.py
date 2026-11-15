"""Review playback anchors against current media; preserve source metadata verbatim."""
import json
import math
from pathlib import Path
import time

from . import board, core, steward
from .core import q

INTERVAL = 6 * 3600


def numeric(value):
    return type(value) in (int,float) and math.isfinite(value) and value >= 0


def files(row):
    return [p for p in steward._object(row['files'],[]) if isinstance(p,str) and Path(p).suffix.lower() in core.VIDEO_EXT | core.AUDIO_EXT]


def durations(row):
    result,stamps = {},[]
    for part,path in enumerate(files(row)):
        try:
            stamp=steward._stamp(path)
            stat=Path(path).stat()
            probe=q('SELECT duration FROM probes WHERE path=? AND mtime=?',(path,stat.st_mtime),one=True)
            value=probe['duration'] if probe else None
            result[part]=float(value) if numeric(value) and value>0 else None
            stamps.append([stamp,result[part]])
        except (OSError,ValueError,RuntimeError):
            result[part]=None;stamps.append([path,None])
    return result,stamps


def review(row, times=None):
    analysis=steward._object(row['analysis'],{})
    bounds,stamps=durations(row)
    result={'chapters':{},'markers':{},'times':[],'unavailable':{},'heat':{},'rejected':0,'merged_duplicates':0,'unverified_parts':[],'_source_stamps':stamps}
    for part,value in bounds.items():
        if value is None:result['unverified_parts'].append(part)
    def valid(t,part):
        return numeric(t) and part in bounds and (bounds[part] is None or t<bounds[part])
    for field in ('chapters','markers'):
        mapping=analysis.get(field)
        if not isinstance(mapping,dict):continue
        for key,entries in mapping.items():
            if not isinstance(entries,list):continue
            part=int(key) if isinstance(key,str) and key.isascii() and key.isdecimal() and len(key)<8 else -1
            accepted=[]
            for entry in entries[:1000]:
                if not isinstance(entry,dict) or not isinstance(entry.get('title'),str):
                    result['rejected']+=1;continue
                t=entry.get('t')
                if valid(t,part):
                    accepted.append({'t':t,'title':entry['title'][:500]})
                else:
                    result['rejected']+=1
                    result['unavailable'].setdefault(str(part),[]).append({'title':entry['title'][:500],'reason':'时间超出视频范围或格式无效'})
            accepted.sort(key=lambda entry:entry['t'])
            merged=[]
            for entry in accepted:
                if field=='chapters' and merged and entry['t']==merged[-1]['t']:
                    if entry['title']!=merged[-1]['title']:merged[-1]['title']+=' / '+entry['title']
                    result['merged_duplicates']+=1
                else:merged.append(entry)
            if part>=0:result[field][str(part)]=merged
    raw=times if times is not None else analysis.get('point_times',[])
    raw=raw if isinstance(raw,list) else []
    for entry in raw[:1000]:
        if entry is None:
            result['times'].append(None);continue
        part,t=(entry.get('part'),entry.get('t')) if isinstance(entry,dict) else (0,entry)
        if type(part) is int and valid(t,part):
            result['times'].append({'part':part,'t':t})
        else:
            result['times'].append(None);result['rejected']+=1
    # Heat may be absent or malformed; never derive executable coordinates from arbitrary JSON.
    heat=analysis.get('heat',{})
    for key,item in (heat.items() if isinstance(heat,dict) else []):
        if not isinstance(item,dict) or not numeric(item.get('step')) or not item['step']:continue
        values=item.get('values')
        if isinstance(values,list) and 0<len(values)<=10000 and all(numeric(v) for v in values):
            result['heat'][str(key)]={'step':item['step'],'values':[min(v,100) for v in values]}
    revision=steward.digest([row['owner'],row['files'],stamps,analysis.get('chapters'),analysis.get('markers'),raw])
    result['quality']={'revision':revision,'rejected':result['rejected'],'merged_duplicates':result['merged_duplicates'],'unverified_parts':result['unverified_parts'],
                       'status':'needs_attention' if result['rejected'] else 'unverified' if not bounds or result['unverified_parts'] else 'passed'}
    return result


def observe(row):
    inspected_at=time.time()
    report=review(row)
    with board.mutation() as db:
        current=db.execute('SELECT owner,files,analysis,status FROM jobs WHERE id=?',(row['id'],)).fetchone()
        if not current or any(current[k]!=row[k] for k in current.keys()):return
        db.execute('INSERT INTO media_quality(job_id,owner,revision,report,checked) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(job_id) DO UPDATE SET owner=excluded.owner,revision=excluded.revision,report=excluded.report,checked=excluded.checked '
                   'WHERE media_quality.checked<=excluded.checked',
                   (row['id'],row['owner'],report['quality']['revision'],json.dumps(report['quality']),inspected_at))
    return report['quality']


@board.task('media_quality_scan','播放时间轴质检','steward')
def scan(task,beat):
    owner=task['payload']['owner']
    rows=q("SELECT j.* FROM jobs j LEFT JOIN media_quality v ON v.job_id=j.id WHERE j.owner=? AND j.status='done' "
           'ORDER BY COALESCE(v.checked,0),j.id LIMIT ?',(owner,steward.BATCH))
    rejected=0
    for row in rows:
        beat({'checked':row['id']})
        result=observe(row)
        rejected+=(result or {}).get('rejected',0)
    return {'checked':len(rows),'invalid_anchors':rejected}


def tick():
    for row in q("SELECT DISTINCT j.owner FROM jobs j LEFT JOIN media_quality v ON v.job_id=j.id WHERE j.owner IS NOT NULL AND j.status='done' "
                 'AND (v.checked IS NULL OR v.checked<? OR j.updated>v.checked)',(time.time()-INTERVAL,)):
        board.publish('media_quality_scan','quality:'+row['owner'],4,force=True,payload={'owner':row['owner']})


def summary(cond,args):
    rows=q('SELECT v.report FROM media_quality v JOIN jobs j ON j.id=v.job_id AND j.owner=v.owner WHERE '+cond,args)
    reports=[steward._object(row['report'],{}) for row in rows]
    return {'checked':len(reports),'invalid_anchors':sum(r.get('rejected',0) for r in reports),
            'merged_duplicates':sum(r.get('merged_duplicates',0) for r in reports),
            'unverified':sum(r.get('status')=='unverified' for r in reports)}
