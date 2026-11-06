"""Small primary files only; preserve retrieval errors, no full datasets/models."""
from pathlib import Path
import urllib.request,json,hashlib,concurrent.futures,zipfile,io
P=Path(__file__).resolve().parent;L=P/'literature';L.mkdir(exist_ok=True)
JOBS=[('asap-paper.html','https://arxiv.org/html/2304.06815v3'),('memdocagent-paper.html','https://arxiv.org/html/2605.14563v1'),('verifiedcoupling-paper.html','https://arxiv.org/html/2606.21678v1'),('vlp-paper.html','https://arxiv.org/html/2607.02333v1'),('asap-artifact-record.json','https://zenodo.org/api/records/7792276')]
def one(pair):
 name,url=pair
 try:
  raw=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'OS-description-research/1.0'}),timeout=30).read()
  assert len(raw)<3000000
  (L/name).write_bytes(raw);return {'url':url,'file':name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'status':'retrieved'}
 except Exception as e:return {'url':url,'file':name,'status':'failed','error':type(e).__name__+': '+str(e)[:150]}
rows=list(concurrent.futures.ThreadPoolExecutor(max_workers=5).map(one,JOBS))
record=L/'asap-artifact-record.json'
if record.exists():
 obj=json.loads(record.read_text())
 for f in obj.get('files',[]):
  if f['key']=='scripts.zip':
   url=f['links']['self'];r=one(('asap-scripts.zip',url));rows.append(r)
   if r['status']=='retrieved':
    z=zipfile.ZipFile(L/'asap-scripts.zip');files=[]
    for n in z.namelist():
     if n.endswith('.py') and not n.startswith('/') and '..' not in Path(n).parts:
      raw=z.read(n);q=L/'asap-original'/n;q.parent.mkdir(parents=True,exist_ok=True);q.write_bytes(raw);files.append({'file':str(q.relative_to(L)),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
    r['extracted_python_files']=files
(L/'retrieval.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(rows,ensure_ascii=False))
