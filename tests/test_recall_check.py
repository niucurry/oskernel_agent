import copy
import json
from dataclasses import replace

import pytest

from oskernel_agent.analysis import recall_check as checks
from oskernel_agent.engines import llm_batch, recall_check
from oskernel_agent.analysis.read_canary import prepare_canary
from oskernel_agent.analysis.read_coverage import read_requirements

SOURCE = '''int helper(int n) { return n + 1; }
int work(int n) {
    int x = helper(n);
    x += helper(2);
    return x;
}
int second(int n) {
    int x = helper(n);
    return x;
}
int control_a(void) { return 0; }
int control_b(void) { return 0; }
'''


@pytest.mark.parametrize('present,value,expected,returned,valid,want', [
    (True, 'a'*32, 'a'*32, True, True, 'retained_recalled'),
    (False, 'a'*32, 'a'*32, True, True, 'dropped'),
    (False, None, 'a'*32, True, True, 'dropped'),
    (True, None, 'a'*32, True, True, 'present_not_recalled'),
    (True, 'a'*16, 'a'*32, True, True, 'present_not_recalled'),
    (True, 'b'*32, 'a'*32, True, True, 'misattributed'),
    (True, 'c'*32, 'a'*32, True, True, 'present_not_recalled'),
    (None, 'a'*32, 'a'*32, True, True, 'unknown'),
    (False, None, 'a'*32, False, True, 'not_observed'),
    (True, 'a'*32, 'a'*32, True, False, 'unknown'),
    (None, None, None, True, True, 'control_correct'),
    (None, 'a'*32, None, True, True, 'confabulation'),
])
def test_states(present, value, expected, returned, valid, want):
    assert checks.classify(present, value, expected, {'a'*32: 'a', 'b'*32: 'b', 'c'*32: 'a'},
                           'a', returned=returned, valid=valid) == want


def test_clean_report_keeps_lines_and_removes_echo_values():
    token = 'a'*32
    text = 'int f() { /* READ_AUDIT_CANARY:' + token + ' */ \n return 1;\n}'
    value = {'quote': text, 'html': '<p>' + token + '</p>', 'summary': 'READ_AUDIT_CANARY:' + token}
    cleaned = checks.clean(value, [token])
    assert cleaned['quote'] == 'int f() {\n return 1;\n}'
    assert cleaned['quote'].count('\n') == text.count('\n')
    assert token not in json.dumps(cleaned) and 'READ_AUDIT_CANARY' not in json.dumps(cleaned)


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'mm.c').write_text(SOURCE)
    items = checks.inventory(repo)
    audit = {'stream_status': 'complete', 'files': [{'path': 'mm.c', 'sha256': items[0]['sha256'],
                                                   'returned_ranges': [[1, 12]]}]}
    return repo, items, audit


def test_selection_prioritizes_plan_and_citations_including_control_budget(source):
    repo, items, audit = source
    plan = {'items': [{'score_impact': 9, 'evidence': [{'requirements': read_requirements(repo, {'ref': 'mm.c:7'})}]}]}
    result = checks.select_functions(repo, items, items[-2:], audit, plan, {'summary': 'mm.c:2'}, limit=4, seed=3)
    assert result['selected'][0]['symbol'] == 'second'
    assert len(result['selected']) + len(result['controls']) + len({i['path'] for i in result['selected']}) <= 4
    assert result['omitted'][0]['symbol'] == 'work'
    assert result['sample_requested'] == 1


def test_fresh_copies_have_distinct_tokens_and_unchanged_source(source, tmp_path):
    repo, items, _ = source
    manifests = []
    for n in range(2):
        result = prepare_canary(repo, [dict(items[1], confirmed=True, reason='entry')], tmp_path / f'copy{n}',
                                positions=('start', 'mid', 'end'))
        manifest = json.loads(__import__('pathlib').Path(result['manifest']).read_text())
        manifests.append(manifest)
    tokens = [recall_check._tokens(m) for m in manifests]
    assert set(tokens[0]).isdisjoint(tokens[1])
    assert (repo / 'mm.c').read_text() == SOURCE
    for manifest in manifests:
        marked = (__import__('pathlib').Path(manifest['repo']) / 'mm.c').read_text()
        assert checks.clean(marked) == SOURCE


def test_disabled_dispatch_is_byte_identical(source, tmp_path, monkeypatch):
    monkeypatch.delenv('AGENT_RECALL_CHECK', raising=False)
    task = llm_batch.BatchTask('a', 'os-kernel-subsys', 'prompt', tmp_path / 'report.json', tmp_path, '',
                               cache_enabled=False, repo_path=source[0])
    exact = b'{"summary":"plain", "number":7}\n'
    def runner(task, timeout):
        task.output_path.write_bytes(exact)
        return True, '', []
    monkeypatch.setattr(llm_batch, '_run_opencode_once_raw', runner)
    monkeypatch.setattr(recall_check, 'run_once', lambda *a: pytest.fail('disabled hook called'))
    assert llm_batch.run_batch_task(task) == {'summary': 'plain', 'number': 7}
    assert task.output_path.read_bytes() == exact
    assert task.recall_evidence == {} and task.recall_runs == []


def test_targeted_review_includes_both_swapped_functions_outside_normal_budget(source):
    repo, _, _ = source
    reqs = [read_requirements(repo, {'ref': f'mm.c:{line}'}) for line in (2, 7)]
    functions = [dict(r[0], status='misattributed') for r in reqs]
    evidence = {'affected': [checks.identity(f) for f in functions], 'functions': functions}
    claims = [{'id': str(i), 'requirements': reqs[i]} for i in range(2)]
    selection = recall_check.prioritize_claims(evidence, {'selected': [], 'unreviewed': claims})
    assert selection['recall_forced'] == ['0', '1']
    assert len(selection['selected']) == 2 and not selection['unreviewed']


def test_repair_budget_shared_and_no_answers_in_prompt(source, tmp_path, monkeypatch):
    repo, items, audit = source
    monkeypatch.setenv('AGENT_RECALL_CHECK', '1')
    token = 'b'*32
    task = llm_batch.BatchTask('a', 'os-kernel-subsys', 'prompt', tmp_path / 'r.json', tmp_path, '', repo_path=repo,
                               recall_runs=[{'tokens': [token]}], read_evidence={'repair_rounds': 1})
    statuses = iter(['dropped', 'retained_recalled'])
    def check(*args):
        status = next(statuses)
        row = dict(items[1], status=status, actions=['reread', 'claim_review'] if status == 'dropped' else [])
        return {'functions': [row], 'affected': [checks.identity(row)]}
    seen = []
    def run(repair, **kwargs):
        seen.append(repair.user_request)
        assert token not in repair.user_request and 'READ_AUDIT_CANARY' not in repair.user_request
        assert repair.read_repair_rounds == 0
        repair.result_read_audit = audit
        return {'summary': 'mm.c:2', 'score_total': 1}
    monkeypatch.setattr(recall_check, 'check_latest', check)
    monkeypatch.setattr(llm_batch, '_run_batch_task', run)
    result = recall_check.verify(task, {'summary': 'mm.c:2 /* READ_AUDIT_CANARY:' + token + ' */ ', 'score_total': 80}, 10)
    assert result['score_total'] == 80
    assert len(seen) == 1 and task.recall_evidence['total_repair_rounds'] == 2
    assert len(task.recall_evidence['attempts']) == 2


def test_renderer_only_changes_when_enabled():
    from oskernel_agent.reports.html_tree import _render_recall_check, _recall_dependency_text
    assert _render_recall_check({}) == ''
    evidence = {'functions': [], 'metrics': {'retention_rate': None, 'recall_rate': None}}
    rendered = _render_recall_check({'verdict': {'recall_check': evidence}})
    assert '源码保留与召回核对' in rendered and '未知' in rendered
    assert '已读标记不在最终请求中' in _recall_dependency_text([
        {'path': 'x.c', 'start_line': 2, 'symbol': 'x', 'history': ['dropped', 'retained_recalled']}])


def test_marked_copy_compiles_with_available_compiler(source, tmp_path):
    import shutil
    import subprocess
    from pathlib import Path
    repo, items, _ = source
    compiler = shutil.which('cc') or shutil.which('x86_64-linux-gnu-gcc-13')
    if compiler is None:
        pytest.skip('C compiler unavailable')
    result = prepare_canary(repo, [dict(items[1], confirmed=True, reason='entry')], tmp_path / 'compiled',
                            positions=('start', 'mid', 'end'))
    subprocess.run([compiler, '-std=c11', '-Wall', '-Werror', '-fsyntax-only', str(Path(result['repo']) / 'mm.c')],
                   check=True, capture_output=True)


def test_recorder_redacts_key_even_if_body_contains_it(tmp_path):
    from test_recall_probe import _upstream
    from oskernel_agent.engines.request_recorder import RecordingProxy, complete, load_records
    http = _upstream([])
    try:
        with RecordingProxy(f'http://127.0.0.1:{http.server_port}', 'not-a-real-key', tmp_path) as proxy:
            result = complete(proxy.base_url, 'placeholder', {'model': 'm', 'messages': [
                {'role': 'user', 'content': 'not-a-real-key'}]}, attempts=1)
            assert not result.get('error')
    finally:
        http.shutdown()
        http.server_close()
    assert 'not-a-real-key' not in json.dumps(load_records(tmp_path))


def test_missing_log_is_unknown_and_invalid_probe_does_not_count_as_recall(source, tmp_path):
    from pathlib import Path
    from oskernel_agent.analysis.recall_probe import batch_prompt, build_probes
    repo, items, _ = source
    prepared = prepare_canary(repo, [dict(items[1], confirmed=True, reason='entry')], tmp_path / 'trial')
    manifest = json.loads(Path(prepared['manifest']).read_text())
    marked = Path(prepared['repo'])
    inv = checks.inventory(marked)
    target = next(i for i in inv if i['symbol'] == 'work')
    audit = {'stream_status': 'complete', 'files': [{'path': 'mm.c', 'sha256': target['sha256'], 'returned_ranges': [[1, 12]]}]}
    selection = {'selected': [dict(target, critical=True, coverage='returned')], 'controls': []}
    probes = build_probes(manifest)
    probes = [p for p in probes if p['kind'] == 'canary' and p['role'] == 'target']
    _, ids = batch_prompt(probes)
    answer = json.dumps({'answers': {'q1': probes[0]['answer']}})
    result = checks.assess(marked, manifest, selection, probes, ids, answer, audit, [], True)
    assert result['metrics']['answer_accuracy'] == 1
    assert result['metrics']['retention_rate'] is None and result['metrics']['recall_rate'] is None
    assert result['functions'][0]['markers'][0]['status'] == 'unknown'
    assert not result['affected']
    records = [{'label': 'main', 'seq': 1, 'body': {'messages': [{'role': 'assistant', 'content': probes[0]['answer']}]},
                'response': {'status': 200}},
               {'label': 'probe', 'seq': 1, 'body': {'tools': [{'type': 'function'}]}, 'response': {'status': 200}}]
    result = checks.assess(marked, manifest, selection, probes, ids, answer, audit, records, True)
    assert result['status'] == 'probe_invalid'
    assert result['metrics']['recall_rate'] is None
    assert result['functions'][0]['source_present'] is False
    assert result['functions'][0]['markers'][0]['echo'] is True


def test_full_source_retained_without_marker_in_summary(source):
    repo, items, _ = source
    item = items[1]
    code = '\n'.join(SOURCE.splitlines()[item['start_line'] - 1:item['end_line']])
    assert checks.source_present(repo, item, [{'role': 'assistant', 'content': code}], {'files': []})
    assert not checks.source_present(repo, item, [{'role': 'assistant', 'content': 'a'*32}], {'files': []})


def test_verdict_input_does_not_receive_probe_results(monkeypatch):
    from oskernel_agent.pipeline.tree_builder import _collect_subsys_summaries
    monkeypatch.setenv('AGENT_RECALL_CHECK', '1')
    result = _collect_subsys_summaries({'children': [{'type': 'subsystem', 'name': '测试',
        'evaluation_assertions': {'items': [{'statement': '事实', 'recall_dependencies': [{'status': 'dropped'}]}]},
        'recall_check': {'answers': 'private'}}]})
    assert 'recall_dependencies' not in json.dumps(result)
    assert 'private' not in json.dumps(result) and '事实' in json.dumps(result, ensure_ascii=False)


def test_invalid_log_returns_no_presence_evidence(tmp_path):
    (tmp_path / 'requests.jsonl').write_text('{"body":')
    assert recall_check._records(tmp_path) == []


def test_nested_output_directory_rejected_without_mutating_repo(source, monkeypatch, tmp_path):
    repo, _, _ = source
    monkeypatch.setenv('AGENT_RECALL_DIR', str(repo / 'private'))
    task = llm_batch.BatchTask('x', 'os-kernel-subsys', '', tmp_path / 'r.json', tmp_path, '', repo_path=repo)
    with pytest.raises(ValueError):
        recall_check.run_once(task, 1, lambda *a: pytest.fail('model must not run'))
    assert not (repo / 'private').exists()
