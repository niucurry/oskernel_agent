"""Frozen limited description experiment; compiler facts, no model evaluator.
Run prepare (no calls), freeze, then run. All call bodies/responses are retained.
"""
from __future__ import annotations
import argparse, collections, hashlib, json, math, os, pathlib, re, resource, subprocess, sys, time, urllib.request
from tree_sitter import Language, Parser
import tree_sitter_c
ROOT=pathlib.Path(__file__).resolve().parent
DATA=pathlib.Path('/home/niu/.cache/oskernel-description-interface-witness')
PROTOCOL=json.loads((ROOT/'protocol.json').read_text())
PARSER=Parser(Language(tree_sitter_c.language()))
sys.path.insert(0,str(DATA/'literature/vendor'))
from codewiki.src.be.dependency_analyzer.analyzers.c import analyze_c_file
from oskernel_agent.tools.tool_handlers import read_file
from oskernel_agent.pipeline.tree_builder import _files_for_subsys_prompt, _build_subsys_request


def dump(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def sha(raw): return hashlib.sha256(raw).hexdigest()
def walk(node):
    yield node
    for c in node.children: yield from walk(c)

def sources(task):
    m=json.loads((ROOT/(task['repo']+'-manifest.json')).read_text())
    result={}
    for e in m['files']:
        p=pathlib.Path(m['source_root'])/e['path'];raw=p.read_bytes()
        if sha(raw)!=e['sha256']:raise ValueError('source changed '+e['path'])
        result[e['path']]=raw.decode('utf8',errors='replace')
    return result

def ast_units(src):
    units=[]; functions={}; tables={}
    for path,text in sorted(src.items()):
        tree=PARSER.parse(text.encode()); lines=text.splitlines()
        for n in walk(tree.root_node):
            if n.type=='function_definition':
                d=n.child_by_field_name('declarator')
                fd=next((x for x in walk(d) if x.type=='function_declarator'),None) if d else None
                ident=fd.child_by_field_name('declarator') if fd else None
                if not ident:continue
                name=ident.text.decode();start=n.start_point[0]+1;end=n.end_point[0]+1
                unit={'path':path,'start':start,'end':end,'name':name,'text':'\n'.join(lines[start-1:end])}
                units.append(unit);functions.setdefault(name,[]).append(unit)
            elif n.type=='declaration':
                for d in n.named_children:
                    if d.type!='init_declarator':continue
                    ident=d.child_by_field_name('declarator');value=d.child_by_field_name('value')
                    if not ident or ident.type!='identifier' or not value or value.type!='initializer_list':continue
                    start=n.start_point[0]+1;end=n.end_point[0]+1;name=ident.text.decode()
                    unit={'path':path,'start':start,'end':end,'name':name,'text':'\n'.join(lines[start-1:end])}
                    pairs=[]
                    for pair in value.named_children:
                        if pair.type!='initializer_pair':continue
                        des=pair.child_by_field_name('designator');rhs=pair.child_by_field_name('value')
                        if not des or not rhs:continue
                        f=next((x for x in walk(des) if x.type=='field_identifier'),None)
                        v=rhs.text.decode().strip().lstrip('&').strip()
                        if f and re.fullmatch(r'[A-Za-z_]\w*',v):pairs.append((f.text.decode(),v,pair.start_point[0]+1))
                    tables[(path,name)]=(unit,pairs);units.append(unit)
    return units,functions,tables

def ctags_defs(src,root):
    cmd=['ctags','--output-format=json','--fields=+neK','--kinds-C=f','-f','-']+[str(root/p) for p in sorted(src)]
    r=subprocess.run(cmd,capture_output=True,text=True,timeout=45,check=True)
    result={}
    for line in r.stdout.splitlines():
        x=json.loads(line)
        if x.get('kind')!='function':continue
        x['path']=pathlib.Path(x['path']).relative_to(root).as_posix();result.setdefault(x['name'],[]).append(x)
    return result

def choose_def(defs,name,path):
    candidates=defs.get(name,[]);local=[x for x in candidates if x['path']==path]
    return local[0] if len(local)==1 else (candidates[0] if len(candidates)==1 else None)

def oracle(task,src):
    """Separate regex projection + compiled original assignments + Ctags spans.
    Not a build or behavioral execution of the OS. Preserve every projected clause.
    """
    text=src[task['path']];match=re.search(r'\b'+re.escape(task['table'])+r'\s*=\s*\{',text)
    if not match:raise ValueError('reference table absent')
    start=match.end();end=text.find('}',start);body=text[start:end]
    if '{' in body:raise ValueError('unsupported nested reference initializer')
    clauses=[]
    for m in re.finditer(r'\.([A-Za-z_]\w*)\s*=\s*(&?\s*[A-Za-z_]\w*|0)\s*,',body):
        field,rhs=m.group(1),m.group(2);symbol=rhs.replace('&','').strip()
        if symbol in ['NULL','RT_NULL']:rhs='0';symbol='0'
        clauses.append({'role':field,'raw':'.'+field+' = '+rhs+',','rhs':symbol,'binding_line':text[:start+m.start()].count('\n')+1})
    if not clauses:raise ValueError('no reference clauses')
    ids=sorted({x['rhs'] for x in clauses if x['rhs']!='0'})
    # Each distinct stub has an observable address; compiler executes assignments.
    code='#include <stdio.h>\n'+''.join('static void '+s+'(void) {}\n' for s in ids)
    code+='struct projected_ops {'+''.join('void (*'+x['role']+')(void);' for x in clauses)+'};\n'
    code+='static struct projected_ops ops = {\n'+ '\n'.join(x['raw'] for x in clauses)+'\n};\nint main(void) {\n'
    for x in clauses:
        f=x['role'];code+='if (!ops.'+f+') puts("'+f+'=NULL");\n'
        for s in ids:code+='if (ops.'+f+' == '+s+') puts("'+f+'='+s+'");\n'
    code+='return 0;}\n'
    folder=ROOT/'references'/task['repo'];folder.mkdir(parents=True,exist_ok=True);p=folder/'projection.c';p.write_text(code)
    binary=DATA/'executables'/task['repo'];binary.parent.mkdir(exist_ok=True)
    cmd=['/usr/bin/x86_64-linux-gnu-gcc-12','-std=c11','-O0','-Wall','-Wextra','-Werror',str(p),'-o',str(binary)]
    r=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
    dump(folder/'compile.json',{'command':cmd,'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr,'source_sha256':sha(p.read_bytes())})
    if r.returncode:raise ValueError('oracle compile failed')
    runs=[subprocess.check_output([str(binary)],text=True,timeout=5) for _ in range(2)]
    if runs[0]!=runs[1]:raise ValueError('unstable oracle')
    actual=dict(line.split('=',1) for line in runs[0].splitlines())
    if len(actual)!=len(clauses):raise ValueError('incomplete oracle')
    defs=ctags_defs(src,DATA/task['repo']);facts=[]
    for c in clauses:
        d=choose_def(defs,actual[c['role']],task['path'])
        facts.append(dict(c,implementation=actual[c['role']],binding_path=task['path'],definition=d))
    dump(folder/'facts.json',{'task':task,'observations':runs,'facts':facts,'ctags_version':subprocess.check_output(['ctags','--version'],text=True).splitlines()[0]})
    return facts,defs

def render_unit(u,src=None,window=None):
    lines=u['text'].splitlines() if src is None else src[u['path']].splitlines()[u['start']-1:u['end']]
    if window:lines=lines[:window]
    return '## '+u['path']+'\n'+'\n'.join(f'{u["start"]+i:5d} | {line}' for i,line in enumerate(lines))+'\n'

def pack(texts):
    result='';picked=[]
    for label,text in texts:
        if len((result+text).encode())<=PROTOCOL['source_context_utf8_bytes']:
            result+=text;picked.append(label)
    return result,picked

def tokens(text): return re.findall(r'[a-z0-9]+', re.sub(r'([a-z])([A-Z])',r'\1 \2',text).lower().replace('_',' '))
def bm25(units,query):
    docs=[tokens(u['name']+' '+u['path']+' '+u['text']) for u in units];df=collections.Counter(t for d in docs for t in set(d));avg=sum(map(len,docs))/max(1,len(docs));qs=tokens(query)
    def score(i):
        tf=collections.Counter(docs[i]);dl=len(docs[i]);return sum(math.log(1+(len(docs)-df[q]+.5)/(df[q]+.5))*tf[q]*2.5/(tf[q]+1.5*(.25+.75*dl/max(1,avg))) for q in set(qs) if tf[q])
    return [units[i] for i in sorted(range(len(units)),key=lambda i:(-score(i),units[i]['path'],units[i]['start']))]

def contexts(task,src,facts,defs):
    units,functions,tables=ast_units(src);target,pairs=tables[(task['path'],task['table'])]
    query=task['table']+' filesystem file operations open close read write seek directory flush poll'
    lexical,_=pack([(u['name'],render_unit(u)) for u in bm25(units,query)])
    official=[]
    for path,text in sorted(src.items()):
        nodes,_=analyze_c_file(str(DATA/task['repo']/path),text,str(DATA/task['repo']))
        for n in nodes:official.append({'name':n.name,'path':path,'start':n.start_line,'end':n.end_line,'text':n.source_code or ''})
    cw,_=pack([(u['name'],render_unit(u)) for u in bm25(official,query)])
    # Candidate: include the ownership site, then the definitions named by it.
    # No generated facts are injected; all context consists of unmodified source.
    candidate=[(target['name'],render_unit(target))]
    for role,name,line in pairs:
        d=choose_def(functions,name,task['path'])
        if d:candidate.append((name,render_unit(d,window=8)))
    wit,_=pack(candidate)
    # Strong simple baseline independently locates the table by text and Ctags.
    text=src[task['path']];m=re.search(r'\b'+re.escape(task['table'])+r'\s*=\s*\{',text)
    begin=text.rfind('\n',0,m.start())+1;finish=text.find(';',m.end())+1
    start=text[:begin].count('\n')+1;end=text[:finish].count('\n')+1
    simple=[(task['table'],render_unit({'path':task['path'],'start':start,'end':end,'text':'\n'.join(text.splitlines()[start-1:end])}))]
    body=text[m.end():text.find('}',m.end())]
    for m2 in re.finditer(r'\.\w+\s*=\s*&?\s*(\w+)\s*,',body):
        name=m2.group(1);d=choose_def(defs,name,task['path'])
        if d:simple.append((name,render_unit({'path':d['path'],'start':d['line'],'end':d.get('end',d['line'])},src,window=8)))
    look,_=pack(simple)
    dump(ROOT/'prepared'/task['repo']/'inventory.json',{'ast_units':len(units),'official_components':len(official),'official_retained_target_table':any(u['name']==task['table'] for u in official),'source_files':len(src)})
    return {'bm25_chunks':lexical,'codewiki_components':cw,'interface_witness':wit,'simple_lookup':look},functions

class Client:
    def __init__(self):
        self.conf=json.loads(pathlib.Path('/home/niu/.config/oskernel-research/deepseek.json').read_text());self.ledger_path=ROOT/'ledger.json';self.ledger=json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else [];self.cache={}
        for x in self.ledger:
            if x.get('status')=='ok':self.cache[(x['repeat'],x['request_sha256'])]=x
    def call(self,messages,max_tokens,task,arm,repeat,phase):
        body={'model':PROTOCOL['model'],'messages':messages,'max_tokens':max_tokens,'temperature':0.2,'thinking':{'type':'disabled'},'response_format':{'type':'json_object'}}
        raw=json.dumps(body,ensure_ascii=False).encode();h=sha(raw);key=(repeat,h)
        if key in self.cache:
            old=self.cache[key];return json.loads(pathlib.Path(old['response_path']).read_text()),{'reused_call':old['id']}
        total=sum((x.get('usage') or {}).get('total_tokens',0) for x in self.ledger)
        if len(self.ledger)>=PROTOCOL['request_limit'] or total+len(raw)+max_tokens>PROTOCOL['actual_token_limit']:raise RuntimeError('budget exhausted')
        if any(x.get('status')=='usage_missing' for x in self.ledger):raise RuntimeError('unmeasured provider usage; stop budget risk')
        i=len(self.ledger)+1;folder=ROOT/'calls'/f'{i:03d}';dump(folder/'request.json',body)
        row={'id':i,'repo':task['repo'],'arm':arm,'repeat':repeat,'phase':phase,'request_sha256':h,'request_path':str(folder/'request.json'),'response_path':str(folder/'response.json'),'status':'started'}
        self.ledger.append(row);dump(self.ledger_path,self.ledger);start=time.monotonic()
        try:
            req=urllib.request.Request(self.conf['base_url'].rstrip('/')+'/chat/completions',data=raw,headers={'Authorization':'Bearer '+self.conf['api_key'],'Content-Type':'application/json'})
            response=json.load(urllib.request.urlopen(req,timeout=PROTOCOL['request_timeout_seconds']));dump(folder/'response.json',response)
            row.update(status='ok' if response.get('usage') else 'usage_missing',usage=response.get('usage'),model=response.get('model'),finish_reason=response['choices'][0].get('finish_reason'));self.cache[key]=row
        except Exception as e:
            row.update(status='api_failure',error_type=type(e).__name__,error=str(e));response=None
        row['elapsed_seconds']=round(time.monotonic()-start,3);dump(self.ledger_path,self.ledger)
        print(f'call {i} {task["repo"]}/{arm}/r{repeat}/{phase}: {row["status"]} tokens={(row.get("usage") or {}).get("total_tokens")}',flush=True)
        return response,{'call':i}
    def balance(self):
        req=urllib.request.Request(self.conf['base_url'].rstrip('/')+'/user/balance',headers={'Authorization':'Bearer '+self.conf['api_key']})
        return json.load(urllib.request.urlopen(req,timeout=20))

def parse_response(response):
    if response is None:return None,'api_failure'
    if response['choices'][0].get('finish_reason')=='length':return None,'output_budget_exhausted'
    try:
        x=json.loads(response['choices'][0]['message']['content'])
        if not isinstance(x,dict):raise ValueError('not object')
        return x,'ok'
    except Exception:return None,'format_failure'

def production_context(client,task,src,functions,repeat):
    files=[{'path':p,'name':pathlib.Path(p).name,'lang':'c'} for p in sorted(src)]
    prompt_files=_files_for_subsys_prompt(files)
    node={'name':'文件系统','files':files}
    # Keep the original request for auditing; output schema is intentionally adapted.
    request=_build_subsys_request(node,DATA/task['repo'],{'content_path':'unused','json_path':'unused','module_paths':[]},None)
    (ROOT/'prepared'/task['repo']/'original-production-request.txt').write_text(request)
    default=read_file(str(DATA/task['repo']),task['path'])
    names=[f'{u["name"]} {u["path"]}:{u["start"]}-{u["end"]}' for xs in functions.values() for u in xs if u['path']==task['path']]
    msg='描述文件系统模块，先收集源码。生产策略按目录/语言提示文件，读文件默认头80尾40，范围最多200行，工具读取最多12次。给出最有用的读取请求，保留中间段和实现依据。只返回 JSON {"reads":[{"path":"...","start_line":整数或null,"end_line":整数或null}]}。最多12项。所有文件可读取，但总返回仅10000 UTF-8字节。\n目标表：'+task['table']+'\n提示文件：'+json.dumps(prompt_files,ensure_ascii=False)+'\n目标文件符号：\n'+'\n'.join(names)+'\n默认读取：\n'+default
    response,ref=client.call([{'role':'user','content':msg}],PROTOCOL['production_selection_max_tokens'],task,'production_reader',repeat,'allocation')
    x,status=parse_response(response);requests=(x or {}).get('reads')
    if status!='ok' or not isinstance(requests,list):return '',{'allocation_status':status if status!='ok' else 'format_failure',**ref}
    parts=[];errors=[]
    for q in requests[:PROTOCOL['production_max_reads']]:
        if not isinstance(q,dict) or q.get('path') not in src:errors.append('invalid_path');continue
        try:
            v=read_file(str(DATA/task['repo']),q['path'],q.get('start_line'),q.get('end_line'))
            parts.append((q,v+'\n'))
        except Exception as e:errors.append(type(e).__name__)
    text,picked=pack(parts)
    return text,{'allocation_status':status,'requested_reads':requests,'retained_reads':picked,'tool_errors':errors,**ref}

PROMPT='''你为 OS 评委描述指定文件系统模块的接口实现。仅使用附带源码，描述操作表每个显式函数绑定由哪一个实现承接；不把静态绑定写成运行可用性或完整接口兼容。不依据记忆补充。不要描述源码条件、修复摘要或评分。
返回 JSON：{"module_description":"中文模块职责，最多160字","claims":[{"role":"源码操作表字段名","implementation":"被绑定的函数名；不明为unknown","binding":"表赋值的path:line，不明为unknown","definition":"实现函数内path:line，不明为unknown","description":"一句中文接口职责，最多70字"}]}。
覆盖能核实的显式函数字段；不要把名称字符串、嵌套子表作为函数。未知与遗漏分别记录，不得编造。所有字符串值累计最多4500字符，无额外详情。绑定引用和实现引用必须分别来自所给材料。源码片段可能不完整，这时应明确unknown。'''

def score(task,facts,value,status,context):
    result={'status':status,'reference_bindings':len(facts),'correct':0,'wrong':0,'unknown':0,'omitted':len(facts),'complete_evidence':0,'unsupported_extra_claims':0,'duplicate_claims':0}
    if status!='ok':return result
    claims=value.get('claims');summary=value.get('module_description')
    if not isinstance(claims,list) or not isinstance(summary,str):result['status']='format_failure';return result
    chars=sum(len(v) for v in [summary]+[v for q in claims if isinstance(q,dict) for v in q.values() if isinstance(v,str)])
    result['output_string_characters']=chars
    if chars>PROTOCOL['output_string_characters']:result['status']='output_budget_violation';return result
    index={f['role']:f for f in facts};seen=set()
    lines={}
    path=None
    for line in context.splitlines():
        if line.startswith('## '):path=line[3:].strip().split('  行')[0].split('（')[0].strip()
        m=re.match(r'\s*(\d+) \| (.*)',line)
        if m and path:lines[(path,int(m.group(1)))]=m.group(2)
    for c in claims:
        if not isinstance(c,dict):result['status']='format_failure';return result
        role=c.get('role')
        if role not in index:result['unsupported_extra_claims']+=1;continue
        if role in seen:result['duplicate_claims']+=1;continue
        seen.add(role);f=index[role];impl=c.get('implementation')
        if impl=='unknown' or not impl:result['unknown']+=1;continue
        if impl!=f['implementation']:result['wrong']+=1;continue
        result['correct']+=1
        b=re.fullmatch(r'(.+):(\d+)',str(c.get('binding','')));d=re.fullmatch(r'(.+):(\d+)',str(c.get('definition','')))
        bind_ok=b and b.group(1)==f['binding_path'] and int(b.group(2))==f['binding_line'] and (b.group(1),int(b.group(2))) in lines
        fd=f['definition'];def_ok=d and fd and d.group(1)==fd['path'] and fd['line']<=int(d.group(2))<=fd.get('end',fd['line']) and (d.group(1),int(d.group(2))) in lines
        if bind_ok and def_ok:result['complete_evidence']+=1
    result['omitted']=len(facts)-len(seen)
    return result

def prepare():
    for task in PROTOCOL['tasks']:
        src=sources(task);facts,defs=oracle(task,src);ctx,funcs=contexts(task,src,facts,defs)
        folder=ROOT/'prepared'/task['repo']
        for arm,text in ctx.items():(folder/(arm+'.txt')).write_text(text)
        default=read_file(str(DATA/task['repo']),task['path']);(folder/'production-default-read.txt').write_text(default)
        dump(folder/'witness.json',{'table':task['table'],'binding_lines':[x['binding_line'] for x in facts],'default_read_contains_bindings':[f'{x["binding_line"]:5d} |' in default for x in facts],'ast_simple_equal':ctx['interface_witness']==ctx['simple_lookup'],'context_bytes':{k:len(v.encode()) for k,v in ctx.items()}})
        print(task['repo'],'reference',len(facts),'context bytes',{k:len(v.encode()) for k,v in ctx.items()},flush=True)

def freeze():
    files={}
    for p in ROOT.rglob('*'):
        if p.is_file() and p.name!='freeze.json' and not any(s in p.parts for s in ['calls','runs','__pycache__']):files[str(p.relative_to(ROOT))]=sha(p.read_bytes())
    for p in [pathlib.Path('src/oskernel_agent/tools/tool_handlers.py'),pathlib.Path('src/oskernel_agent/pipeline/tree_builder.py')]:files[str(p.resolve())]=sha(p.read_bytes())
    dump(ROOT/'freeze.json',{'frozen_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'files':files,'git_head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'python':sys.version})

def run():
    frozen=json.loads((ROOT/'freeze.json').read_text())
    for p,h in frozen['files'].items():
        path=pathlib.Path(p) if p.startswith('/') else ROOT/p
        if sha(path.read_bytes())!=h:raise ValueError('freeze mismatch '+p)
    client=Client();dump(ROOT/'balance-before.json',client.balance());results=[];start=time.monotonic();cpu0=resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        for task in PROTOCOL['tasks']:
            src=sources(task);facts=json.loads((ROOT/'references'/task['repo']/'facts.json').read_text())['facts'];_,functions,_=ast_units(src)
            for repeat in range(PROTOCOL['repeats']):
                for arm in PROTOCOL['arms']:
                    folder=ROOT/'runs'/task['repo']/str(repeat)/arm
                    if (folder/'result.json').exists():results.append(json.loads((folder/'result.json').read_text()));continue
                    if len(client.ledger)%10==0:
                        balance=client.balance();available=sum(float(x['total_balance']) for x in balance.get('balance_infos',[]) if x['currency']=='CNY')
                        if available<.35:raise RuntimeError('balance floor')
                    if arm=='production_reader':context,allocation=production_context(client,task,src,functions,repeat)
                    else:context=(ROOT/'prepared'/task['repo']/(arm+'.txt')).read_text();allocation=None
                    folder.mkdir(parents=True,exist_ok=True);(folder/'context.txt').write_text(context)
                    response,ref=client.call([{'role':'system','content':PROMPT},{'role':'user','content':f'目标模块表 {task["table"]}，路径 {task["path"]}。\n源码材料：\n'+context}],PROTOCOL['output_max_tokens'],task,arm,repeat,'description')
                    value,status=parse_response(response);evaluation=score(task,facts,value,status,context)
                    result={'task':task,'repeat':repeat,'arm':arm,'context_bytes':len(context.encode()),'context_sha256':sha(context.encode()),'allocation':allocation,'call_ref':ref,'output':value,'evaluation':evaluation}
                    dump(folder/'result.json',result);results.append(result);dump(ROOT/'results.json',results)
    except RuntimeError as e:dump(ROOT/'stop.json',{'reason':str(e),'completed_cells':len(results)})
    finally:
        cpu1=resource.getrusage(resource.RUSAGE_CHILDREN);dump(ROOT/'balance-after.json',client.balance());dump(ROOT/'run-cost.json',{'wall_seconds':round(time.monotonic()-start,3),'subprocess_cpu_seconds_run_only':cpu1.ru_utime+cpu1.ru_stime-cpu0.ru_utime-cpu0.ru_stime,'model_requests':len(client.ledger),'actual_tokens':sum((x.get('usage') or {}).get('total_tokens',0) for x in client.ledger),'usage_missing_calls':[x['id'] for x in client.ledger if x.get('status')=='usage_missing']})
    print('completed',len(results),'of',len(PROTOCOL['tasks'])*PROTOCOL['repeats']*len(PROTOCOL['arms']),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','freeze','run']);args=p.parse_args();globals()[args.action]()
