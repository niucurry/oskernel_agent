"""Recompute frozen and separately disclosed format diagnostics. No API calls."""
import copy,hashlib,json,pathlib,collections,importlib.util
ROOT=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('frozen_experiment',ROOT/'experiment.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
results=json.loads((ROOT/'results.json').read_text());ledger=json.loads((ROOT/'ledger.json').read_text())
primary=collections.defaultdict(collections.Counter);diagnostic=collections.defaultdict(collections.Counter);byrepo=collections.defaultdict(dict);failures=[];equal=[]
for r in results:
 e=r['evaluation'];arm=r['arm'];repo=r['task']['repo'];key=(repo,r['repeat']);primary[arm].update({k:e.get(k,0) for k in ['reference_bindings','correct','wrong','unknown','omitted','complete_evidence','unsupported_extra_claims']});primary[arm]['cells']+=1
 if e['status']!='ok':failures.append({'repo':repo,'repeat':r['repeat'],'arm':arm,'status':e['status']})
 byrepo[key][arm]=e
 # Prefixed field names are diagnostically canonicalized for every arm; never replace primary score.
 value=copy.deepcopy(r['output']);changed=[]
 if value:
  for c in value.get('claims',[]):
   role=str(c.get('role',''));prefix=r['task']['table']+'.'
   if role.startswith(prefix):c['role']=role[len(prefix):];changed.append(role)
 facts=json.loads((ROOT/'references'/repo/'facts.json').read_text())['facts'];folder=ROOT/'runs'/repo/str(r['repeat'])/arm
 de=module.score(r['task'],facts,value,e['status'],(folder/'context.txt').read_text())
 diagnostic[arm].update({k:de.get(k,0) for k in ['reference_bindings','correct','wrong','unknown','omitted','complete_evidence','unsupported_extra_claims']})
 if changed:failures.append({'repo':repo,'repeat':r['repeat'],'arm':arm,'diagnostic_only':'table-prefixed field names','original_fields':changed})
 for other in results:
  if other['task']['repo']==repo and other['repeat']==r['repeat'] and arm=='interface_witness' and other['arm']=='simple_lookup':equal.append({'repo':repo,'repeat':r['repeat'],'context_equal':r['context_sha256']==other['context_sha256'],'output_equal':r['output']==other['output'],'candidate_complete':e['complete_evidence'],'baseline_complete':other['evaluation']['complete_evidence']})
def method_cost(arm):
 refs=[]
 for r in results:
  if r['arm']!=arm:continue
  for ref in [r.get('call_ref'),r.get('allocation')]:
   if ref:
    i=ref.get('call',ref.get('reused_call'))
    if i:refs.append(i)
 rows=[ledger[i-1] for i in refs]
 return {'independent_required_requests':len(refs),'reported_tokens':sum((x.get('usage') or {}).get('total_tokens',0) for x in rows),'calls_without_usage':[x['id'] for x in rows if not x.get('usage')]}
freeze=json.loads((ROOT/'freeze.json').read_text());mismatch=[]
for p,h in freeze['files'].items():
 f=pathlib.Path(p) if p.startswith('/') else ROOT/p
 if not f.exists() or hashlib.sha256(f.read_bytes()).hexdigest()!=h:mismatch.append(p)
analysis={'primary':dict(primary),'format_diagnostic_only':dict(diagnostic),'diagnostics_and_failures':failures,'candidate_vs_simple':equal,'method_costs':{a:method_cost(a) for a in primary},'actual_requests':len(ledger),'requests_with_usage':sum(bool(x.get('usage')) for x in ledger),'reported_actual_tokens':sum((x.get('usage') or {}).get('total_tokens',0) for x in ledger),'tokens_of_failed_tls_call':'unknown; not treated as zero; predeclared additional global probe reserves 20000 tokens conservatively','freeze_mismatches':mismatch,'decision':'reject interface_witness as research method: no gain beyond simple lookup in development or either confirmation repository','failure_class':'mechanism adds no distinct evidence beyond ordinary lookup; not format failure, API absence or reference instability of the candidate','claim_limit':'Only explicit binding identities and cited input lines; no accuracy claim about free Chinese prose, runtime capability, complete production reports or judges','limitations':['production_reader is a bounded stage adapter, not the unchanged full production prompt; additional original production SUBSYS probe reported separately','production allocation also saw a default-read source excerpt before its final 10000-byte context; source information is not precisely matched across this arm and deterministic arms','official CodeWiki C components plus common BM25 is not full hierarchical CodeWiki with complete-source agent tools','post-hoc table-prefix normalization is diagnostic and cannot replace frozen metric','No length-matched natural prose semantic evaluation: all arms share output ceiling, but actual string lengths differ and candidate descriptions are longer than several adapters','Ctags and compiler projection support limited references; not OS build or live dispatch execution','Three repositories and two generation repetitions; no independent-judge study or significance inference','Provider balance snapshots both report CNY 1.55 to two decimals; zero visible delta cannot establish zero charge or eventual billing']}
(ROOT/'analysis.json').write_text(json.dumps(analysis,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(analysis['primary'],ensure_ascii=False,indent=2));print(analysis['method_costs']);print('freeze mismatches',mismatch)
