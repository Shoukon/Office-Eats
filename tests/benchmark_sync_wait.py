"""PERF-02 invented-data benchmark; no app bootstrap or live transport.
Run: PYTHONPATH=tests;. python -B tests/benchmark_sync_wait.py --output before.json
Output is restricted to this checkout's ignored .test-tmp directory.
Component probes overlap conceptually (encryption includes serialization); do not sum them.
"""
import argparse
import hashlib
import json
import statistics
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken
from backup_safety import BackupGuard, content_digest, validate_backup
from test_backup_safety import FakeRemote, fixture, schema, NOW, SOURCE, load_app_functions


def ms(fn):
    start=time.perf_counter(); value=fn()
    return value,(time.perf_counter()-start)*1000


def summary(values):
    return {'median_ms':round(statistics.median(values),3),'min_ms':round(min(values),3),
            'max_ms':round(max(values),3),'n':len(values)}


def invented(count):
    data=fixture();base=data['orders'][0]
    data['orders']=[dict(base,id=i+1,item_name='Fake '+str(i)) for i in range(count)]
    return data


def mutate(guard,operation):
    if operation=='create':
        view=guard.member_snapshot(); token={'generation':view['generation'],'member_id':1,'name':'測試甲'}
        return guard.create_order(token,('主餐','Fake added',100,'',1,NOW,100))
    if operation=='shops':
        conn=guard.connect(write=True)
        try:
            conn.execute("UPDATE config_shop SET shop_name='Fake main changed' WHERE category='main'")
            conn.execute("UPDATE config_shop SET shop_name='Fake drink changed' WHERE category='drink'")
            conn.commit()
        finally:conn.close()
        return 2
    token=guard.order_token(guard.order_snapshot(),[1])
    return guard.apply_order_action(operation,token,{'item_name':'Fake edited','price':120,
        'unit_price':120,'quantity':1,'custom':''} if operation=='edit' else None)


def trial(root,count,operation,get_delay,put_delay,mode='normal'):
    with TemporaryDirectory(prefix='perf-probe-',dir=root) as folder:
        guard=BackupGuard(Path(folder)/'fake.db',lambda:NOW,timeout=3);guard.prepare(schema)
        remote=FakeRemote(invented(count));guard.startup(True,remote.get)
        remote.get_calls=remote.put_calls=0
        wall_start=time.perf_counter()
        _,local=ms(lambda:mutate(guard,operation));expected=guard.export()
        remote.mode=mode
        if mode=='get_failure':remote.fail_get=True
        if mode=='sha_conflict':remote.sha='fake-other-writer'
        entered,ready=threading.Event(),threading.Event();times={'get':0.,'put':0.};out={};errors=[]
        def get():
            start=time.perf_counter()
            if not entered.is_set():
                entered.set()
                if not ready.wait(3):raise RuntimeError('reader not started')
            try:
                time.sleep(get_delay);return remote.get()
            finally:times['get']+=(time.perf_counter()-start)*1000
        def put(data,sha):
            start=time.perf_counter()
            try:
                time.sleep(put_delay);return remote.put(data,sha)
            finally:times['put']+=(time.perf_counter()-start)*1000
        def sync():
            try:
                out['ok'],out['sync_ms']=ms(lambda:guard.transfer(True,get,put))
                out['total_ms']=(time.perf_counter()-wall_start)*1000
            except BaseException as exc:errors.append(type(exc).__name__)
        thread=threading.Thread(target=sync);thread.start()
        if not entered.wait(3):raise AssertionError('GET not reached')
        other=BackupGuard(guard.db_file,lambda:NOW,timeout=3)
        start=time.perf_counter();ready.set()
        try:
            view=other.order_snapshot();read=(time.perf_counter()-start)*1000
        finally:thread.join(6)
        if thread.is_alive() or errors:raise AssertionError(errors or 'worker stuck')
        assert guard.export()==expected
        assert len(view['rows'])==len(expected['orders'])
        return {'local_ms':local,'sync_ms':out['sync_ms'],'local_to_sync_ms':local+out['sync_ms'],
                'total_ms':out['total_ms'],
                'read_ms':read,'get_ms':times['get'],'put_ms':times['put'],
                'get_calls':remote.get_calls,'put_calls':remote.put_calls,'ok':out['ok'],
                'status':guard.load_state()['sync']['kind'],'attempt':bool(guard.load_state()['attempt'])}


def run(root):
    samples=3;rows=[]
    for count in (10,500):
        for get_delay,put_delay in ((0,0),(.05,.05),(.5,0)):
            for operation in ('create','edit','delete','shops'):
                values=[trial(root,count,operation,get_delay,put_delay) for _ in range(samples)]
                rows.append({'orders':count,'operation':operation,'get_delay_ms':get_delay*1000,
                             'put_delay_ms':put_delay*1000,'samples':values,
                             'timings':{key:summary([v[key] for v in values]) for key in
                                        ('local_ms','sync_ms','local_to_sync_ms','total_ms','read_ms','get_ms','put_ms')}})
    failures=[]
    for mode in ('get_failure','reject','timeout_before','lost_response','lost_response_get_fail','sha_conflict'):
        values=[trial(root,10,'edit',.05,.05,mode) for _ in range(samples)]
        failures.append({'mode':mode,'samples':values,'timings':{key:summary([v[key] for v in values])
                         for key in ('total_ms','sync_ms','read_ms','get_ms','put_ms')}})
    # Separate local component probes; never treat their sum as total sync time.
    key=Fernet.generate_key().decode()
    codec=load_app_functions(['encrypt_github_backup','decrypt_github_backup'],Fernet=Fernet,
        InvalidToken=InvalidToken,get_github_settings=lambda:('fake','fake','fake','fake','fake',key))
    components=[]
    for count in (10,500):
        with TemporaryDirectory(prefix='perf-components-',dir=root) as folder:
            guard=BackupGuard(Path(folder)/'fake.db',lambda:NOW);guard.prepare(schema)
            guard.startup(True,FakeRemote(invented(count)).get);data=guard.export()
            encrypted=codec['encrypt_github_backup'](data)
            probes={'snapshot':guard.export,'validation':lambda:validate_backup(data),
                    'digest':lambda:content_digest(data),'serialization':lambda:json.dumps(data,ensure_ascii=False).encode(),
                    'encryption_including_serialization':lambda:codec['encrypt_github_backup'](data),
                    'decryption_including_validation':lambda:codec['decrypt_github_backup'](encrypted)}
            components.append({'orders':count,'timings':{name:summary([ms(fn)[1] for _ in range(5)])
                                                        for name,fn in probes.items()}})
    return {'base_commit':'452a8ae31eedf2d108fa3007e43fcb816b1e1b9d',
            'source_sha256':{p:hashlib.sha256((SOURCE.parent/p).read_bytes()).hexdigest()
                             for p in ('order.py','backup_safety.py')},
            'note':'FakeRemote delays, local wall times; read_ms includes snapshot work and coordination wait. No browser UI timing. Component probes independent; not additive.',
            'normal':rows,'failure':failures,'components':components}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    if Path(args.output).name!=args.output or not args.output.endswith('.json'):parser.error('output must be a JSON filename')
    root=SOURCE.parent/'.test-tmp';root.mkdir(exist_ok=True);target=root/args.output
    if target.exists():parser.error('refusing to overwrite existing evidence')
    with patch('urllib.request.urlopen',side_effect=AssertionError('LIVE NETWORK FORBIDDEN')):
        result=run(root)
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Saved fake-data benchmark:',target)
