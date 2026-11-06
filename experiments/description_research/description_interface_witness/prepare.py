"""Download pinned filesystem slices and verify Git blob identities. No model calls."""
import concurrent.futures, hashlib, json, pathlib, urllib.request, shutil, time
ROOT=pathlib.Path(__file__).resolve().parent
DATA=pathlib.Path('/home/niu/.cache/oskernel-description-interface-witness')
SPECS=[('rtthread','RT-Thread/rt-thread','v5.2.1','97893c004c65760c638fd7eb571a08fc987a55e5',['components/dfs/dfs_v2/']),('riot','RIOT-OS/RIOT','2025.07','eb65305cf9f1b7affb50b17af5c12341b83a8636',['sys/fs/','sys/include/vfs.h']),('mynewt','apache/mynewt-core','mynewt_1_14_0_tag','828b84d04ea2dd826390ca458a32116a0af116fd',['fs/fs/','fs/nffs/','fs/littlefs/src/mynewt_glue.c'])]
def prepare(spec):
    name,repo,tag,revision,prefixes=spec
    req=urllib.request.Request(f'https://api.github.com/repos/{repo}/git/trees/{revision}?recursive=1',headers={'User-Agent':'oskernel-description-research'})
    tree=json.load(urllib.request.urlopen(req,timeout=60))
    if tree.get('truncated'): raise ValueError('incomplete tree')
    entries=[x for x in tree['tree'] if x['type']=='blob' and any(x['path'].startswith(p) for p in prefixes) and x['path'].endswith(('.c','.h')) and not any(p in x['path'].split('/') for p in ['selftest','test','tests']) and not x['path'].endswith(('/ff.c','/ff.h','/integer.h'))]
    def one(item):
        path=item['path'];dest=DATA/name/path;dest.parent.mkdir(parents=True,exist_ok=True)
        url=f'https://raw.githubusercontent.com/{repo}/{revision}/{path}'
        if not dest.exists():
            cached=pathlib.Path('/home/niu/.cache/oskernel-capability-confirmation')/name/path
            if cached.is_file(): shutil.copyfile(cached,dest)
            else:
                for attempt in range(3):
                    try:
                        dest.write_bytes(urllib.request.urlopen(url,timeout=45).read());break
                    except Exception:
                        if attempt==2: raise
                        time.sleep(1)
        raw=dest.read_bytes();gitsha=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if gitsha!=item['sha']: raise ValueError('blob mismatch '+path)
        return dict(item,sha256=hashlib.sha256(raw).hexdigest(),url=url)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex: result=list(ex.map(one,entries))
    m={'name':name,'repo':repo,'tag':tag,'revision':revision,'prefixes':prefixes,'partition':'development' if name=='rtthread' else 'confirmation','exposure':'RT-Thread previously used in other research' if name=='rtthread' else 'no matching repository found in available historical documents; not guaranteed blind or uncontaminated','source_root':str(DATA/name),'files':result}
    (ROOT/(name+'-manifest.json')).write_text(json.dumps(m,indent=2))
    print(name,len(result),sum(x.get('size',0) for x in result),flush=True)
if __name__=='__main__':
    for spec in SPECS: prepare(spec)
