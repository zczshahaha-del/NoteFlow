"""Bounded local endurance check with synthetic accounts; never sends email.

Uses current server/.env through app.config. No tokens or content enter reports.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))

import httpx
from sqlalchemy import delete, func, select, text
from app.config import cfg
from app.database import AsyncSessionLocal, engine
from app.models.db import IntegrationOutbox, NoteIndexJob, User, UserSession
from app.utils import create_token


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def stats(values):
    ordered = sorted(values)
    return {'samples':len(ordered),'p50Ms':round(statistics.median(ordered),2) if ordered else None,
            'p95Ms':round(ordered[max(0,math.ceil(len(ordered)*.95)-1)],2) if ordered else None,
            'maxMs':round(max(ordered),2) if ordered else None}


class RequestFailed(RuntimeError):
    """A request failure already recorded in the report."""


class Soak:
    def __init__(self,args):
        self.args=args
        self.prefix='soak-'+uuid.uuid4().hex[:12]
        self.owners=[self.prefix+f'-{i}' for i in range(args.concurrency)]
        self.clients=[];self.fixtures=[]
        self.latencies=defaultdict(list);self.statuses=Counter();self.failures=Counter()
        self.resources=[];self.completed_cycles=0;self.writes=0;self.index_waits=[]
        self.start=time.monotonic();self.workload_start=None
        self.path=Path(args.report) if args.report else Path(tempfile.mkdtemp(prefix='noteflow-soak-'))/'report.json'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.report={'startedAt':utcnow(),'status':'preparing','sourceCommit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                     'scope':'local endurance for the configured duration; not production or maximum-capacity acceptance',
                     'plannedSeconds':args.duration_seconds,'concurrency':args.concurrency,'pauseSeconds':args.pause_seconds,
                     'testScriptSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                     'emailSent':False,'usesSyntheticSessions':True,'realEmailLoginCoveredSeparately':True,
                     'syntheticAccountsCleaned':False,'limitations':['No physical-device, production or disaster-recovery acceptance',
                       'External AI probes are bounded, not concurrent model-provider load']}
        self.write_report()

    def write_report(self):
        self.report.update({'elapsedSeconds':round(time.monotonic()-(self.workload_start or self.start),1),
            'completedCycles':self.completed_cycles,'writes':self.writes,'httpStatuses':dict(self.statuses),
            'failures':dict(self.failures),'latency':{k:stats(v) for k,v in self.latencies.items()},
            'indexReadyWaitMs':stats(self.index_waits),'resources':self.resources})
        temporary=self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.report,ensure_ascii=False,indent=2)+'\n');temporary.chmod(0o600);temporary.replace(self.path)

    async def request(self,client,label,method,path,*,expected=200,**kwargs):
        started=time.monotonic()
        try:
            response=await client.request(method,path,**kwargs)
            self.statuses[str(response.status_code)]+=1
            if response.status_code!=expected:raise RuntimeError(f'HTTP_{response.status_code}')
            return response
        except Exception as exc:
            kind=str(exc) if str(exc).startswith('HTTP_') else type(exc).__name__
            self.failures[label+':'+kind]+=1
            raise RequestFailed(label) from None
        finally:self.latencies[label].append((time.monotonic()-started)*1000)

    async def index_ready(self,client,note_id,timeout=120):
        started=time.monotonic()
        while time.monotonic()-started<timeout:
            response=await self.request(client,'index_poll','GET','/api/index-jobs',params={'noteId':note_id})
            jobs=response.json().get('jobs',[])
            if jobs and jobs[0]['status']=='success':
                self.index_waits.append((time.monotonic()-started)*1000);return
            if jobs and jobs[0]['status']=='failed':raise RuntimeError('synthetic_index_failed')
            await asyncio.sleep(1)
        raise TimeoutError('synthetic_index_timeout')

    async def prepare(self):
        now=datetime.now(timezone.utc).replace(tzinfo=None)
        sessions=[uuid.uuid4().hex for _ in self.owners]
        async with AsyncSessionLocal() as db:
            db.add_all([User(id=owner,email=owner+'@example.invalid',display_name='Synthetic Soak',email_verified_at=now) for owner in self.owners])
            await db.flush()
            db.add_all([UserSession(id=sid,user_id=owner,expires_at=now+timedelta(seconds=self.args.duration_seconds+3600)) for owner,sid in zip(self.owners,sessions)])
            await db.commit()
        for i,(owner,sid) in enumerate(zip(self.owners,sessions)):
            client=httpx.AsyncClient(base_url=self.args.base_url,timeout=30,headers={
                'Authorization':'Bearer '+create_token(owner,owner+'@example.invalid','Synthetic Soak',sid)})
            self.clients.append(client)
            marker=self.prefix+f'_note_{i}'
            body='# 合成持续运行笔记\n\n'+marker+'\n\n'+('这是隔离测试正文。\n'*80)
            note=(await self.request(client,'create_note','POST','/api/notes',json={'title':marker,'content':body,'tags':['soak-test']})).json()['note']
            attachment_body=(marker+'_attachment').encode()
            attachment=(await self.request(client,'upload_attachment','POST','/api/attachments',content=attachment_body,headers={
                'Content-Type':'application/octet-stream','X-File-Name':'soak.txt','X-Note-Id':note['id'],'X-Content-Type':'text/plain'})).json()['attachment']
            self.fixtures.append({'id':note['id'],'marker':marker,'content':body,'attachment':attachment['id'],'bytes':attachment_body})
            await self.index_ready(client,note['id'])

    async def external_probe(self,label):
        # At most one call per phase; no loop multiplies provider requests.
        response=await self.request(self.clients[0],label+'_hybrid','POST','/api/notes/search',json={'query':self.fixtures[0]['marker'],'limit':3})
        if not response.json().get('results'):raise RuntimeError('synthetic_hybrid_result_missing')
        response=await self.request(self.clients[0],label+'_agent','POST','/api/agent/chat',json={
            'question':'请只回复 SOAK_OK','mode':'chat','memoryEnabled':False,'history':[],'maxTokens':32})
        events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ') and line!='data: [DONE]']
        if not any(e.get('type')=='agent_done' and e.get('status')=='completed' for e in events):raise RuntimeError('synthetic_agent_not_completed')

    async def worker(self,index,deadline):
        client=self.clients[index];fixture=self.fixtures[index];other=self.fixtures[(index+1)%len(self.fixtures)]
        step=0;next_write=time.monotonic()+min(60,self.args.write_interval_seconds)
        while time.monotonic()<deadline:
            try:
                action=step%7
                if action==0:
                    note=(await self.request(client,'read_note','GET','/api/notes/'+fixture['id'])).json()['note']
                    if note['content']!=fixture['content']:raise AssertionError('synthetic_note_content_mismatch')
                elif action==1:
                    notes=(await self.request(client,'list_notes','GET','/api/notes')).json()['notes']
                    ids={n['id'] for n in notes}
                    if fixture['id'] not in ids or (len(self.fixtures)>1 and other['id'] in ids):raise AssertionError('synthetic_isolation_mismatch')
                elif action==2:
                    await self.request(client,'versions','GET',f"/api/notes/{fixture['id']}/versions")
                elif action==3:
                    response=await self.request(client,'literal_search','POST','/api/notes/search',json={'query':fixture['marker'],'mode':'literal','limit':8})
                    if not response.json().get('results'):raise AssertionError('synthetic_literal_result_missing')
                elif action==4:
                    response=await self.request(client,'read_attachment','GET',f"/api/attachments/{fixture['attachment']}/content")
                    if response.content!=fixture['bytes']:raise AssertionError('synthetic_attachment_mismatch')
                elif action==5:
                    await self.request(client,'list_attachments','GET',f"/api/notes/{fixture['id']}/attachments")
                elif len(self.fixtures)>1:
                    await self.request(client,'cross_user_denied','GET','/api/notes/'+other['id'],expected=404)
                if time.monotonic()>=next_write:
                    next_content=fixture['content']+'\n更新 '+str(step)
                    await self.request(client,'update_note','PUT','/api/notes/'+fixture['id'],json={
                        'content':next_content,'expectedContentHash':hashlib.sha256(fixture['content'].encode()).hexdigest()})
                    fixture['content']=next_content;self.writes+=1
                    await self.index_ready(client,fixture['id'])
                    next_write=time.monotonic()+self.args.write_interval_seconds
                self.completed_cycles+=1
            except Exception as exc:
                # request failures already counted; add failed semantic assertions only.
                if not isinstance(exc,RequestFailed):self.failures['workflow:'+type(exc).__name__]+=1
            step+=1
            await asyncio.sleep(self.args.pause_seconds)

    async def sample_resources(self):
        sample={'at':utcnow()}
        async with AsyncSessionLocal() as db:
            connections=(await db.execute(text("SELECT state,count(*) FROM pg_stat_activity WHERE datname=current_database() GROUP BY state"))).all()
            sample['databaseConnections']={str(state):count for state,count in connections}
            for name,model in [('indexJobs',NoteIndexJob),('outboxJobs',IntegrationOutbox)]:
                values=(await db.execute(select(model.status,func.count()).where(model.user_id.in_(self.owners)).group_by(model.status))).all()
                sample[name]={status:count for status,count in values}
                for bad_status in ['failed','dead']:
                    if sample[name].get(bad_status,0):self.failures[name+':'+bad_status]=max(self.failures[name+':'+bad_status],sample[name][bad_status])
        def processes():
            result={}
            found=subprocess.run(['lsof','-t','-nP',f'-iTCP:{urlparse(self.args.base_url).port or 8080}','-sTCP:LISTEN'],capture_output=True,text=True)
            pids=sorted(set(found.stdout.split()))
            if pids:
                info=subprocess.run(['ps','-p',','.join(pids),'-o','pid=,rss=,pcpu='],capture_output=True,text=True)
                result['listenerProcesses']=[{'pid':int(parts[0]),'rssMiB':round(int(parts[1])/1024,2),'cpuPercent':float(parts[2])}
                    for line in info.stdout.splitlines() if len(parts:=line.split())==3]
            docker=subprocess.run(['docker','stats','--no-stream','--format','{{json .}}','noteflow-postgres','noteflow-redis','noteflow-hindsight'],capture_output=True,text=True,timeout=15)
            result['containers']=[{k:v[k] for k in ['Name','CPUPerc','MemUsage','MemPerc','PIDs']} for line in docker.stdout.splitlines() if (v:=json.loads(line))]
            if docker.returncode:raise RuntimeError('container_stats_unavailable')
            return result
        sample.update(await asyncio.to_thread(processes));self.resources.append(sample)
        response=await self.request(self.clients[0],'health','GET','/api/health')
        if not response.json().get('ok'):self.failures['health:not_ready']+=1
        async with httpx.AsyncClient(timeout=15) as client:
            response=await self.request(client,'hindsight_health','GET','http://127.0.0.1:8888/health')

    async def cleanup(self):
        from app.providers.hindsight import HindsightProvider
        from app.agent.langgraph_readonly import postgres_checkpointer
        async with engine.connect() as db:
            rows=await db.execute(text("SELECT DISTINCT thread_id FROM checkpoints WHERE checkpoint->'channel_values'->>'user_id' = ANY(:owners)"),{'owners':self.owners})
            threads=[row[0] for row in rows]
        async with postgres_checkpointer() as saver:
            for thread in threads:await saver.adelete_thread(thread)
        for owner in self.owners:await HindsightProvider().delete_bank(owner)
        for attempt in range(5):
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(delete(IntegrationOutbox).where(IntegrationOutbox.user_id.in_(self.owners)))
                    await db.execute(delete(User).where(User.id.in_(self.owners)));await db.commit()
                    assert await db.scalar(select(func.count()).select_from(User).where(User.id.in_(self.owners)))==0
                break
            except Exception as exc:
                if getattr(getattr(exc,'orig',None),'sqlstate',None) not in {'40P01','40001'} or attempt==4:raise
                await asyncio.sleep(attempt+1)
        root=Path(cfg.ATTACHMENT_STORAGE_ROOT)
        if not root.is_absolute():root=ROOT/'server'/root
        for owner in self.owners:
            folder=root/owner
            if folder.is_dir():shutil.rmtree(folder)
        self.report['syntheticAccountsCleaned']=True
        self.report['syntheticCheckpointThreadsCleaned']=len(threads)

    async def run(self):
        tasks=[]
        try:
            await self.prepare()
            if not self.args.skip_external:await self.external_probe('start')
            self.workload_start=time.monotonic();deadline=self.workload_start+self.args.duration_seconds
            self.report['status']='running';self.report['workloadStartedAt']=utcnow();self.write_report()
            print(json.dumps({'status':'running','plannedSeconds':self.args.duration_seconds,'concurrency':len(self.clients),'report':str(self.path)}),flush=True)
            tasks=[asyncio.create_task(self.worker(i,deadline)) for i in range(len(self.clients))]
            while time.monotonic()<deadline:
                try:await self.sample_resources()
                except Exception as exc:self.failures['resource_sampling:'+type(exc).__name__]+=1
                self.write_report()
                print(json.dumps({'elapsedSeconds':self.report['elapsedSeconds'],'cycles':self.completed_cycles,'failures':sum(self.failures.values())}),flush=True)
                await asyncio.sleep(min(self.args.sample_interval_seconds,max(0,deadline-time.monotonic())))
            await asyncio.gather(*tasks);tasks=[]
            if not self.args.skip_external:await self.external_probe('end')
            for i,client in enumerate(self.clients):await self.index_ready(client,self.fixtures[i]['id'])
            await self.sample_resources()
            self.report['latencyBudgetsMs']={'health':250,'read_note':500,'list_notes':500,'literal_search':1500}
            for label,budget in self.report['latencyBudgetsMs'].items():
                if self.latencies[label] and stats(self.latencies[label])['p95Ms']>budget:self.failures['latency_budget:'+label]+=1
            self.report['loadFinishedAt']=utcnow()
            self.report['loadElapsedSeconds']=round(time.monotonic()-self.workload_start,1)
            self.report['status']='passed' if not self.failures else 'failed'
        except BaseException as exc:
            self.report['status']='incomplete';self.report['failureType']=type(exc).__name__
            raise
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            try:await self.cleanup()
            except Exception as exc:self.report['cleanupFailureType']=type(exc).__name__;self.report['status']='incomplete'
            for client in self.clients:await client.aclose()
            await engine.dispose()
            self.report['completedAt']=utcnow();self.write_report()
            print(json.dumps({'status':self.report['status'],'elapsedSeconds':self.report['elapsedSeconds'],'requests':sum(self.statuses.values()),
                 'failures':sum(self.failures.values()),'cleaned':self.report['syntheticAccountsCleaned'],'report':str(self.path)}),flush=True)
        return 0 if self.report['status']=='passed' else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8080')
    parser.add_argument('--duration-seconds',type=int,default=3600)
    parser.add_argument('--concurrency',type=int,default=3)
    parser.add_argument('--pause-seconds',type=float,default=1)
    parser.add_argument('--sample-interval-seconds',type=float,default=60)
    parser.add_argument('--write-interval-seconds',type=float,default=300)
    parser.add_argument('--skip-external',action='store_true')
    parser.add_argument('--report')
    args=parser.parse_args();url=urlparse(args.base_url)
    if url.hostname not in {'127.0.0.1','localhost'} or url.scheme not in {'http','https'} or url.username or url.password:
        parser.error('only a local loopback API is allowed')
    if cfg.ENVIRONMENT=='production' or cfg.DB_HOST not in {'127.0.0.1','localhost'}:parser.error('requires local non-production configuration')
    if not 1<=args.duration_seconds<=86400 or not 2<=args.concurrency<=8 or args.pause_seconds<.2 or args.sample_interval_seconds<1 or args.write_interval_seconds<5:
        parser.error('invalid or unbounded workload parameters')
    try:return asyncio.run(Soak(args).run())
    except KeyboardInterrupt:return 130
    except Exception as exc:print('soak incomplete: '+type(exc).__name__);return 1

if __name__=='__main__':raise SystemExit(main())
