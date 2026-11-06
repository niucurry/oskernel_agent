"""One original OpenCode/MCP subsystem run with a bounded, audited API relay."""
from __future__ import annotations
import pathlib, json, hashlib, os, sys, threading, time, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import importlib.metadata, resource

P = pathlib.Path(__file__).resolve().parent
PRIVATE = pathlib.Path('/home/niu/.config/oskernel-research/deepseek.json')
PROTOCOL = json.loads((P / 'protocol.json').read_text())
CONF = json.loads(PRIVATE.read_text())
LEDGER = []
LOCK = threading.Lock()
OUT = P / 'outputs'
OUT.mkdir(exist_ok=True)
CALLS = P / 'calls'
CALLS.mkdir(exist_ok=True)
START = time.monotonic()
PREVIOUS_PROMPT_TOKENS = None
PREVIOUS_MESSAGES = None

def dump(path, value):
    raw = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    if CONF['api_key'] in raw:
        raise ValueError('private credential must not enter research artifacts')
    path.write_text(raw)

class Relay(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, raw, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.respond(200, json.dumps({'object': 'list', 'data': [{'id': PROTOCOL['model']['id'], 'object': 'model'}]}).encode())

    def do_POST(self):
        global PREVIOUS_PROMPT_TOKENS, PREVIOUS_MESSAGES
        if not self.path.endswith('/chat/completions'):
            self.respond(404, b'{"error":{"message":"unsupported relay route"}}')
            return
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        # Generation parameters are fixed before the first request. No messages
        # or tool results are changed by the relay.
        body['model'] = PROTOCOL['model']['id']
        body['temperature'] = PROTOCOL['model']['temperature']
        body['thinking'] = {'type': PROTOCOL['model']['thinking']}
        body['max_tokens'] = PROTOCOL['budget']['max_completion_tokens']
        body.pop('max_completion_tokens', None)
        body.pop('reasoning_effort', None)
        if body.get('stream'):
            body['stream_options'] = {'include_usage': True}
        raw = json.dumps(body, ensure_ascii=False).encode()
        with LOCK:
            used = sum(r['accounted_tokens'] for r in LEDGER)
            # Whole-byte bound unless the reported prefix is exactly reused.
            messages = body.get('messages', [])
            if PREVIOUS_MESSAGES is not None and messages[:len(PREVIOUS_MESSAGES)] == PREVIOUS_MESSAGES:
                added = messages[len(PREVIOUS_MESSAGES):]
                reserve = PREVIOUS_PROMPT_TOKENS + len(json.dumps(added, ensure_ascii=False).encode()) + 1000 * len(added) + body['max_tokens']
            else:
                reserve = len(raw) + body['max_tokens']
            if len(LEDGER) >= PROTOCOL['budget']['requests'] or used + reserve > PROTOCOL['budget']['reported_actual_tokens']:
                dump(P / 'relay-budget-stop.json', {'used_tokens_including_unknown_reserve': used, 'next_reserve': reserve, 'requests': len(LEDGER)})
                self.respond(400, b'{"error":{"type":"budget_stop","message":"Frozen research API budget exhausted; no retry permitted"}}')
                return
            call_id = len(LEDGER) + 1
            row = {'id': call_id, 'request_sha256': hashlib.sha256(raw).hexdigest(), 'status': 'pending', 'reserved_next_call_tokens': reserve, 'accounted_tokens': max(reserve, PROTOCOL['budget']['unknown_request_reserve'])}
            LEDGER.append(row)
            dump(CALLS / f'{call_id:02d}-request.json', body)
            dump(P / 'ledger.json', LEDGER)
        try:
            request = urllib.request.Request(CONF['base_url'].rstrip('/') + '/chat/completions', data=raw, headers={'Authorization': 'Bearer ' + CONF['api_key'], 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=90) as response:
                response_raw = response.read()
                content_type = response.headers.get('Content-Type', 'application/json')
            if CONF['api_key'].encode() in response_raw:
                raise ValueError('credential unexpectedly present in response')
            (CALLS / f'{call_id:02d}-response.txt').write_bytes(response_raw)
            chunks = []
            if body.get('stream'):
                for line in response_raw.decode().splitlines():
                    if line.startswith('data: ') and line[6:] != '[DONE]':
                        chunks.append(json.loads(line[6:]))
            else:
                chunks = [json.loads(response_raw)]
            usages = [c['usage'] for c in chunks if c.get('usage')]
            row.update(status='ok' if usages else 'usage_unknown', usage=usages[-1] if usages else None, model=next((c.get('model') for c in chunks if c.get('model')), None))
            if usages:
                row['accounted_tokens'] = row['usage']['total_tokens']
                PREVIOUS_PROMPT_TOKENS = row['usage']['prompt_tokens']
                PREVIOUS_MESSAGES = messages
            self.respond(200, response_raw, content_type)
        except Exception as exc:
            row.update(status='api_or_relay_failure', error={'type': type(exc).__name__, 'message': str(exc)[:300]})
            self.respond(400, json.dumps({'error': {'type': 'research_api_failure', 'message': type(exc).__name__}}).encode())
        finally:
            dump(P / 'ledger.json', LEDGER)
            print('relay', call_id, row['status'], (row.get('usage') or {}).get('total_tokens', 'unknown'), flush=True)

def main():
    # This dependency check must finish before a network request can be relayed.
    from oskernel_agent.cli import mcp_server
    from oskernel_agent.cli.setup_opencode import _build_session_prompt
    from oskernel_agent.prompts.builder import SessionType, SESSION_AGENT_NAMES
    from oskernel_agent.pipeline.tree_builder import _build_subsys_request, _build_subsys_outputs, _validate_subsys_result, _apply_subsys_result, _clean_tree
    from oskernel_agent.engines.llm_batch import BatchTask, _run_opencode_once
    from oskernel_agent.reports.html_tree import _render_all_subsystems
    repo = P / 'repo'
    files = [{'path': r['path'], 'name': pathlib.Path(r['path']).name, 'lang': 'c'} for r in PROTOCOL['inputs']]
    for row in PROTOCOL['inputs']:
        assert hashlib.sha256((repo / row['path']).read_bytes()).hexdigest() == row['sha256']
    node = {'name': '进程间通信', 'path': '<subsys>/进程间通信', 'files': files, 'children': []}
    outputs = _build_subsys_outputs(node, OUT)
    user = _build_subsys_request(node, repo, outputs, None)
    system = _build_session_prompt(SessionType.SUBSYS)
    (P / 'original-user-request.txt').write_text(user)
    (P / 'original-system-prompt.txt').write_text(system)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Relay)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config_dir = P / 'runtime-config'
    config_dir.mkdir(exist_ok=True)
    model = 'description-proxy/' + PROTOCOL['model']['id']
    agent_name = SESSION_AGENT_NAMES[SessionType.SUBSYS]
    permission = {k: {'*': 'deny'} for k in ['bash', 'edit', 'write', 'read', 'glob', 'grep', 'task', 'skill']}
    permission.update({'webfetch': 'deny', 'websearch': 'deny', 'todowrite': 'deny', 'os-kernel-tools_initialize_analysis': {'*': 'deny'}, 'os-kernel-tools_compare_with_reference_os': {'*': 'deny'}})
    mcp_boot = "import runpy,sys;sys.path.insert(0,'/home/niu/agent/src');runpy.run_module('oskernel_agent.cli.mcp_server',run_name='__main__')"
    config = {'$schema': 'https://opencode.ai/config.json', 'model': model, 'small_model': model, 'provider': {'description-proxy': {'npm': '@ai-sdk/openai-compatible', 'name': 'Budgeted description research relay', 'options': {'baseURL': f'http://127.0.0.1:{server.server_port}/v1', 'apiKey': 'local-placeholder'}, 'models': {PROTOCOL['model']['id']: {'name': PROTOCOL['model']['id'], 'limit': {'context': 128000, 'output': 6000}}}}}, 'agent': {agent_name: {'model': model, 'system': system, 'permission': permission}}, 'mcp': {'os-kernel-tools': {'type': 'local', 'enabled': True, 'command': [sys.executable, '-c', mcp_boot, '--max-steps', '20'], 'environment': {'PYTHONPATH': os.environ['PYTHONPATH'], 'PYTHONIOENCODING': 'utf-8', 'OSKERNEL_AGENT_REPO_PATH': str(repo)}}}}
    dump(config_dir / 'opencode.json', config)
    os.environ.update({'AGENT_OPENCODE_SOURCE_CONFIG_DIR': str(config_dir), 'AGENT_OPENCODE_SOURCE_DATA_DIR': str(P / 'empty-auth-dir'), 'AGENT_OPENCODE_DATA_ROOT': str(P / 'opencode-state'), 'AGENT_OPENCODE_KEEP_DATA': '1'})
    # No actual upstream API key is given to OpenCode or the MCP server.
    inputs = [P / 'protocol.json', P / 'run.py', P / 'reference-service-identities.json', P / 'original-system-prompt.txt', P / 'original-user-request.txt', pathlib.Path('/home/niu/agent/src/oskernel_agent/cli/mcp_server.py'), pathlib.Path('/home/niu/agent/src/oskernel_agent/pipeline/tree_builder.py')]
    import mcp
    dump(P / 'freeze.json', {'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'files': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}, 'versions': {n: importlib.metadata.version(n) for n in ['pydantic', 'tree-sitter', 'tree-sitter-c', 'networkx']}, 'mcp_loaded_source': mcp.__file__, 'mcp_author_version': '1.26.0; exact GitHub source and retrieval SHA in mcp-sdk-retrieval.json, not the installed 2.x metadata', 'mcp_tools': len(mcp_server._make_tool_defs())})
    task = BatchTask(batch_id='chibios-messaging-original-runtime', agent_name=agent_name, user_request=user, output_path=pathlib.Path(outputs['json_path']), cache_dir=P / 'unused-cache', cache_key='no-cache', cache_enabled=False, repo_path=repo)
    state = 'incomplete'
    error = None
    try:
        ok, stdout, written = _run_opencode_once(task, timeout=240)
        (P / 'opencode-stdout.txt').write_text(stdout)
        if not ok:
            state = 'no_complete_report'
        else:
            parsed = json.loads(pathlib.Path(outputs['json_path']).read_text())
            parsed['content'] = pathlib.Path(outputs['content_path']).read_text()
            for module in parsed.get('modules', []):
                module['content'] = pathlib.Path(outputs['module_paths'][module['slot'] - 1]).read_text()
            _validate_subsys_result(parsed, node['name'], repo)
            _apply_subsys_result(node, parsed, repo)
            tree = {'meta': {'repo': 'ChibiOS-public-messaging-unit'}, 'facts': {}, 'verdict': {}, 'tree': {'children': [_clean_tree(node)]}}
            dump(OUT / 'tree.json', tree)
            (OUT / 'module-report.html').write_text(_render_all_subsystems(tree, lambda path, line: f'#source:{path}:{line}'))
            state = 'complete_validated_subsystem'
    except Exception as exc:
        error = {'type': type(exc).__name__, 'message': str(exc)[:500]}
        state = 'runtime_or_validation_failure'
    finally:
        server.shutdown()
        own = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        dump(P / 'audit.json', {'state': state, 'error': error, 'requests': len(LEDGER), 'reported_tokens': sum((r.get('usage') or {}).get('total_tokens', 0) for r in LEDGER), 'unknown_requests': sum(not r.get('usage') for r in LEDGER), 'wall_seconds': time.monotonic() - START, 'cpu_seconds_self_and_children': own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime, 'semantic_quality': 'unmeasured; validation is not a quality oracle'})
        print(state, error, flush=True)

if __name__ == '__main__':
    main()
