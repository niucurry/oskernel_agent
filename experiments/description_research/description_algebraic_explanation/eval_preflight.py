"""Paid, bounded report-consumer probe with an independent GCC answer file."""
from __future__ import annotations
import pathlib,json,hashlib,re,urllib.request,time,resource
P=pathlib.Path(__file__).resolve().parent
ORDER=['template-report.md','normal-form-template-report.md','production-module-plain.md','names-only-report.md']

def main():
 conf=json.loads(pathlib.Path('/home/niu/.config/oskernel-research/deepseek.json').read_text());key=conf['api_key']
 def dump(path,obj):
  raw=json.dumps(obj,ensure_ascii=False,indent=2)+'\n';assert key not in raw;path.write_text(raw)
 calls=P/'pilot-calls';calls.mkdir(exist_ok=True)
 ledger=json.loads((P/'pilot-ledger.json').read_text()) if (P/'pilot-ledger.json').exists() else []
 rows=[];questions=json.loads((P/'questions.json').read_text());expected=json.loads((P/'oracle-answers.json').read_text())['answers'];mapping=json.loads((P/'symbol-mask.json').read_text());start=time.monotonic()
 system='你是报告使用者。只根据给出的报告计算标量服务返回值，不访问源码，不根据不透明服务身份猜测语义。报告不足以唯一确定的答案为 UNKNOWN。输出且只输出 12 元素 JSON 数组；确定的整数用十进制字符串，不确定用 "UNKNOWN"。'
 for name in ORDER:
  report=(P/name).read_text();assert len(report)<=900
  for old,new in mapping.items():report=re.sub(r'\b'+re.escape(old)+r'\b',new,report)
  user='输入范围：无副作用的非负整数实参，原生 size_t 为 64 位；只问标量结果，不问实际地址访问。\n报告：\n'+report+'\n问题（按 id 顺序；arguments 为服务的实参）：\n'+json.dumps(questions,ensure_ascii=False)
  body={'model':'deepseek-flash','temperature':0.2,'thinking':{'type':'disabled'},'max_tokens':1000,'messages':[{'role':'system','content':system},{'role':'user','content':user}]};raw=json.dumps(body,ensure_ascii=False).encode();digest=hashlib.sha256(raw).hexdigest()
  reused=next((r for r in ledger if r['request_sha256']==digest and r.get('status')=='ok'),None)
  if reused:
   response=json.loads((calls/reused['response_file']).read_text());row=reused
  else:
   reserve=max(len(raw)+1000,20000);used=sum(r['accounted_tokens'] for r in ledger)
   if len(ledger)>=24 or used+reserve>60000:
    dump(P/'pilot-budget-stop.json',{'requests':len(ledger),'used':used,'next_reserve':reserve});break
   index=len(ledger)+1;request_file=f'{index:02d}-request.json';response_file=f'{index:02d}-response.json'
   row={'id':index,'report':name,'request_sha256':digest,'request_file':request_file,'response_file':response_file,'status':'pending','accounted_tokens':reserve};ledger.append(row);dump(calls/request_file,body);dump(P/'pilot-ledger.json',ledger)
   try:
    req=urllib.request.Request(conf['base_url'].rstrip('/')+'/chat/completions',data=raw,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    response_raw=urllib.request.urlopen(req,timeout=90).read();assert key.encode() not in response_raw;(calls/response_file).write_bytes(response_raw);response=json.loads(response_raw)
    row['usage']=response.get('usage');row['status']='ok' if row['usage'] else 'usage_unknown'
    if row['usage']:row['accounted_tokens']=row['usage']['total_tokens']
   except Exception as e:
    row.update(status='api_or_interface_failure',error={'type':type(e).__name__,'message':str(e)[:160]});dump(P/'pilot-ledger.json',ledger);break
   dump(P/'pilot-ledger.json',ledger)
  content=response['choices'][0]['message'].get('content','');(P/(name+'.consumer.txt')).write_text(content)
  try:
   parsed=json.loads(content);assert isinstance(parsed,list) and len(parsed)==12
   values=[int(x) if isinstance(x,(str,int)) and str(x).isdigit() else None for x in parsed]
   correct=sum(v==e for v,e in zip(values,expected));unknown=sum(v is None for v in values)
   scores={'correct':correct,'wrong':12-correct-unknown,'unknown':unknown,'missing':0,'answers':parsed,'format_failure':False}
  except Exception as e:
   scores={'correct':0,'wrong':0,'unknown':0,'missing':12,'format_failure':True,'format_error':type(e).__name__}
  rows.append({'report':name,'report_characters':len((P/name).read_text()),'masked_consumer_report_characters':len(report),'reused':bool(reused),'request_id':row['id'],**scores})
  print(name,scores.get('correct'),scores.get('unknown'),flush=True)
  # A saturated simple baseline makes the frozen +2 requirement impossible.
  # Diagnostic consumers continue for prototype/production, not to rescue it.
  if name=='template-report.md' and scores['correct']==12:
   dump(P/'development-stop.json',{'reason':'strong_simple_baseline_ceiling','baseline_correct':12,'denominator':12,'required_margin':2,'maximum_possible_candidate_score':12,'paid_generator_requests':0,'confirmation_not_run':True,'hypothesis':'No algebraic generator can pass frozen >=2/12 margin over this exact-expression report in this reference scope.','reference_boundary':'12 bounded scalar results, not report-wide truth or judge effectiveness'})
  if row['status']!='ok':break
 audit={'results':rows,'requests':len(ledger),'reported_tokens':sum((r.get('usage') or {}).get('total_tokens',0) for r in ledger),'unknown_requests':sum(not r.get('usage') for r in ledger),'wall_seconds':time.monotonic()-start,'cpu_seconds_self':resource.getrusage(resource.RUSAGE_SELF).ru_utime+resource.getrusage(resource.RUSAGE_SELF).ru_stime,'ground_truth':'Independent GCC original-header observations, not consumer answers or model judging','reader':'deepseek-flash is an experimental consumer; no human comprehension claim','no_new_generator_tuning':True}
 dump(P/'pilot-results.json',audit);print(json.dumps(audit,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
