"""Prepare an unchanged-header compiler oracle and a narrow algebraic prototype."""
from __future__ import annotations
import pathlib, json, hashlib, subprocess, time, resource, re
P=pathlib.Path(__file__).resolve().parent
HEADER=P/'sources/chalign.h'
NAMES=['MEM_ALIGN_MASK','MEM_ALIGN_PREV','MEM_ALIGN_NEXT','MEM_IS_ALIGNED','MEM_IS_VALID_ALIGNMENT']
CASES=[('MEM_ALIGN_MASK',16,None),('MEM_ALIGN_PREV',65,32),('MEM_ALIGN_NEXT',64,32),('MEM_ALIGN_NEXT',65,32),('MEM_ALIGN_PREV',4294967321,16),('MEM_ALIGN_NEXT',4294967321,16),('MEM_ALIGN_NEXT',18446744073709551608,16),('MEM_ALIGN_PREV',18446744073709551608,16),('MEM_IS_ALIGNED',64,32),('MEM_IS_ALIGNED',65,32),('MEM_IS_VALID_ALIGNMENT',0,None),('MEM_IS_VALID_ALIGNMENT',48,None)]

def dump(q,v):q.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')

def macros(text):
 from tree_sitter import Language,Parser
 import tree_sitter_c
 root=Parser(Language(tree_sitter_c.language())).parse(text.encode()).root_node
 out={}
 def visit(n):
  if n.type=='preproc_function_def':
   name=n.child_by_field_name('name'); val=n.child_by_field_name('value'); params=n.child_by_field_name('parameters')
   out[name.text.decode()]={'body':val.text.decode(),'parameters':params.text.decode(),'line':name.start_point[0]+1}
  for c in n.children:visit(c)
 visit(root)
 return out

def normalized_note(data):
 # Narrow recognizer: never infer a formula for an unsupported expression.
 bodies={n:re.sub(r'/\*.*?\*/','',r['body'],flags=re.S).replace('\\\n','').replace(' ','').replace('\n','') for n,r in data.items()}
 assert bodies['MEM_ALIGN_MASK']=='((size_t)(a)-1U)'
 assert bodies['MEM_ALIGN_PREV']=='((size_t)(p)&~MEM_ALIGN_MASK(a))'
 assert bodies['MEM_ALIGN_NEXT']=='MEM_ALIGN_PREV((size_t)(p)+MEM_ALIGN_MASK(a),(a))'
 return ('代数重表达（限定无副作用整数实参、原生 size_t 64 位，a 为有效对齐数）：'
         'MEM_ALIGN_MASK(a)=a-1；MEM_ALIGN_PREV(p,a)=floor((p mod 2^64)/a)*a；'
         'MEM_ALIGN_NEXT(p,a)=floor(((p+a-1) mod 2^64)/a)*a。'
         'floor 表示数学向下取整。两个检测宏仍按原始布尔表达式理解。'
         '此处只给出标量地址表示，不证明实际内存访问、OS 能力或所有 C 实参形式。')

def main():
 t=time.monotonic(); before=resource.getrusage(resource.RUSAGE_CHILDREN)
 data=macros(HEADER.read_text());assert set(data)==set(NAMES);dump(P/'macro-source-spans.json',data)
 note=normalized_note(data);(P/'normal-form-note.txt').write_text(note)
 # Full original chalign.h is compiled, not a handwritten reimplementation.
 src=['#include <stddef.h>','#include <stdint.h>','#include <stdio.h>','#include "sources/chalign.h"','int main(void){','printf("BITS %zu\\n",sizeof(size_t)*8);']
 for i,(name,x,a) in enumerate(CASES):
  args=f'((size_t)UINT64_C({x}))'+(f', ((size_t)UINT64_C({a}))' if a is not None else '')
  src.append(f'printf("Q{i+1} %zu\\n",(size_t){name}({args}));')
 # Additional examples are not scored cases; every arm gets the same source.
 for i,(name,x,a) in enumerate([('MEM_ALIGN_PREV',7,4),('MEM_ALIGN_NEXT',7,4),('MEM_ALIGN_NEXT',8,4),('MEM_IS_VALID_ALIGNMENT',8,None)]):
  args=str(x)+(f',{a}' if a is not None else '')
  src.append(f'printf("X{i+1} %zu\\n",(size_t){name}({args}));')
 src.append('for(size_t a=1;a<=256;a*=2){for(size_t p=0;p<65536;p++){printf("V %zu %zu %zu %zu\\n",p,a,MEM_ALIGN_PREV(p,a),MEM_ALIGN_NEXT(p,a));}} return 0;}')
 (P/'oracle.c').write_text('\n'.join(src)+'\n')
 command=['/usr/bin/gcc-12','-std=c11','-O0','-Wall','-Wextra','-Werror',str(P/'oracle.c'),'-o',str(P/'oracle')]
 result=subprocess.run(command,cwd=P,capture_output=True,text=True);(P/'compiler.stdout.txt').write_text(result.stdout);(P/'compiler.stderr.txt').write_text(result.stderr);assert result.returncode==0
 raw=subprocess.check_output([str(P/'oracle')],cwd=P);digest=hashlib.sha256(raw).hexdigest();lines=raw.decode().splitlines();assert lines[0]=='BITS 64'
 answers=[int(lines[i+1].split()[1]) for i in range(12)]
 extra=[int(lines[i+13].split()[1]) for i in range(4)]
 bad=[];count=0
 for line in lines[17:]:
  _,x,a,lo,hi=line.split();x,a,lo,hi=map(int,[x,a,lo,hi]);count+=1
  if (lo,hi)!=(x//a*a,((x+a-1)%(1<<64))//a*a):bad.append(line)
 assert not bad
 # Distinct boundary cases are checked against compiled source, not another LLM.
 for (n,x,a),v in zip(CASES,answers):
  if n=='MEM_ALIGN_PREV':assert v==(x%(1<<64))//a*a
  if n=='MEM_ALIGN_NEXT':assert v==((x+a-1)%(1<<64))//a*a
 ids={n:'E'+hashlib.sha256(n.encode()).hexdigest()[:10] for n in NAMES}
 questions=[{'id':i+1,'symbol':ids[n],'arguments':[x]+([a] if a is not None else [])} for i,(n,x,a) in enumerate(CASES)]
 dump(P/'questions.json',questions);dump(P/'oracle-answers.json',{'answers':answers,'scope':'12 scalar observations of original macros at native size_t 64; not all prose correctness','source_sha256':hashlib.sha256(HEADER.read_bytes()).hexdigest(),'compiler_raw_output_sha256':digest})
 (P/'gcc-example-note.txt').write_text('原宏编译执行例子（无副作用整数实参、原生 size_t 64 位）：MEM_ALIGN_PREV(7,4)='+str(extra[0])+'；MEM_ALIGN_NEXT(7,4)='+str(extra[1])+'；MEM_ALIGN_NEXT(8,4)='+str(extra[2])+'；MEM_IS_VALID_ALIGNMENT(8)='+str(extra[3])+'。这些是有限例子，不代表任意输入。')
 template='本模块提供内存对齐宏。以下保留原 C 标量表达式；size_t 为原生 64 位无符号数，整数实参无副作用。a 的有效对齐值为 2 的幂；地址值是数值表示，不是实际访问验证。\n'
 for n in NAMES:
  body=re.sub(r'/\*.*?\*/','',data[n]['body'],flags=re.S).replace('\\\n',' ').strip()
  template+=n+data[n]['parameters']+'='+re.sub(r'\s+',' ',body)+' [sources/chalign.h:'+str(data[n]['line'])+']\n'
 assert len(template)<=900;(P/'template-report.md').write_text(template)
 dump(P/'symbol-mask.json',ids);(P/'names-only-report.md').write_text('size_t 为原生 64 位；整数实参无副作用。\n'+'\n'.join(ids.values()))
 after=resource.getrusage(resource.RUSAGE_CHILDREN)
 dump(P/'preprocessing.json',{'compiler_command':command,'compiler_version':subprocess.check_output(['/usr/bin/gcc-12','--version'],text=True).splitlines()[0],'complete_original_header':True,'macro_names':NAMES,'exhaustive_projection_pairs':count,'projection_mismatches':len(bad),'all_64bit_values_proved':False,'math_identities':'standard power-of-two mask/division identity; narrow recognizer, full machine range not exhaustively verified','boundary_observations':12,'execution_stdout_bytes':len(raw),'execution_stdout_sha256':digest,'wall_seconds':time.monotonic()-t,'cpu_seconds_child_delta':after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,'quality_claim':'preprocessing checks alone are engineering evidence, not a report quality effect','template_characters':len(template),'note_characters':len(note)})
 print(json.dumps(json.loads((P/'preprocessing.json').read_text()),ensure_ascii=False))
if __name__=='__main__':main()
