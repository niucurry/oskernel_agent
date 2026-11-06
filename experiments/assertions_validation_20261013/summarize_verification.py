"""Summarize existing experiment artifacts without calling the API again."""
import hashlib
import json
from pathlib import Path
from oskernel_agent.analysis.evidence_plan import check_evidence_plan
from oskernel_agent.reports.html_tree import _render_description_scope, _render_claim_reviews

root = Path(__file__).resolve().parent

def read(path):
    return json.loads((root / path).read_text())

before = read('source-before.json')
after = {name: hashlib.sha256((root / 'repo' / name).read_bytes()).hexdigest() for name in before}
assert before == after
oracle = read('oracle.json')
assert oracle == {'total_calls': 4, 'additional_retries': 3, 'result': -1}
plan = read('generated/plan.snapshot.json')
normal = read('generated/assertions.json')
injected = read('injected-claim/before-check.json')
final = read('injected-claim/assertions.json')
assert injected['items'][0]['status'] == 'ready_for_review'
assert injected['items'][0]['claim_id'] != final['items'][0]['claim_id']
assert injected['report_sha256'] != final['report_sha256']
assert normal['snapshot_sha256'] == injected['snapshot_sha256'] == final['snapshot_sha256']
outputs = {}
for key, assertions in [('generated', normal), ('injected-claim', final)]:
    assert assertions['shape_errors'] == []
    row = assertions['items'][0]
    assert row['status'] == 'supported' and row['errors'] == []
    assert row['execution_status'] == 'not_run' and row['score_status'] == 'not_computed'
    audit = json.loads(Path(assertions['read_artifact']).read_text())
    acquisition = check_evidence_plan(root / 'repo', plan, audit)
    assert all(i['status'] == 'returned' and i['returned_count'] == i['required_count'] == 3 for i in acquisition['items'])
    review = read(key + '/review.json')
    selected = next(c for c in review['claims'] if c['kind'] == 'evaluation_assertion')
    assert selected['id'] == row['claim_id'] and selected['independent_sessions']
    assert len(selected['reviews']) == 2 and all(r['verdict'] == 'supported' and not r['errors'] for r in selected['reviews'])
    outputs[key] = {
        'assertions_artifact': str(root / key / 'assertions.json'),
        'statement': row['statement'], 'claim_id': row['claim_id'],
        'report_sha256': assertions['report_sha256'], 'status': row['status'],
        'execution_status': row['execution_status'], 'score_status': row['score_status'],
        'required_evidence_returned': {'returned': 3, 'required': 3},
        'author_run_id': audit['run_id'], 'author_sessions': audit['session_ids'],
        'revision': review.get('revision'),
    }
    sessions = []
    for checked in sorted((root / key / 'reviews').glob('**/checked.json')):
        d = json.loads(checked.read_text())
        rr = [r for r in d['reviews'] if r['claim_id'] in {row['claim_id'], injected['items'][0]['claim_id']}]
        a = json.loads(Path(d['artifact']).read_text())
        assert a['stream_status'] == 'complete'
        sessions.append({
            'phase': checked.parent.parent.name, 'role': checked.parent.name,
            'run_id': d['run_id'], 'session_ids': d['session_ids'], 'artifact': str(checked),
            'assertion_reviews': [{k: r[k] for k in ['claim_id', 'verdict', 'model_verdict', 'errors']} for r in rr],
        })
    ids = [s for x in sessions for s in x['session_ids']]
    assert len(ids) == len(set(ids))
    outputs[key]['independent_review_sessions'] = sessions
fault_review = read('injected-claim/review.json')
initial = next(c for c in fault_review['initial'] if c['kind'] == 'evaluation_assertion')
assert initial['status'] == 'insufficient'
assert [r['verdict'] for r in initial['reviews']] == ['contradicted', 'insufficient']
assert all(r['model_verdict'] == 'contradicted' for r in initial['reviews'])
assert initial['reviews'][1]['errors'] == ['unverified_evidence']
failed_tools = []
tools_seen = set()
for file in sorted(root.glob('*/receipts/**/audit.json')):
    a = json.loads(file.read_text())
    for c in a['calls']:
        if c['status'] == 'completed':
            tools_seen.add(c['tool'])
        if c['status'] == 'error':
            failed_tools.append({
                'artifact': str(file), 'run_id': a['run_id'], 'tool': c['tool'],
                'subsequent_report_write_completed': any(x['tool'] == 'os-kernel-tools_write_report' and x['status'] == 'completed' for x in a['calls']),
            })
summary = {
    'date': '2026-10-13', 'model': 'deepseek-flash',
    'purpose': '评价项事实断言的来源检查、独立源码复核和有限修订；不是准确率评测',
    'source_hashes_before': before, 'source_hashes_after': after, 'source_unchanged': True,
    'normal_generation': outputs['generated'],
    'controlled_wrong_statement': {
        'method': '只修改报告中的计数断言，保留真实源码引文及生成会话的实际读取记录，未修改示例源码；不是自然发生的模型计数错误',
        'before_semantic_review_status': injected['items'][0]['status'],
        'initial_combined_status': initial['status'],
        'initial_review_verdicts': [{k: r[k] for k in ['verdict', 'model_verdict', 'errors']} for r in initial['reviews']],
        'invalid_quote': '第二份初始复核标注 request.c:10-18，但引文包含第 19 行，宿主拒绝该次结论作为有效反驳',
        'final': outputs['injected-claim'],
    },
    'successful_tools_seen': sorted(tools_seen), 'tool_errors': failed_tools,
    'external_experiment_oracle': {
        'artifact': str(root / 'oracle.json'), 'source': str(root / 'oracle.c'),
        'compiler': '/usr/bin/x86_64-linux-gnu-gcc-13',
        'flags': ['-std=c11', '-Wall', '-Wextra', '-Werror'], 'observed': oracle,
        'integrated_in_production': False, 'provided_to_model': False,
    },
    'regression': {
        'passed': 328, 'skipped': 0, 'elapsed_seconds': 16.84,
        'description': '本轮已运行的相关回归套件，包含原生 OpenCode 审计和报告/PDF 回归',
    },
    'limitations': ['未自动执行断言验证测试', '未从核验断言计算原有六维评分',
                    '只验证一个小型计数示例，不证明比赛仓库评价准确率', '同一模型的不同会话不等于统计独立'],
}
(root / 'verification-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>事实断言验证预览</title>
<style>body{font:16px/1.65 sans-serif;max-width:1250px;margin:32px auto;padding:0 20px;color:#17232e}table{border-collapse:collapse;width:100%;table-layout:fixed}th,td{border:1px solid #bfcbd5;padding:12px;vertical-align:top;overflow-wrap:anywhere}th{background:#eef3f7}h1{font-size:28px}h2{margin-top:36px}li{margin:6px 0}</style>
<h1>评价项事实断言：来源核对与语义复核</h1>
<p>实验条件：DEFAULT_RETRIES=3，每次请求均失败，循环从 0 开始且包含上界。下方使用项目现有报告组件渲染。外部实验程序观察到 4 次总调用、3 次额外重试；自动执行验证尚未接入。</p>
<h2>人工放入错误结论后，只有来源核对通过</h2><p>下面的错误数字和真实引文同时存在，因此“来源核对通过”不能解释为“结论正确”。</p>'''
page += _render_description_scope({'tree': {'children': [{'name': '受控错误草稿', 'evaluation_assertions': injected}]}})
page += '<h2>独立复核发现错误，一轮修订后的最终结果</h2><p>初始一份有效反驳；另一份因引文行范围错误被降为证据不足。修订后两次新会话均支持 4 次总调用、3 次额外重试。</p>'
final_tree = {'tree': {'children': [{'name': '修订后', 'evaluation_assertions': final, 'claim_review': fault_review}]}}
page += _render_description_scope(final_tree) + _render_claim_reviews(final_tree) + '</html>'
(root / 'assertions-preview.html').write_text(page)
assert '执行验证：未运行' in page and '来源核对通过，待语义复核' in page and '两次源码复核支持' in page
print(json.dumps({'source_unchanged': True, 'failed_tool_calls_retained': len(failed_tools), 'successful_tools_seen': sorted(tools_seen), 'summary': str(root / 'verification-summary.json'), 'preview': str(root / 'assertions-preview.html')}, ensure_ascii=False))
