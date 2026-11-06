"""Recompute independent identity localization scores; no config, network or model."""
import pathlib,json,hashlib,collections,re
P=pathlib.Path(__file__).resolve().parent
prepared=json.loads((P/'prepared.json').read_text());truth=prepared['truth'];ledger=json.loads((P/'ledger.json').read_text());freeze=json.loads((P/'freeze.json').read_text());N=len(truth)
def score(ans):
 grouped=collections.defaultdict(list)
 for a in ans:
  if isinstance(a,dict):grouped[a.get('question_id')].append(a.get('anchor'))
 c=w=u=o=0
 for q,expected in truth.items():
  values=grouped.get(q,[])
  if not values:o+=1
  elif len(values)!=1:w+=1
  elif values[0]==expected:c+=1
  elif values[0]=='UNKNOWN':u+=1
  else:w+=1
 return dict(correct=c,wrong=w,unknown=u,omitted=o,denominator=N,accuracy=c/N)
rows=[]
for r in ledger:
 response=json.loads((P/'calls'/f'{r["id"]:02d}-response.json').read_text());answers=json.loads(response['choices'][0]['message']['content'])['answers'];s=score(answers)
 rows.append(dict(arm=r['arm'],repeat=r['repeat'],score=s,wrong_questions=[a for a in answers if a.get('question_id') in truth and a.get('anchor')!=truth[a['question_id']]]))
result=dict(rows=rows,freeze_mismatches=[name for name,h in freeze['files'].items() if hashlib.sha256((P/name).read_bytes()).hexdigest()!=h],requests=len(ledger),actual_tokens=sum(r['usage']['total_tokens'] for r in ledger),recomputes_only='service identity localization, not arbitrary claim correctness')
(P/'recomputed.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))
