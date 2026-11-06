"""Bounded execution of original production SUBSYS templates, without OpenCode setup."""
import json,pathlib,sys,time,hashlib,urllib.request
ROOT=pathlib.Path(__file__).resolve().parent
OUT=ROOT/'outputs';OUT.mkdir(exist_ok=True)
CONF=json.loads(pathlib.Path('/home/niu/.config/oskernel-research/deepseek.json').read_text())
PROTOCOL=json.loads((ROOT/'protocol.json').read_text())
MANIFEST=json.loads(pathlib.Path('/home/niu/agent-research/description_responsibility_budget/ChibiOS-manifest.json').read_text());REPO=pathlib.Path('/home/niu/agent-research/description_responsibility_budget/sources/ChibiOS')
from oskernel_agent.cli.setup_opencode import _build_session_prompt
from oskernel_agent.prompts.builder import SessionType
from oskernel_agent.pipeline.tree_builder import _build_subsys_request,_build_subsys_outputs,_validate_subsys_result,_apply_subsys_result,_clean_tree
from oskernel_agent.tools.tool_handlers import read_file,search_code
# Preserve exact original fast-mode functions while avoiding server startup and missing mcp package.
import ast,re
_mcp_source=pathlib.Path('src/oskernel_agent/cli/mcp_server.py').read_text()
_mcp_ast=ast.parse(_mcp_source)
_kept=[n for n in _mcp_ast.body if (isinstance(n,ast.FunctionDef) and n.name in ['_quick_source_files','_quick_analyze_subtree']) or (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_QUICK_SYMBOL_RE' for t in n.targets))]
_mcp_globals={'Path':pathlib.Path,'re':re,'__name__':'oskernel_agent.cli.mcp_server','__package__':'oskernel_agent.cli'}
exec(compile(ast.Module(body=_kept,type_ignores=[]),'<production-fast-mode-functions>','exec'),_mcp_globals)
_quick_analyze_subtree=_mcp_globals['_quick_analyze_subtree']
from oskernel_agent.reports.html_tree import _render_all_subsystems

def dump(p,x): p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
files=[{'path':x['path'],'name':pathlib.Path(x['path']).name,'lang':'c'} for x in MANIFEST['files']]
node={'name':'进程管理','path':'<subsys>/进程管理','files':files,'children':[]}
outputs=_build_subsys_outputs(node,OUT)
request=_build_subsys_request(node,REPO,outputs,None)
(OUT/'original-user-request.txt').write_text(request);system=_build_session_prompt(SessionType.SUBSYS);(OUT/'original-system-prompt.txt').write_text(system)

def tool(name,props,required):return {'type':'function','function':{'name':name,'description':name,'parameters':{'type':'object','properties':props,'required':required}}}
S={'type':'string'};N={'type':'integer'}
tools=[tool('analyze_subtree',{'subtree_path':S},[]),tool('read_file',{'path':S,'start_line':N,'end_line':N},['path']),tool('search_code',{'pattern':S,'file_glob':S,'max_results':N},['pattern']),tool('write_report',{'output_path':S,'content':S},['output_path','content'])]
dump(OUT/'source-adapter.json',{'mcp_dependency_preflight':'ModuleNotFoundError: mcp; zero model requests','adapter':'AST load exact existing fast-mode functions, no edits or dependency graph','mcp_server_sha256':hashlib.sha256(_mcp_source.encode()).hexdigest(),'script_sha256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'frozen_before_probe_requests_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())})
messages=[{'role':'system','content':system},{'role':'user','content':request}];ledger=[];toolcount=0;state='incomplete';error=None
start=time.monotonic(); previous_prompt_tokens=None; previous_message_count=0; analysis_tools=0
try:
 for i in range(PROTOCOL['probe_request_ceiling']):
  body={'model':'deepseek-flash','messages':messages,'tools':tools,'max_tokens':6000,'temperature':0.2,'thinking':{'type':'disabled'}}
  raw=json.dumps(body,ensure_ascii=False).encode();used=sum((x.get('usage') or {}).get('total_tokens',20000) for x in ledger)
  if previous_prompt_tokens is None:reserve=len(raw)+6000
  else:
   added=messages[previous_message_count:]
   reserve=previous_prompt_tokens+len(json.dumps(added,ensure_ascii=False).encode())+1000*len(added)+6000
  if used+reserve>184000:state='token_budget_stop';break
  dump(OUT/f'request-{i+1:02d}.json',body)
  req=urllib.request.Request(CONF['base_url'].rstrip('/')+'/chat/completions',data=raw,headers={'Authorization':'Bearer '+CONF['api_key'],'Content-Type':'application/json'})
  if i<5:
   prior_request=json.loads((ROOT/'probe-120k-stop/outputs'/f'request-{i+1:02d}.json').read_text())
   if prior_request!=body:raise ValueError('existing request changed, cannot reuse response')
   response=json.loads((ROOT/'probe-120k-stop/outputs'/f'response-{i+1:02d}.json').read_text())
  else:response=json.load(urllib.request.urlopen(req,timeout=90))
  dump(OUT/f'response-{i+1:02d}.json',response)
  if not response.get('usage'):state='usage_unknown';break
  ledger.append({'id':i+1,'request_sha256':hashlib.sha256(raw).hexdigest(),'usage':response['usage'],'model':response.get('model'),'reused_existing_response':i<5});dump(OUT/'ledger.json',ledger)
  previous_prompt_tokens=response['usage']['prompt_tokens'];previous_message_count=len(messages)
  msg=response['choices'][0]['message'];messages.append(msg)
  calls=msg.get('tool_calls') or []
  if not calls:state='model_stopped';break
  for call in calls:
   toolcount+=1
   name=call['function']['name'];a=json.loads(call['function']['arguments'])
   if name!='write_report':analysis_tools+=1
   if analysis_tools>12:state='tool_budget_stop';raise RuntimeError(state)
   if name=='read_file':result=read_file(str(REPO),a['path'],a.get('start_line'),a.get('end_line'))
   elif name=='search_code':result=search_code(str(REPO),a['pattern'],a.get('file_glob','*'),max_results=min(40,a.get('max_results',20)))
   elif name=='analyze_subtree':result=_quick_analyze_subtree(REPO,a.get('subtree_path',''))
   elif name=='write_report':
    p=pathlib.Path(a['output_path']).resolve();p.relative_to(OUT.resolve());p.write_text(a['content']);result='已写入 '+str(p)
   else:result='[未提供的工具] '+name
   messages.append({'role':'tool','tool_call_id':call['id'],'content':result})
  print('production call',i+1,'tokens',response['usage']['total_tokens'],'tools',toolcount,flush=True)
  p=pathlib.Path(outputs['json_path'])
  if p.exists():
   parsed=json.loads(p.read_text());parsed['content']=pathlib.Path(outputs['content_path']).read_text()
   for m in parsed.get('modules',[]):m['content']=pathlib.Path(outputs['module_paths'][m['slot']-1]).read_text()
   _validate_subsys_result(parsed,'进程管理',REPO);_apply_subsys_result(node,parsed,REPO)
   tree={'meta':{'repo':'ChibiOS-public-portable-core','indexed_files':len(files)},'facts':{},'verdict':{},'tree':{'children':[_clean_tree(node)]}}
   dump(OUT/'tree.json',tree)
   (OUT/'module-report.html').write_text(_render_all_subsystems(tree,lambda x:'#'+x))
   state='complete_subsys';break
except Exception as e:error={'type':type(e).__name__,'message':str(e)}
dump(OUT/'audit.json',{'state':state,'error':error,'requests_with_usage':len(ledger),'actual_tokens':sum((x.get('usage') or {}).get('total_tokens',20000) for x in ledger),'tools':toolcount,'analysis_tools':analysis_tools,'wall_seconds':time.monotonic()-start,'opencode_configuration_missing':True,'scope':'current original production subsystem templates via bounded 4-tool direct API adapter; no full OpenCode/VERDICT/digest/PDF','protocol':PROTOCOL})
print('production probe',state,error,flush=True)
