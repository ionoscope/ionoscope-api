"""Run three sibling services and verify HTTP contracts with isolated test data.

Synthetic fixture values verify software integration, not forecast quality.
No working datasets, model artifacts or production cache are changed.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import os
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator, FormatChecker


class ContainerProcess:
    def __init__(self, command, name):
        self.name = name
        subprocess.run(command, check=True, capture_output=True, text=True)

    def poll(self):
        result = subprocess.run(['docker','inspect','--format','{{.State.Running}}',self.name], capture_output=True,text=True)
        return None if result.stdout.strip() == 'true' else 1

    def terminate(self):
        subprocess.run(['docker','stop','--time','2',self.name],capture_output=True,check=False)

    def wait(self, timeout=None): return 0

    def kill(self): subprocess.run(['docker','kill',self.name],capture_output=True,check=False)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', help='Optional output JSON report')
    parser.add_argument('--docker', action='store_true', help='Use prebuilt ionoscope-api-check-* images instead of local Python processes')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    results = []
    processes, logs = [], []
    network = 'ionoscope-check-' + str(os.getpid())
    container_names = []
    documents, covered = {}, set()
    with tempfile.TemporaryDirectory(prefix='ionoscope-api-check-') as directory:
        work = Path(directory)
        features = work/'features/stations'
        features.mkdir(parents=True)
        cache = work/'cache'
        path = features/'TR169_features.csv'
        now = datetime.now(UTC)
        boundary = now.replace(minute=(now.minute//15)*15, second=0, microsecond=0)
        start = boundary-timedelta(minutes=15)
        def write_features(value=4.05, historical=False):
            timestamp = datetime(2025, 1, 1, tzinfo=UTC) if historical else start
            temporary = path.with_suffix('.tmp')
            with temporary.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['time_utc','foF2','foF2_state','foF2_source_timestamp','foF2_target_15min'])
                writer.writeheader()
                writer.writerow({'time_utc': timestamp.isoformat(), 'foF2': value, 'foF2_state': value,
                    'foF2_source_timestamp': (timestamp+timedelta(minutes=10)).isoformat() if value != '' else '',
                    'foF2_target_15min': 999})
            temporary.replace(path)
        write_features()
        ports = [free_port() for _ in range(3)]
        while len(set(ports)) != 3: ports = [free_port() for _ in range(3)]
        data_url, ml_url, api_url = [f'http://127.0.0.1:{port}' for port in ports]
        gateway_port = free_port() if args.docker else None
        if args.docker:
            api_url = f'http://127.0.0.1:{gateway_port}'
        env = os.environ.copy()
        env.update(IONOSCOPE_FEATURES_DIR=str(work/'features'), IONOSCOPE_DATA_WORKSPACE='',
                   IONOSCOPE_UPDATE_TASK_DIR=str(work/'tasks'),
                   IONOSCOPE_DATA_API_URL=data_url, IONOSCOPE_ML_SERVICE_URL=ml_url,
                   IONOSCOPE_FORECAST_CACHE_DIR=str(cache), IONOSCOPE_REQUEST_TIMEOUT_SECONDS='5')
        data_code = ('from pathlib import Path; from api import update_tasks; '
                     f'update_tasks.TASK_DIR=Path({str(work / "tasks")!r}); '
                     f'import uvicorn; uvicorn.run("api.main:app",host="127.0.0.1",port={ports[0]})')
        specs = [('ionoscope-data', [sys.executable,'-c',data_code], data_url),
                 ('ionoscope-ml-service', [sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(ports[1])], ml_url),
                 ('ionoscope-api', [sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(ports[2])], api_url)]
        try:
            if args.docker:
                subprocess.run(['docker','network','create',network],check=True,capture_output=True)
            def launch(index):
                name, command, url = specs[index]
                if args.docker:
                    alias = ['data','ml','api'][index]
                    container_name = network+'-'+alias
                    if container_name not in container_names: container_names.append(container_name)
                    cmd = ['docker','run','--detach','--name',container_name,'--network',network,
                           '--network-alias',alias,'--publish',f'127.0.0.1:{ports[index]}:{[8001,8002,8000][index]}']
                    if index == 0:
                        (work/'tasks').mkdir(exist_ok=True)
                        cmd += ['--mount',f'type=bind,source={work / "features"},target=/features,readonly',
                                '--mount',f'type=bind,source={work / "tasks"},target=/tasks',
                                '--env','IONOSCOPE_FEATURES_DIR=/features','--env','IONOSCOPE_UPDATE_TASK_DIR=/tasks']
                    elif index == 1:
                        cmd += ['--mount',f'type=bind,source={root / "ionoscope-ml-service/production_models"},target=/app/production_models,readonly']
                    else:
                        cache.mkdir(exist_ok=True)
                        cmd += ['--mount',f'type=bind,source={cache},target=/cache',
                                '--env','IONOSCOPE_DATA_API_URL=http://data:8001',
                                '--env','IONOSCOPE_ML_SERVICE_URL=http://ml:8002',
                                '--env','IONOSCOPE_FORECAST_CACHE_DIR=/cache','--env','IONOSCOPE_REQUEST_TIMEOUT_SECONDS=5']
                    cmd += ['ionoscope-api-check-'+['data-api','ml-service','api'][index]]
                    return ContainerProcess(cmd,container_name)
                log = (work/(name+'.log')).open('a',encoding='utf-8'); logs.append(log)
                return subprocess.Popen(command,cwd=root/name,env=env,stdout=log,stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            for name, command, url in specs:
                processes.append(launch(len(processes)))
            if args.docker:
                gateway_name=network+'-gateway'
                container_names.append(gateway_name)
                processes.append(ContainerProcess(['docker','run','--detach','--name',gateway_name,
                    '--network',network,'--publish',f'127.0.0.1:{gateway_port}:80',
                    '--mount',f'type=bind,source={root / "ionoscope-infrastructure/nginx/ionoscope.conf"},target=/etc/nginx/conf.d/default.conf,readonly',
                    'nginx:1.27-alpine'],gateway_name))
            with httpx.Client(timeout=30, trust_env=False) as client:
                for name, _, url in specs:
                    for attempt in range(120):
                        try:
                            if client.get(url+'/health',timeout=1).status_code == 200: break
                        except httpx.HTTPError: pass
                        if any(p.poll() is not None for p in processes): raise RuntimeError('A service exited during startup')
                        time.sleep(.5)
                    else: raise RuntimeError(f'{name} failed to start')
                def check(name, method, url, expected=200, **kwargs):
                    response = client.request(method,url,**kwargs)
                    assert response.status_code == expected, (name,response.status_code,response.text)
                    parsed = httpx.URL(url)
                    base = str(parsed.copy_with(path='/',query=None)).rstrip('/')
                    document = documents.get(base)
                    matched = None
                    payload = response.json()
                    if document:
                        for template, operations in document['paths'].items():
                            if re.fullmatch(re.sub(r'\{[^{}]+\}', '[^/]+', template),parsed.path) and method.lower() in operations:
                                matched = template
                                operation = operations[method.lower()]
                                schema = operation.get('responses',{}).get(str(expected),{}).get('content',{}).get('application/json',{}).get('schema')
                                if schema:
                                    Draft202012Validator({'components':document.get('components',{}),**schema},
                                        format_checker=FormatChecker()).validate(payload)
                                covered.add((base,method.lower(),template))
                                break
                    results.append({'check':name,'status':response.status_code,'passed':True,'route':matched})
                    return payload
                for name, _, url in specs:
                    documents[url] = client.get(url+'/openapi.json').json()
                    check(name+' health','GET',url+'/health')
                    check(name+' OpenAPI','GET',url+'/openapi.json')
                    for page in ['/docs','/redoc']:
                        response=client.get(url+page)
                        assert response.status_code==200 and 'html' in response.headers['content-type']
                        results.append({'check':name+page,'status':200,'passed':True})
                stations = check('station list','GET',api_url+'/api/v1/forecast/stations')
                assert any(s['code']=='TR169' and s['data_available'] for s in stations['stations'])
                check('data station list','GET',data_url+'/api/v1/stations')
                check('station detail','GET',data_url+'/api/v1/stations/tr169')
                check('unknown station detail','GET',data_url+'/api/v1/stations/UNKNOWN',404)
                latest = check('latest completed features','GET',data_url+'/api/v1/features/tr169/latest')
                assert latest['features']['foF2'] == 4.05 and 'foF2_target_15min' not in latest['features']
                info = check('model metadata','GET',ml_url+'/api/v1/model-info')
                assert info['artifact_loaded'], 'This smoke check requires the real production artifact'
                prediction = check('real model inference','POST',ml_url+'/api/v1/predict',json={'station':'tr169','features':latest['features']})
                assert math.isfinite(prediction['prediction']) and prediction['model'] != 'fallback_persistence'
                check('invalid ML request','POST',ml_url+'/api/v1/predict',422,json={'station':'TR169','features':{}})
                check('invalid forecast request','POST',api_url+'/api/v1/forecast',422,json={'station':'../bad'})
                check('missing forecast body','POST',api_url+'/api/v1/forecast',422,json={})
                check('malformed JSON body','POST',api_url+'/api/v1/forecast',422,content='{broken',headers={'Content-Type':'application/json'})
                check('invalid status station','GET',api_url+'/api/v1/forecast/status/bad%20station',422)
                check('unknown station','POST',api_url+'/api/v1/forecast',404,json={'station':'UNKNOWN'})
                check('feature range with UTC normalization','GET',data_url+'/api/v1/features/TR169',params={'start':start.replace(tzinfo=None).isoformat(),'end':start.isoformat()})
                check('data availability','GET',data_url+'/api/v1/availability/TR169',params={'start':start.isoformat(),'end':start.isoformat()})
                check('unknown station range','GET',data_url+'/api/v1/features/UNKNOWN',404,params={'start':start.isoformat(),'end':start.isoformat()})
                check('unknown station availability','GET',data_url+'/api/v1/availability/UNKNOWN',404,params={'start':start.isoformat(),'end':start.isoformat()})
                check('missing range parameter','GET',data_url+'/api/v1/features/TR169',422)
                check('invalid range timestamp','GET',data_url+'/api/v1/features/TR169',422,params={'start':'bad','end':start.isoformat()})
                check('reversed range','GET',data_url+'/api/v1/features/TR169',400,params={'start':start.isoformat(),'end':(start-timedelta(days=1)).isoformat()})
                check('unavailable range','GET',data_url+'/api/v1/features/TR169',409,params={'start':(start-timedelta(days=1)).isoformat(),'end':start.isoformat()})
                unavailable=check('unavailable coverage','GET',data_url+'/api/v1/availability/TR169',params={'start':(start-timedelta(days=1)).isoformat(),'end':start.isoformat()})
                assert not unavailable['available'] and unavailable['update_required']
                missing = check('missing cache','GET',api_url+'/api/v1/forecast/status/TR169')
                assert missing['status']=='missing'
                forecast = check('forecast refresh','POST',api_url+'/api/v1/forecast/refresh',json={'station':'TR169'})
                assert forecast['forecast_interval_start']==boundary.isoformat().replace('+00:00','Z')
                assert forecast['forecast_timestamp']==(boundary+timedelta(minutes=15)).isoformat().replace('+00:00','Z')
                cached = check('cached forecast','POST',api_url+'/api/v1/forecast',json={'station':'TR169'})
                assert cached['cached'] and cached['prediction']==forecast['prediction']
                status = check('fresh cache status','GET',api_url+'/api/v1/forecast/status/TR169')
                assert status['is_usable'] and status['status']=='fresh'
                with ThreadPoolExecutor(max_workers=8) as pool:
                    responses=list(pool.map(lambda _:client.post(api_url+'/api/v1/forecast',json={'station':'TR169'}),range(40)))
                assert all(r.status_code==200 and r.json()['cached'] and r.json()['prediction']==forecast['prediction'] for r in responses)
                results.append({'check':'parallel cache reads','requests':40,'concurrency':8,'passed':True})
                before=(cache/'TR169.json').read_bytes()
                write_features(value='')
                error = check('missing foF2','POST',api_url+'/api/v1/forecast/refresh',503,json={'station':'TR169'})
                assert error['detail']['code']=='insufficient_observations'
                assert (cache/'TR169.json').read_bytes()==before
                write_features(historical=True)
                check('historical data rejected as current','POST',api_url+'/api/v1/forecast/refresh',503,json={'station':'TR169'})
                assert (cache/'TR169.json').read_bytes()==before
                month_start=datetime(2025,9,1,tzinfo=UTC)
                temporary=path.with_suffix('.tmp')
                with temporary.open('w',newline='') as handle:
                    writer=csv.DictWriter(handle,fieldnames=['time_utc','foF2','foF2_source_timestamp','foF2_target_15min'])
                    writer.writeheader()
                    for i in range(2880):
                        timestamp=month_start+timedelta(minutes=15*i)
                        writer.writerow({'time_utc':timestamp.isoformat(),'foF2':5+math.sin(i/96),
                            'foF2_source_timestamp':(timestamp+timedelta(minutes=10)).isoformat(),'foF2_target_15min':999})
                temporary.replace(path)
                month=check('full month feature range','GET',data_url+'/api/v1/features/TR169',params={
                    'start':month_start.isoformat(),'end':(month_start+timedelta(minutes=15*2879)).isoformat()})
                assert len(month['records'])==2880 and all('foF2_target_15min' not in row['features'] for row in month['records'])
                write_features()
                (cache/'TR169.json').write_text('{broken')
                check('recover corrupt cache','POST',api_url+'/api/v1/forecast',json={'station':'TR169'})
                queued = check('queue update','POST',data_url+'/api/v1/updates',202,json={'station':'TR169','start':'2025-01-01T00:00:00Z','end':'2025-01-02T00:00:00Z','sources':'giro'})
                task = check('read update status','GET',data_url+'/api/v1/updates/'+queued['task_id'])
                assert task['status']=='queued'
                processes[0].terminate();processes[0].wait(timeout=10)
                if args.docker:
                    subprocess.run(['docker','rm',container_names[0]],check=True,capture_output=True)
                processes[0]=launch(0)
                for attempt in range(120):
                    try:
                        if client.get(data_url+'/health',timeout=1).status_code==200:break
                    except httpx.HTTPError:pass
                    time.sleep(.5)
                else:raise RuntimeError('Data API did not restart')
                persisted=check('queue survives data API restart','GET',data_url+'/api/v1/updates/'+queued['task_id'])
                assert persisted['status']=='queued'
                check('unknown update station','POST',data_url+'/api/v1/updates',404,json={'station':'UNKNOWN','start':'2025-01-01','end':'2025-01-02'})
                check('invalid update source','POST',data_url+'/api/v1/updates',422,json={'station':'TR169','start':'2025-01-01','end':'2025-01-02','sources':'bad'})
                check('reversed update range','POST',data_url+'/api/v1/updates',400,json={'station':'TR169','start':'2025-01-03','end':'2025-01-02'})
                check('invalid task ID','GET',data_url+'/api/v1/updates/not-a-task',404)
                processes[1].terminate(); processes[1].wait(timeout=10)
                before=(cache/'TR169.json').read_bytes()
                check('ML connection failure','POST',api_url+'/api/v1/forecast/refresh',502,json={'station':'TR169'})
                assert (cache/'TR169.json').read_bytes()==before
                processes[0].terminate();processes[0].wait(timeout=10)
                check('Data API connection failure','POST',api_url+'/api/v1/forecast/refresh',502,json={'station':'TR169'})
                available_cache=check('valid cache survives upstream outage','POST',api_url+'/api/v1/forecast',json={'station':'TR169'})
                assert available_cache['cached']
                expected_routes={(url,method,template) for url,document in documents.items() for template,ops in document['paths'].items() for method in ops if method in {'get','post','put','delete','patch'}}
                assert not expected_routes-covered, ('Uncovered routes',expected_routes-covered)
            report={'passed':len(results),'routes_covered':len(covered),'routes_total':len(expected_routes),
                    'runtime':'docker with nginx gateway' if args.docker else 'local Python processes',
                    'checks':results,'fixture':'synthetic completed interval; real model artifact; no forecast-quality claim'}
            if args.report:
                report_path=Path(args.report);report_path.parent.mkdir(parents=True,exist_ok=True)
                report_path.write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(report,indent=2))
        except Exception:
            if args.docker:
                for name in container_names:
                    result=subprocess.run(['docker','logs','--tail','30',name],capture_output=True,text=True)
                    print(result.stdout+result.stderr,file=sys.stderr)
            for log in logs: log.flush()
            for name, _, _ in specs:
                log_path=work/(name+'.log')
                if log_path.exists(): print(log_path.read_text(encoding='utf-8')[-4000:],file=sys.stderr)
            raise
        finally:
            for process in processes:
                if process.poll() is None: process.terminate()
            for process in processes:
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill();process.wait(timeout=10)
            for log in logs: log.close()
            if args.docker:
                for name in container_names:
                    subprocess.run(['docker','rm','--force',name],capture_output=True,check=False)
                subprocess.run(['docker','network','rm',network],capture_output=True,check=False)


if __name__=='__main__': main()
