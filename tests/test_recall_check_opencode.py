import json
import os
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from oskernel_agent.analysis.recall_probe import PROBE_TAG
from oskernel_agent.engines import llm_batch, claim_review
from oskernel_agent.engines.request_recorder import load_records
from test_recall_check import SOURCE


def server(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            messages = body.get('messages', [])
            names = [t['function']['name'] for t in body.get('tools', [])]
            text = lambda m: m.get('content') if isinstance(m.get('content'), str) else ''.join(p.get('text', '') for p in m.get('content', []) or [] if isinstance(p, dict))
            last = text(messages[-1])
            users = '\n'.join(text(m) for m in messages if m.get('role') in {'user', 'assistant'})
            paths = re.search(r'REPO=(\S+) OUTPUT=(\S+)', users)
            assert paths, users[:200]
            repo, output = paths.groups()
            current = state['runs'].setdefault(repo, {'read': False, 'written': False})
            usage = {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}
            message = {'role': 'assistant', 'content': 'done'}
            if 'anchored summary' in last:
                state['compacted'] = True
                message['content'] = '## Goal\n- Describe work and second.\n## Instructions\n' + f'REPO={repo} OUTPUT={output}'
            elif PROBE_TAG in last:
                assert names == []
                state['probe_count'] += 1
                tools = '\n'.join(text(m) for m in messages if m.get('role') == 'tool')
                parts = re.split(r'(q\d+)：', last)
                answers = {}
                for i in range(1, len(parts), 2):
                    qid, question = parts[i:i+2]
                    if '函数体内调用' in question:
                        answers[qid] = ['helper']
                        continue
                    line = re.search(r'第 (\d+) 行（函数', question)
                    hit = line and re.search(rf'^\s*{line[1]}(?::|\s\|)\s.*READ_AUDIT_CANARY:([0-9a-f]{{32}})', tools, re.M)
                    value = hit[1] if hit else None
                    if value and state['mode'] == 'wrong':
                        value = '0'*32
                    if value and state['mode'] == 'swap' and ('函数 work' in question or '函数 second' in question):
                        other_line = 7 if '函数 work' in question else 2
                        other = re.search(rf'^\s*{other_line}(?::|\s\|)\s.*READ_AUDIT_CANARY:([0-9a-f]{{32}})', tools, re.M)
                        value = other[1] if other else value
                    answers[qid] = value
                message['content'] = json.dumps({'answers': answers})
            elif names:
                if not current['read']:
                    current['read'] = True
                    call = ('read', {'filePath': repo + '/mm.c'})
                    if state['mode'] == 'mcp':
                        call = ('_read_file', {'path': 'mm.c'})
                    if state['mode'] == 'compact' and len(state['runs']) == 1:
                        usage = {'prompt_tokens': 62000, 'completion_tokens': 10, 'total_tokens': 62010}
                elif not current['written']:
                    current['written'] = True
                    call = ('_write_report', {'output_path': output, 'content': json.dumps(
                        {'summary': 'mm.c:2-6；mm.c:7-10', 'issues': [
                            {'quote': 'work 调用 helper，见 mm.c:2-6', 'path': 'mm.c:2-6'},
                            {'quote': 'second 调用 helper，见 mm.c:7-10', 'path': 'mm.c:7-10'}], 'score_total': 80}, ensure_ascii=False)})
                else:
                    call = None
                if call:
                    name = next(n for n in names if n == call[0] or n.endswith(call[0]))
                    message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                        'index': 0, 'id': f'call_{len(messages)}', 'type': 'function',
                        'function': {'name': name, 'arguments': json.dumps(call[1])}}]}
            reason = 'tool_calls' if message.get('tool_calls') else 'stop'
            self.send_response(200)
            if not body.get('stream'):
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'choices': [{'index': 0, 'message': message, 'finish_reason': reason}], 'usage': usage}).encode())
            else:
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for payload in ({'choices': [{'index': 0, 'delta': message, 'finish_reason': None}]},
                                {'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}], 'usage': usage}):
                    self.wfile.write(('data: ' + json.dumps(dict(payload, id='x', object='chat.completion.chunk', created=1, model='mock')) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    return http


@pytest.mark.skipif(os.environ.get('OSKERNEL_TEST_OPENCODE') != '1', reason='opt-in localhost OpenCode integration')
@pytest.mark.parametrize('mode', ['normal', 'mcp', 'compact', 'wrong', 'swap'])
def test_production_recall_gate(tmp_path, monkeypatch, mode):
    pytest.importorskip('mcp')
    if not shutil.which('opencode'):
        pytest.skip('OpenCode unavailable')
    from oskernel_agent.cli.recall_probe import _opencode_config
    repo = tmp_path / 'original'
    repo.mkdir()
    (repo / 'mm.c').write_text(SOURCE)
    state = {'mode': mode, 'runs': {}, 'probe_count': 0, 'compacted': False}
    http = server(state)
    cfg = _opencode_config(f'http://127.0.0.1:{http.server_port}', 'mock', repo, 65536, 4096, 'auto' if mode == 'compact' else 'off')
    cfg['provider']['audit']['options']['apiKey'] = 'local-secret-only'
    cfg['agent']['os-kernel-subsys'] = cfg['agent'].pop('read-audit-trial')
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    config_bytes = json.dumps(cfg)
    (config_dir / 'opencode.json').write_text(config_bytes)
    for key, value in {'AGENT_RECALL_CHECK': '1', 'AGENT_RECALL_DIR': str(tmp_path / 'recall'),
                       'AGENT_OPENCODE_SOURCE_CONFIG_DIR': str(config_dir),
                       'AGENT_READ_AUDIT_DIR': str(tmp_path / 'receipts'),
                       'AGENT_CLAIM_REVIEW_DIR': str(tmp_path / 'reviews'),
                       'OPENCODE_DISABLE_DEFAULT_PLUGINS': 'true', 'OPENCODE_DISABLE_MODELS_FETCH': 'true',
                       'OPENCODE_DISABLE_AUTOUPDATE': 'true', 'OPENCODE_DISABLE_LSP_DOWNLOAD': 'true'}.items():
        monkeypatch.setenv(key, value)
    out = tmp_path / 'report.json'
    task = llm_batch.BatchTask('subsys-test', 'os-kernel-subsys', f'REPO={repo} OUTPUT={out} Describe work and second.',
                               out, tmp_path, '', repo_path=repo, audit_source_reads=True, read_repair_rounds=2)
    try:
        report = llm_batch.run_batch_task(task, timeout=90)
    finally:
        http.shutdown()
        http.server_close()
    evidence = task.recall_evidence
    assert report['score_total'] == 80
    assert (repo / 'mm.c').read_text() == SOURCE
    assert (config_dir / 'opencode.json').read_text() == config_bytes
    assert evidence['status'] == 'checked', evidence
    assert evidence['metrics']['confabulations'] == 0
    assert 'READ_AUDIT_CANARY' not in out.read_text()
    for run in task.recall_runs:
        from pathlib import Path
        root = Path(run['root'])
        records = load_records(root / 'main') + load_records(root / 'probe')
        assert 'local-secret-only' not in json.dumps(records)
        assert not any((r.get('body') or {}).get('tools') for r in records if r['label'] == 'probe')
        assert not (Path(run['home']) / 'tmp').exists()
        assert not (Path(run['home']) / 'config/opencode/node_modules').exists()
    initial = evidence['attempts'][0]['functions']
    if mode in {'normal', 'mcp'}:
        assert all(f['status'] == 'retained_recalled' for f in initial)
        assert evidence['repair_rounds'] == 0
    elif mode == 'compact':
        assert state['compacted']
        assert any(f['status'] == 'dropped' for f in initial)
        assert evidence['repair_rounds'] == 1
        assert all(f['status'] == 'retained_recalled' for f in evidence['functions'])
        assert set(task.recall_runs[0]['tokens']).isdisjoint(task.recall_runs[-1]['tokens'])
    elif mode == 'wrong':
        assert any(f['status'] == 'present_not_recalled' for f in initial)
        assert evidence['repair_rounds'] == 2
    else:
        assert {f['symbol'] for f in initial if f['status'] == 'misattributed'} == {'work', 'second'}
        assert evidence['repair_rounds'] == 0
    captured = []
    def pair(repo, selection, directory, timeout, adjudicate):
        captured.extend(c for c in selection['selected'] if c.get('recall_triggered'))
        return []
    monkeypatch.setattr(claim_review, '_review_pair', pair)
    claim_review.review_and_revise(task, report, allow_revision=False)
    if mode in {'wrong', 'swap', 'compact'}:
        assert {c['location'] for c in captured if c['kind'] == 'issue'} == {'mm.c:2-6', 'mm.c:7-10'}
    else:
        assert not captured
