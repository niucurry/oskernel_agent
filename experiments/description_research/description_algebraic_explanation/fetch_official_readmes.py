from pathlib import Path
import urllib.request,json,base64,hashlib,concurrent.futures
P=Path(__file__).resolve().parent/'literature'
def get(url):return json.load(urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'OS-description-research','Accept':'application/vnd.github+json'}),timeout=25))
def one(repo):
 row={'official_repository':repo,'full_method_run':False}
 try:
  meta=get('https://api.github.com/repos/'+repo);branch=meta['default_branch'];rev=get('https://api.github.com/repos/'+repo+'/git/ref/heads/'+branch)['object']['sha'];row.update(branch=branch,revision=rev)
  data=get('https://api.github.com/repos/'+repo+'/contents/README.md?ref='+rev);raw=base64.b64decode(data['content']);blob=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest();assert blob==data['sha']
  name=repo.replace('/','--')+'-README.md';(P/name).write_bytes(raw);row.update(file=name,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),git_blob_verified=blob,status='retrieved')
 except Exception as e:row.update(status='failed',error=type(e).__name__+': '+str(e)[:150])
 return row
rows=list(concurrent.futures.ThreadPoolExecutor(max_workers=2).map(one,['bsy99615/MemDocAgent','vatsalananthula/verifiedcoupling']))
(P/'official-readmes.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n');print(json.dumps(rows))
