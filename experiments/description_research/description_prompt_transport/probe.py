"""Original OpenCode with a loopback mock: check prompt transport, no inference."""
from __future__ import annotations
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

P = pathlib.Path(__file__).resolve().parent
REQUESTS = []
LOCK = threading.Lock()


def dump(q, value):
    q.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with LOCK:
            REQUESTS.append(request)
        response = {'id': 'local-mock-only', 'object': 'chat.completion', 'created': 0,
                    'model': 'mock-transport', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '本地传输核验完成。'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}
        if request.get('stream'):
            start = {'id': 'local-mock-only', 'object': 'chat.completion.chunk', 'created': 0,
                     'model': 'mock-transport', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': '本地传输核验完成。'}, 'finish_reason': None}]}
            finish = {'id': 'local-mock-only', 'object': 'chat.completion.chunk', 'created': 0,
                      'model': 'mock-transport', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': response['usage']}
            raw = ('data: ' + json.dumps(start, ensure_ascii=False) + '\n\ndata: ' + json.dumps(finish, ensure_ascii=False) + '\n\ndata: [DONE]\n\n').encode()
            content_type = 'text/event-stream'
        else:
            raw = json.dumps(response, ensure_ascii=False).encode()
            content_type = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def run_case(label, field, endpoint):
    from oskernel_agent import config
    from oskernel_agent.cli import setup_opencode as setup
    from oskernel_agent.engines import llm_batch as batch
    from oskernel_agent.prompts.builder import SessionType, SESSION_AGENT_NAMES
    case = P / label
    case.mkdir(exist_ok=True)
    os.environ.update({'AGENT_OPENCODE_SOURCE_CONFIG_DIR': str(case / 'source-config'),
                       'AGENT_OPENCODE_SOURCE_DATA_DIR': str(case / 'dummy-auth'),
                       'AGENT_OPENCODE_KEEP_DATA': '1', 'AGENT_OPENCODE_ISOLATED_DATA': '1',
                       'LLM_MODEL': 'mock-transport'})
    setup.PROJECT_ROOT = case
    setup._VENV_PY = sys.executable
    config.api = {'key': 'local-test-placeholder', 'base_url': endpoint}
    config.engine = {'max_steps': 1}
    setup.setup()
    q = case / 'source-config' / 'opencode.json'
    cfg = json.loads(q.read_text())
    cfg['small_model'] = 'deepseek/mock-transport'
    cfg['mcp']['os-kernel-tools']['enabled'] = False
    agent = SESSION_AGENT_NAMES[SessionType.SUBSYS]
    expected = setup._build_session_prompt(SessionType.SUBSYS)
    if field == 'system':
        cfg['agent'][agent]['system'] = cfg['agent'][agent].pop('prompt')
    dump(q, cfg)
    task = batch.BatchTask(batch_id=label, agent_name=agent,
                           user_request='TRANSPORT_USER_SENTINEL\n第二行：仅检查消息传输，不生成 OS 报告。',
                           output_path=case / 'unused.json', cache_dir=case / 'unused-cache', cache_key='', cache_enabled=False)
    batch._OPENCODE_DATA_ROOT = case / 'opencode-state'
    command = batch._opencode_command(task)
    env = batch._opencode_env(task)
    dump(case / 'command.json', command)
    (case / 'expected-system.txt').write_text(expected)
    before = len(REQUESTS)
    start = time.monotonic()
    proc = subprocess.Popen(command, cwd=case, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL, start_new_session=True)
    timeout = False
    try:
        stdout, stderr = proc.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        timeout = True
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
    (case / 'stdout.txt').write_text(stdout)
    (case / 'stderr.txt').write_text(stderr)
    requests = REQUESTS[before:]
    dump(case / 'captured-requests.json', requests)
    systems = [str(m.get('content', '')) for req in requests for m in req.get('messages', []) if m.get('role') == 'system']
    delivered = any(expected in text for text in systems)
    record = {'case': label, 'field': field, 'returncode': proc.returncode, 'timeout': timeout,
              'wall_seconds': time.monotonic() - start, 'local_http_requests': len(requests),
              'remote_model_requests': 0, 'expected_system_sha256': hashlib.sha256(expected.encode()).hexdigest(),
              'exact_expected_system_delivered': delivered,
              'has_user_sentinel': any('TRANSPORT_USER_SENTINEL' in str(m.get('content', '')) for req in requests for m in req.get('messages', []) if m.get('role') == 'user'),
              'quality': 'not measured; fixed mock response has no semantic inference'}
    dump(case / 'result.json', record)
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


def audit_previous():
    old = pathlib.Path('/home/niu/agent-research/description_runtime_grounding')
    expected = (old / 'original-system-prompt.txt').read_text()
    rows = []
    for q in sorted((old / 'calls').glob('*-request.json')):
        request = json.loads(q.read_text())
        systems = [str(m.get('content', '')) for m in request['messages'] if m['role'] == 'system']
        rows.append({'file': str(q), 'sha256': hashlib.sha256(q.read_bytes()).hexdigest(),
                     'contains_expected_system': any(expected in text for text in systems),
                     'system_characters': [len(text) for text in systems]})
    dump(P / 'previous-run-amendment.json', {'original_requests': rows, 'all_intended_system_absent': all(not r['contains_expected_system'] for r in rows),
        'boundary': 'Then-current production really ran, but computed custom system text never reached the model. Its failure and costs remain valid; intended-template semantic quality was not tested.',
        'old_frozen_artifacts_modified': False})


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Mock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f'http://127.0.0.1:{server.server_port}/v1'
        old = run_case('legacy-system', 'system', endpoint)
        fixed = run_case('correct-prompt', 'prompt', endpoint)
    finally:
        server.shutdown()
    audit_previous()
    dump(P / 'results.json', {'controls': [old, fixed], 'remote_model_requests': 0})
    assert not old['timeout'] and not fixed['timeout']
    assert old['local_http_requests'] > 0 and fixed['local_http_requests'] > 0
    assert not old['exact_expected_system_delivered']
    assert fixed['exact_expected_system_delivered']
    assert old['has_user_sentinel'] and fixed['has_user_sentinel']


if __name__ == '__main__':
    main()
