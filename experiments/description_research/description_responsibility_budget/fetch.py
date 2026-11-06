import pathlib,json,urllib.request,hashlib,concurrent.futures,time
P=pathlib.Path(__file__).resolve().parent
specs=[('threadx','eclipse-threadx/threadx',['common/src/','common/inc/']),('ChibiOS','ChibiOS/ChibiOS',['os/rt/src/','os/rt/include/']),('nuttx','apache/nuttx',['sched/','include/nuttx/sched.h','include/nuttx/semaphore.h','include/mqueue.h','include/signal.h','include/pthread.h','include/time.h'])]
def fetch(j):
 repo,slug,sha,e=j;p=P/'sources'/repo/e['path'];p.parent.mkdir(parents=True,exist_ok=True)
 for i in range(3):
  try:
   if p.exists():raw=p.read_bytes()
   else:raw=urllib.request.urlopen(f'https://raw.githubusercontent.com/{slug}/{sha}/{e["path"]}',timeout=45).read();p.write_bytes(raw)
   actual=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
   if actual!=e['sha']:raise ValueError('blob mismatch '+e['path'])
   return dict(path=e['path'],git_blob=actual,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
  except Exception:
   if i==2:raise
   time.sleep(1)
for repo,slug,prefixes in specs:
 tree=json.loads((P/(repo+'-tree.json')).read_text());es=[e for e in tree['tree'] if e['type']=='blob' and any(e['path'].startswith(pre) for pre in prefixes) and e['path'].endswith(('.c','.h'))]
 with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex: files=list(ex.map(fetch,[(repo,slug,tree['sha'],e) for e in es]))
 (P/(repo+'-manifest.json')).write_text(json.dumps(dict(repo=slug,sha=tree['sha'],files=files),indent=2)+'\n')
 print(repo,len(files),sum(e['bytes'] for e in files),flush=True)
