from pathlib import Path
import copy
import json
import time
from oskernel_agent.analysis.evaluation_assertions import check_assertions
from oskernel_agent.engines.claim_review import review_and_revise
from oskernel_agent.engines.evidence_plan import bind_evidence_plan
from oskernel_agent.engines.llm_batch import BatchTask
from oskernel_agent.cli.read_audit import _environment

root = Path('/home/niu/agent-assertions-validation-20261013')
source = root / 'generated'
output = root / 'injected-claim'
output.mkdir(mode=0o700, exist_ok=False)
plan = json.loads((source / 'plan.snapshot.json').read_text())
repo = Path(plan['repo_path'])
report = copy.deepcopy(json.loads((source / 'report.json').read_text()))
report['summary'] = '请求入口及计数配置见 request.c:10-19、config.h:1。当前断言待独立复核。'
claim = report['evaluation_claims'][0]
claim['statement'] = '在默认配置且每次请求均失败的条件下，总共调用 request_attempt 3 次（包括首次尝试），额外重试 2 次。'
claim['counterevidence'] = ['须核对首次尝试是否已计入总次数，以及循环边界是否包含配置的上限。']
claim['limitations'] = '未执行测试，当前断言待独立复核。'
(output / 'injected-draft.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
receipt_path = json.loads((source / 'assertions.json').read_text())['read_artifact']
receipt = json.loads(Path(receipt_path).read_text())
(output / 'experiment.json').write_text(json.dumps({
    'type':'controlled_statement_error_after_generation',
    'explanation':'实验程序改写断言次数，源码与引文保持不变；不是模型自然犯错率样本。',
    'author_read_artifact':receipt_path,
    'oracle_hidden_from_model':str(root / 'oracle.json')},ensure_ascii=False,indent=2))
task = BatchTask(batch_id='scoped-claim-correction', agent_name='evidence-check',
    user_request=f'根据仓库 {repo} 核对并修订 summary 与 evaluation_claims；保留所有必要字段，用 MCP write_report 写到 {output / "report.json"}。',
    output_path=output/'report.json',cache_dir=output,cache_key='',cache_enabled=False,
    repo_path=repo,audit_source_reads=True,read_repair_rounds=2,
    cache_validator=lambda r: bool(r.get('summary')))
bind_evidence_plan(task,plan,with_assertions=True)
task.result_read_audit=receipt
before=check_assertions(repo,plan,report,receipt)
assert before['items'][0]['status']=='ready_for_review'
(output/'before-check.json').write_text(json.dumps(before,ensure_ascii=False,indent=2))
values={'AGENT_OPENCODE_SOURCE_CONFIG_DIR':str(source/'runtime-config'),
        'AGENT_OPENCODE_SOURCE_DATA_DIR':str(output/'auth'),
        'AGENT_READ_AUDIT_DIR':str(output/'receipts'),
        'AGENT_CLAIM_REVIEW_DIR':str(output/'reviews'),
        'OPENCODE_DISABLE_PROJECT_CONFIG':'true','OPENCODE_DISABLE_EXTERNAL_SKILLS':'true',
        'OPENCODE_DISABLE_DEFAULT_PLUGINS':'true','OPENCODE_DISABLE_AUTOUPDATE':'true',
        'OPENCODE_DISABLE_MODELS_FETCH':'true','OPENCODE_DISABLE_LSP_DOWNLOAD':'true'}
start=time.monotonic()
with _environment(values):
    final,review=review_and_revise(task,report,timeout=240)
checked=check_assertions(repo,plan,final,task.result_read_audit,review)
for name,value in [('report',final),('review',review),('assertions',checked)]:
    (output/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2))
summary={'initial':[{'kind':r['kind'],'status':r['status'],'errors':[v['errors'] for v in r['reviews']]} for r in review['initial']],
         'revision':review['revision'],'final':[{'statement':r['statement'],'status':r['status'],'errors':r['errors']} for r in checked['items']],
         'elapsed_seconds':round(time.monotonic()-start,3)}
(output/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
