"""Execute exact existing file sampler/default reader; no LLM or quality claim."""
import sys,pathlib,json,re,hashlib,time
P=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(P));from preflight import reference_threadx,dump
from oskernel_agent.pipeline.tree_builder import _files_for_subsys_prompt
from oskernel_agent.tools.tool_handlers import read_file
manifest=json.loads((P/'threadx-manifest.json').read_text());root=P/'sources/threadx'
files=[dict(path=e['path'],name=pathlib.Path(e['path']).name,lang='c') for e in manifest['files']]
picked=_files_for_subsys_prompt(files);refs=reference_threadx();rows={}
for budget in [6000,12000,24000]:
 used=0;exposed={};read=[]
 for f in picked:
  text=read_file(str(root),f['path']);n=len(text.encode())
  if used+n>budget:continue
  used+=n;read.append(f['path']);exposed[f['path']]={int(m.group(1)) for m in re.finditer(r'^\s*(\d+) \|',text,re.M)}
  (P/'reference'/('default-read-'+f['name']+'.txt')).write_text(text)
 hit=[r for r in refs if set(range(r['start'],r['end']+1))<=exposed.get(r['path'],set())]
 rows[str(budget)]=dict(bytes=used,read_files=read,fully_exposed_services=len(hit),families=sorted(set(r['family'] for r in hit)),disclaimer='Mechanical packing in sampler order, not model adaptive reading or full production report quality.')
result=dict(timestamp_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),indexed_files=len(files),prompted_files=len(picked),rows=rows,model_calls=0,production_functions_sha256={s:hashlib.sha256(pathlib.Path(s).read_bytes()).hexdigest() for s in ['src/oskernel_agent/pipeline/tree_builder.py','src/oskernel_agent/tools/tool_handlers.py']})
dump(P/'production-observation.json',result);print(json.dumps(result,ensure_ascii=False,indent=2))
