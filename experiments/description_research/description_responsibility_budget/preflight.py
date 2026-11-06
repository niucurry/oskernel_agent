from __future__ import annotations
import pathlib,json,re,hashlib,subprocess,collections,math,time
from tree_sitter import Language,Parser
import tree_sitter_c
P=pathlib.Path(__file__).resolve().parent
parser=Parser(Language(tree_sitter_c.language()))
def dump(p,x):p.write_text(json.dumps(x,indent=2,ensure_ascii=False)+'\n')
def walk(n):
 yield n
 for c in n.children:yield from walk(c)
def words(s):return re.findall('[a-z]+',re.sub(r'([a-z])([A-Z])',r'\1 \2',s).lower())
def blank_comments(text):
 # tree-sitter identifies comments; preserve source coordinates, string literals.
 raw=bytearray(text.encode());tree=parser.parse(bytes(raw))
 for n in walk(tree.root_node):
  if n.type=='comment':
   for i in range(n.start_byte,n.end_byte):
    if raw[i]!=10:raw[i]=32
 return bytes(raw).decode()
def units(repo):
 root=P/'sources'/repo;us=[];files=sorted(root.rglob('*.c'))
 cmd=['ctags','--output-format=json','--fields=+neK','--kinds-C=f','-f','-']+[str(f) for f in files]
 result=subprocess.run(cmd,capture_output=True,text=True,check=True,timeout=60)
 cached={f:blank_comments(f.read_text()).splitlines() for f in files}
 for line in result.stdout.splitlines():
  d=json.loads(line)
  if d.get('kind')!='function':continue
  f=pathlib.Path(d['path']);path=f.relative_to(root).as_posix();name=d['name'];start=d['line'];end=d.get('end')
  if not end:continue
  lines=cached[f];body='\n'.join(lines[start-1:end]);sig=body.split('{',1)[0]
  code='\n'.join(f'{i+1} | {lines[i].rstrip()}' for i in range(start-1,end) if lines[i].strip())
  rendering=f'## {path} : {start}-{end}\n'+code+'\n'
  types=set(re.findall(r'\b(?:TX_[A-Z_]+|[A-Za-z_]\w*_t|struct\s+[A-Za-z_]\w*)\b',sig))
  types-={'size_t','ssize_t','uint8_t','uint16_t','uint32_t','uint64_t','int8_t','int16_t','int32_t','int64_t','uintptr_t','intptr_t','ptrdiff_t'}
  ts=words(name);common={'tx','txe','ch','nx','nxsched','nxsem','nxtask','nxsig','pthread'}
  if '_' in name:
   parts=[w for w in name.strip('_').split('_') if w];fam=parts[1] if parts and parts[0] in common and len(parts)>1 else parts[0]
  else:
   parts=re.findall('[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+',name);fam=parts[1] if parts and parts[0]=='ch' and len(parts)>1 else parts[0]
  us.append(dict(name=name,path=path,start=start,end=end,signature=sig,types=sorted(types),family=fam,text=rendering,cost=len(rendering.encode()),features=sorted(set(ts))))
 return us

def name_rr(us,budget):
 groups=collections.defaultdict(list)
 for u in us:groups[u['family']].append(u)
 for g in groups:groups[g].sort(key=lambda u:(u['cost'],u['name']))
 picked=[];used=0;offset=0
 while offset<max(map(len,groups.values())):
  for g in sorted(groups):
   if offset<len(groups[g]):
    u=groups[g][offset]
    if used+u['cost']<=budget:picked.append(u);used+=u['cost']
  offset+=1
 return picked

def weighted(us,budget):
 counts=collections.Counter(f for u in us for f in u['features']);covered=set();remaining=list(us);picked=[];used=0
 while True:
  feasible=[u for u in remaining if u['cost']+used<=budget]
  if not feasible:break
  def score(u):return sum(math.log(1+len(us)/counts[f]) for f in set(u['features'])-covered)/u['cost']
  u=max(feasible,key=lambda u:(score(u),-u['cost'],u['name']))
  picked.append(u);remaining.remove(u);covered.update(u['features']);used+=u['cost']
 return picked

def minimax(us,budget):
 counts=collections.Counter(t for u in us for t in u['types']);groups={t for t,c in counts.items() if c>=2}
 memberships={u['name']:set(u['types'])&groups for u in us}
 # Objects without explicit types are part of one residual responsibility pool.
 if any(not m for m in memberships.values()):groups.add('<no_object_type>')
 for u in us:
  if not memberships[u['name']]:memberships[u['name']]={'<no_object_type>'}
 coverage=collections.Counter();remaining=list(us);picked=[];used=0
 while True:
  feasible=[u for u in remaining if u['cost']+used<=budget]
  if not feasible:break
  def rank(u):
   after=coverage.copy();after.update(memberships[u['name']]);profile=tuple(sorted(after[t] for t in groups))
   # Full profile lexicographically improves the worst exposed object first.
   return profile,-u['cost'],u['name']
  u=max(feasible,key=rank);picked.append(u);remaining.remove(u);coverage.update(memberships[u['name']]);used+=u['cost']
 return picked

def reference_threadx():
 raw=(P/'reference/threadx-chapter4.md').read_text()
 names=set(re.findall(r'^## (tx_[a-z_]+)\s*$',raw,re.M))
 # Independent Ctags tool, not generator AST, establishes original definition identity and spans.
 root=P/'sources/threadx';cmd=['ctags','--output-format=json','--fields=+neK','--kinds-C=f','-f','-']+[str(f) for f in sorted(root.rglob('*.c'))]
 r=subprocess.run(cmd,capture_output=True,text=True,check=True,timeout=60)
 refs=[]
 for line in r.stdout.splitlines():
  d=json.loads(line);name=d.get('name','');public=name.lstrip('_')
  if public not in names:continue
  parts=public.split('_');family='_'.join(parts[1:3]) if parts[1] in ['block','byte','event'] else parts[1]
  refs.append(dict(name=name,manual_api=public,family=family,path=pathlib.Path(d['path']).relative_to(root).as_posix(),start=d['line'],end=d.get('end',d['line'])))
 dump(P/'reference/threadx-facts.json',dict(manual_sha256=hashlib.sha256(raw.encode()).hexdigest(),manual_api_count=len(names),source_intersection=refs,command=cmd))
 return refs

def exposure(picked,refs):
 by={(u['path'],u['name']):u for u in picked};covered=[];partial=[]
 for r in refs:
  u=by.get((r['path'],r['name']))
  if u and u['start']<=r['start'] and u['end']>=r['end']:covered.append(r)
  elif u:partial.append(r)
 allfam=set(r['family'] for r in refs);hitfam=set(r['family'] for r in covered)
 return dict(selected_functions=len(picked),bytes=sum(u['cost'] for u in picked),exposed_documented_services=len(covered),families=len(hitfam),family_coverage=len(hitfam)/len(allfam),all_families=sorted(allfam),covered_families=sorted(hitfam),partial_definition_count=len(partial),source_function_names=[u['name'] for u in picked])

def main():
 start=time.time();protocol=json.loads((P/'protocol.json').read_text());freeze={str(f.relative_to(P)):hashlib.sha256(f.read_bytes()).hexdigest() for f in [P/'protocol.json',P/'preflight.py',P/'threadx-manifest.json',P/'reference/threadx-chapter4.md']};dump(P/'preflight-freeze.json',dict(timestamp_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),files=freeze))
 us=units('threadx');refs=reference_threadx();rows={}
 for b in protocol['no_call_preflight']['context_bytes']:
  rows[str(b)]={name:exposure(method(us,b),refs) for name,method in [('name_round_robin',name_rr),('weighted_coverage',weighted),('type_minimax',minimax)]}
 gate=True
 for b in ['12000','24000']:
  candidate=rows[b]['type_minimax'];baseline=max((rows[b][a] for a in ['name_round_robin','weighted_coverage']),key=lambda x:(x['family_coverage'],x['exposed_documented_services']))
  gate &= candidate['family_coverage']-baseline['family_coverage']>=.1 and candidate['exposed_documented_services']>=baseline['exposed_documented_services']
 result=dict(rows=rows,gate_passed=bool(gate),decision='advance' if gate else 'retire: no required increment beyond strong source selector',elapsed_seconds=time.time()-start,function_units=len(us),reference_services=len(refs),model_calls=0,main_evaluation_scope='context exposure only; no generated prose quality measured')
 dump(P/'preflight-results.json',result);print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
