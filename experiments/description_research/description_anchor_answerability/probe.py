import pathlib,json,hashlib,time,re,collections,urllib.request
P=pathlib.Path(__file__).resolve().parent
CONF=json.loads(pathlib.Path('/home/niu/.config/oskernel-research/deepseek.json').read_text())
PROTOCOL=json.loads((P/'protocol.json').read_text());prep=json.loads((P/'reference-preflight.json').read_text());items=prep['items'];N=len(items)
# Keep precisely the first manually written purpose line, excluding later section headings.
manual=pathlib.Path('/home/niu/agent-research/description_responsibility_budget/reference/threadx-chapter4.md').read_text()
sections={sec.splitlines()[0].strip():sec for sec in re.split(r'(?m)^## ',manual)[1:]}
for i,x in enumerate(items):
 sec=sections[x['manual_api']];x['purpose']=next(line.strip() for line in sec.split('\n',1)[1].splitlines() if line.strip());x['id']='E'+hashlib.sha256(('source-anchor:'+x['manual_api']).encode()).hexdigest()[:10]
# Opaque labels and docs are deliberately unordered relative to questions.
qs=[dict(question_id=f'Q{i:02d}',purpose=x['purpose']) for i,x in enumerate(items)]
def docs(arm):
 rows=[]
 for i,x in enumerate(items):
  role=x['purpose'] if arm=='good_masked' else items[(i+1)%N]['purpose']
  name=x['manual_api'] if arm in ['wrong_roles_unmasked','names_only_unmasked'] else ''
  if arm=='names_only_unmasked':role=''
  rows.append(dict(anchor=x['id'],name=name,description=role))
 return sorted(rows,key=lambda r:r['anchor'])
def dump(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def tok(s):return re.findall('[a-z]+',s.lower().replace('_',' '))
def lexical(arm):
 rows=docs(arm);answers=[]
 for q in qs:
  scores=[]
  for row in rows:
   # Roles only: applies precisely the measurement goal, rather than rewarding API names.
   a=collections.Counter(tok(q['purpose']));b=collections.Counter(tok(row['description']));overlap=sum((a&b).values());score=overlap/max(1,sum(a.values())+sum(b.values())-overlap)
   scores.append((score,row['anchor']))
  best=max(scores);answers.append(dict(question_id=q['question_id'],anchor=best[1] if best[0]>0 else 'UNKNOWN'))
 return answers
truth={q['question_id']:x['id'] for q,x in zip(qs,items)}
def score(ans):
 if not isinstance(ans,list):return dict(status='format_failure',correct=0,wrong=0,unknown=0,omitted=N)
 grouped=collections.defaultdict(list)
 for a in ans:
  if isinstance(a,dict):grouped[a.get('question_id')].append(a.get('anchor'))
 c=w=u=o=dup=0
 for q,expected in truth.items():
  values=grouped.get(q,[])
  if not values:o+=1
  elif len(values)>1:dup+=1;w+=1
  elif values[0]==expected:c+=1
  elif values[0]=='UNKNOWN':u+=1
  else:w+=1
 return dict(status='ok',correct=c,wrong=w,unknown=u,omitted=o,duplicate_questions=dup,denominator=N,accuracy=c/N)
def main():
 start=time.monotonic();P.joinpath('calls').mkdir(exist_ok=True)
 dump(P/'prepared.json',dict(items=items,questions=qs,truth=truth,arms={a:docs(a) for a in PROTOCOL['arms']}))
 freeze={name:hashlib.sha256((P/name).read_bytes()).hexdigest() for name in ['protocol.json','probe.py','reference-preflight.json','prepared.json']};dump(P/'freeze.json',dict(files=freeze,timestamp_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())))
 ledger=[];results=[]
 for repeat in [1,2]:
  for arm in PROTOCOL['arms']:
   system='你是代码描述报告的读者。只根据给定文档，为每个功能说明选择对应的实现锚点。没有足够文档依据则回答 UNKNOWN。不得凭仓库/API 名称的先验知识覆盖文档。输出 JSON 对象，含 answers 数组，每项 question_id、anchor。不要解释。'
   payload=dict(document=docs(arm),questions=qs)
   body=dict(model='deepseek-flash',messages=[dict(role='system',content=system),dict(role='user',content=json.dumps(payload,ensure_ascii=False))],max_tokens=1800,temperature=.2,thinking={'type':'disabled'},response_format={'type':'json_object'})
   raw=json.dumps(body,ensure_ascii=False).encode();used=sum((r.get('usage') or {}).get('total_tokens',20000) for r in ledger)
   if used+len(raw)+1800>60000:
    results.append(dict(arm=arm,repeat=repeat,state='budget_stop',score=dict(status='budget_stop',correct=0,omitted=N)));continue
   callid=len(ledger)+1;dump(P/'calls'/f'{callid:02d}-request.json',body);row=dict(id=callid,arm=arm,repeat=repeat,request_sha256=hashlib.sha256(raw).hexdigest());state='ok';parsed=None
   try:
    req=urllib.request.Request(CONF['base_url'].rstrip('/')+'/chat/completions',data=raw,headers={'Authorization':'Bearer '+CONF['api_key'],'Content-Type':'application/json'})
    response=json.load(urllib.request.urlopen(req,timeout=90));dump(P/'calls'/f'{callid:02d}-response.json',response);row['usage']=response.get('usage');row['model']=response.get('model');content=response['choices'][0]['message']['content'];parsed=json.loads(content)
    if not row['usage']:state='usage_unknown'
   except Exception as e:
    state='api_failure' if not 'response' in locals() else 'format_failure';row['error']=dict(type=type(e).__name__,detail=str(e))
   ledger.append(row);dump(P/'ledger.json',ledger)
   result=score(parsed.get('answers') if parsed else None);result['status']=state if state!='ok' else result['status'];results.append(dict(arm=arm,repeat=repeat,state=state,score=result));dump(P/'results.json',results)
   print(repeat,arm,result,'tokens',row.get('usage',{}).get('total_tokens') if row.get('usage') else 'unknown',flush=True)
 analysis=dict(results=results,lexical_controls={a:score(lexical(a)) for a in PROTOCOL['arms']},reported_actual_tokens=sum((r.get('usage') or {}).get('total_tokens',0) for r in ledger),requests=len(ledger),usage_unknown=sum(not r.get('usage') for r in ledger),wall_seconds=time.monotonic()-start)
 groups={(r['arm'],r['repeat']):r['score'] for r in results};passes=[]
 for repeat in [1,2]:
  g=groups[('good_masked',repeat)].get('accuracy',0);un=groups[('wrong_roles_unmasked',repeat)].get('accuracy',0);mask=groups[('wrong_roles_masked',repeat)].get('accuracy',0)
  passes.append(g>=.9 and un-mask>=.5)
 analysis['artifact_sensitivity_gate_passed']=all(passes);analysis['novel_method_established']=False;analysis['decision']='retain independent-reference/identity controls as diagnostic; no standalone research method: exact purpose controls are solved by simple lexical role search'
 dump(P/'analysis.json',analysis);print('analysis',json.dumps({k:v for k,v in analysis.items() if k!='results'},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
