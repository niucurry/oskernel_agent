"""Read-only offline recomputation: no private config, API, or generator import."""
from pathlib import Path
import json,hashlib,gzip,collections
P=Path(__file__).resolve().parent

def production():
 d=P/'production';ledger=json.loads((d/'ledger.json').read_text());total=0;tools=collections.Counter();issues=[]
 for row in ledger:
  i=row['id'];obj=json.loads((d/'calls'/f'{i:02d}-request.json').read_text());raw=json.dumps(obj,ensure_ascii=False).encode();assert hashlib.sha256(raw).hexdigest()==row['request_sha256']
  usage=None;parts={}
  for line in (d/'calls'/f'{i:02d}-response.txt').read_text().splitlines():
   if not line.startswith('data: ') or line[6:]=='[DONE]':continue
   chunk=json.loads(line[6:]);usage=chunk.get('usage') or usage
   for choice in chunk.get('choices',[]):
    for tool in choice.get('delta',{}).get('tool_calls',[]):
     old=parts.get(tool['index'],'');parts[tool['index']]=old+(tool.get('function',{}).get('name') or '')
  assert usage==row['usage'];total+=usage['total_tokens'];tools.update(parts.values())
 assert total==74359
 return {'requests':len(ledger),'tokens':total,'unique_tool_requests':dict(tools)}

def main():
 prod=production();answers=json.loads((P/'oracle-answers.json').read_text())['answers'];ledger=json.loads((P/'pilot-ledger.json').read_text());rows=[]
 for row in ledger:
  body=json.loads((P/'pilot-calls'/row['request_file']).read_text());assert hashlib.sha256(json.dumps(body,ensure_ascii=False).encode()).hexdigest()==row['request_sha256']
  reply=json.loads((P/'pilot-calls'/row['response_file']).read_text());assert row['usage']==reply['usage'];parsed=json.loads(reply['choices'][0]['message']['content']);assert len(parsed)==12
  vals=[int(x) if str(x).isdigit() else None for x in parsed]
  rows.append({'report':row['report'],'correct':sum(v==e for v,e in zip(vals,answers)),'unknown':sum(v is None for v in vals),'answered':sum(v is not None for v in vals)})
 pilot_tokens=sum(r['usage']['total_tokens'] for r in ledger);assert pilot_tokens==2901
 raw=gzip.decompress((P/'oracle-output.txt.gz').read_bytes());assert hashlib.sha256(raw).hexdigest()==json.loads((P/'oracle-answers.json').read_text())['compiler_raw_output_sha256']
 assert [int(x.split()[1]) for x in raw.decode().splitlines()[1:13]]==answers
 saved=json.loads((P/'pilot-results.json').read_text())['results']
 assert all((a['correct'],a['unknown'])==(b['correct'],b['unknown']) for a,b in zip(rows,saved))
 note=(P/'normal-form-template-report.md').read_text();template=(P/'template-report.md').read_text()
 result={'production':prod,'pilot':{'requests':len(ledger),'tokens':pilot_tokens,'results':rows},'total_requests':prod['requests']+len(ledger),'reported_tokens':prod['tokens']+pilot_tokens,'unknown_requests':0,'verified_oracle_raw_bytes':len(raw),'citation_identity_scope':{'template_direct_macro_citations':template.count('[sources/chalign.h:'),'normal_form_direct_macro_citations':note.count('[sources/chalign.h:'),'note':'Direct citation counts only, not semantic entailment; normal form lacks explicit per-formula links for 3 rewritten macros'},'research':'No gain in deterministic prototype preflight; no full augmented generator, repeat, heldout or human effect claim'}
 (P/'offline-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
