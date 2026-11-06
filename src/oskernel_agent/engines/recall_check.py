from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

from ..analysis import recall_check as checks
from ..analysis.read_audit import parse_events
from ..analysis.read_canary import prepare_canary
from ..analysis.recall_probe import batch_prompt, build_probes
from ..paths import PROJECT_ROOT
from .request_recorder import RecordingProxy, compress_logs, load_records

PROBE_AGENT = 'os-kernel-recall-probe'


def enabled(task):
    return (os.environ.get('AGENT_RECALL_CHECK') == '1' and task.repo_path is not None
            and task.agent_name in {'os-kernel-subsys', 'os-kernel-verdict'} and not task.runtime_env)


def settings():
    maximum = int(os.environ.get('AGENT_RECALL_MAX_FUNCTIONS', '12'))
    sample = float(os.environ.get('AGENT_RECALL_SAMPLE_RATIO', '0.15'))
    if not 4 <= maximum <= 100 or not 0.1 <= sample <= 0.2:
        raise ValueError('召回核对要求最大函数数为 4–100，抽查比例为 0.1–0.2')
    return maximum, sample, os.environ.get('AGENT_RECALL_CALLS', '1') != '0'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    path.chmod(0o600)


def _configuration(task, repo):
    from . import llm_batch
    from .. import config
    path = llm_batch._opencode_source_config_dir() / 'opencode.json'
    cfg = json.loads(path.read_text())
    cfg = checks.clean(cfg, paths=[(task.repo_path.resolve(), repo)])
    model = (cfg.get('agent', {}).get(task.agent_name) or {}).get('model') or cfg.get('model')
    if not model or '/' not in model:
        raise ValueError('召回核对需要项目配置中明确的 provider/model')
    provider, model_id = model.split('/', 1)
    original = cfg.get('provider', {}).get(provider) or {}
    options = original.get('options') or {}
    key = options.get('apiKey')
    if not key or key.startswith('{'):
        auth_path = llm_batch._opencode_source_data_dir() / 'auth.json'
        auth = json.loads(auth_path.read_text()) if auth_path.is_file() else {}
        key = (auth.get(provider) or {}).get('key') or config.api.get('key', '')
    upstream = options.get('baseURL') or config.api.get('base_url', '')
    if not upstream or not key:
        raise ValueError('召回核对缺少模型接口配置')
    cfg['provider'] = {provider: dict(original, npm='@ai-sdk/openai-compatible',
                                     options={'apiKey': 'injected-by-local-recorder'})}
    cfg['model'] = cfg['small_model'] = model
    cfg['share'] = 'disabled'
    cfg.setdefault('agent', {})[PROBE_AGENT] = {'mode': 'primary', 'permission': {'*': 'deny'},
        'prompt': cfg.get('agent', {}).get(task.agent_name, {}).get('prompt', '')}
    probe_model = os.environ.get('AGENT_RECALL_MODEL', '').strip()
    if probe_model:
        if '/' in probe_model and probe_model.split('/', 1)[0] != provider:
            raise ValueError('AGENT_RECALL_MODEL 当前仅支持主任务同一 provider 内的模型')
        name = probe_model.split('/', 1)[-1]
        models = cfg['provider'][provider].setdefault('models', {})
        models.setdefault(name, copy.deepcopy(models.get(model_id, {'name': name})))
        cfg['agent'][PROBE_AGENT]['model'] = f'{provider}/{name}'
    return cfg, provider, upstream, key


def _env(task, home, repo):
    from . import llm_batch
    stub = replace(task, runtime_env={'XDG_DATA_HOME': str(home / 'data')})
    env = llm_batch._opencode_env(stub)
    for part in ('data', 'state', 'config/opencode', 'tmp'):
        (home / part).mkdir(parents=True, exist_ok=True)
    env.update(XDG_DATA_HOME=str(home / 'data'), XDG_STATE_HOME=str(home / 'state'),
               XDG_CONFIG_HOME=str(home / 'config'), TMPDIR=str(home / 'tmp'),
               OSKERNEL_AGENT_REPO_PATH=str(repo), AGENT_OPENCODE_DATA_ROOT=str(home.parent),
               OPENCODE_DISABLE_PROJECT_CONFIG='true')
    return env


def _tokens(manifest):
    return [m['token'] for t in manifest['targets'] for m in t['markers']]


def _records(directory):
    try:
        return load_records(directory)
    except (OSError, ValueError, TypeError):
        return []


def run_once(task, timeout, runner):
    settings()
    repo = task.repo_path.resolve()
    root = Path(os.environ.get('AGENT_RECALL_DIR') or PROJECT_ROOT / 'data' / 'recall_checks')
    root = (root / uuid.uuid4().hex).resolve()
    if root == repo or repo in root.resolve().parents:
        raise ValueError('召回日志和副本必须位于参赛仓库之外')
    root.mkdir(parents=True, mode=0o700)
    size = sum(p.stat().st_size for p in repo.rglob('*') if p.is_file() and not p.is_symlink()
               and '.git' not in p.relative_to(repo).parts and '__pycache__' not in p.relative_to(repo).parts)
    if shutil.disk_usage(root).free < size * 2 + 200_000_000:
        raise RuntimeError('磁盘不足以准备召回工作副本和运行记录，请调整 AGENT_RECALL_DIR')
    items = checks.inventory(repo)
    ranks = checks.priorities(repo, task.evidence_plan, {})
    promoted = {key for run in task.recall_runs for key in run.get('promoted', [])}
    counts = {}
    for item in items:
        key = (item['path'], item['symbol'])
        counts[key] = counts.get(key, 0) + 1
    controls = [i for i in items if checks.identity(i) not in ranks and checks.identity(i) not in promoted
                and counts[(i['path'], i['symbol'])] == 1][-2:]
    control_keys = {checks.identity(c) for c in controls}
    targets = [dict(i, confirmed=True, reason='宿主源码候选') for i in items if checks.identity(i) not in control_keys]
    manifest = {'repo': str(repo), 'targets': []}
    if targets:
        prepared = prepare_canary(repo, targets, root / 'copy', positions=('start', 'mid', 'end'))
        manifest_path = Path(prepared['manifest'])
        manifest = json.loads(manifest_path.read_text())
        manifest_path.unlink()
        working = Path(prepared['repo'])
    else:
        working = root / 'copy' / 'repo'
        shutil.copytree(repo, working, symlinks=True, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        manifest['repo'] = str(working)
    tokens = _tokens(manifest)
    cfg, provider, upstream, key = _configuration(task, working)
    home = root / 'opencode-data' / 'accepted'
    env = _env(task, home, working)
    inputs = []
    for i, path in enumerate(task.input_files):
        dest = root / 'inputs' / f'{i}-{path.name}'
        dest.parent.mkdir(exist_ok=True)
        dest.write_text(checks.clean(path.read_text(), tokens, [(repo, working)]))
        inputs.append(dest)
    prompt = checks.clean(task.user_request, [t for r in task.recall_runs for t in r.get('tokens', [])], [(repo, working)])
    if controls:
        prompt += '\n定位补充：请完整读取以下函数，作为上下文参考：' + json.dumps(
            [{k: c[k] for k in ('path', 'symbol', 'start_line', 'end_line')} for c in controls], ensure_ascii=False)
    runtime = replace(task, repo_path=working, user_request=prompt, input_files=tuple(inputs),
                      runtime_env=env, read_audit_runs=[], recall_runs=[], audit_source_reads=True)
    raw_audit = None
    recording_complete = False
    try:
        with RecordingProxy(upstream, key, root / 'main', timeout=timeout) as proxy:
            proxy.label = 'main'
            cfg['provider'][provider]['options']['baseURL'] = proxy.base_url
            save(home / 'config' / 'opencode' / 'opencode.json', cfg)
            result = runner(runtime, timeout)
            raw_audit = runtime.read_audit_runs[-1] if runtime.read_audit_runs else None
            recording_complete = not proxy.recording_errors
    finally:
        compress_logs(root / 'main')
        shutil.rmtree(home / 'tmp', ignore_errors=True)
        shutil.rmtree(home / 'config' / 'opencode' / 'node_modules', ignore_errors=True)
        save(root / 'private' / 'manifest.json', manifest)
    if raw_audit:
        audit = copy.deepcopy(raw_audit)
        for file in audit.get('files', []):
            path = file['path']
            try:
                original = (repo / path).read_bytes().decode('utf-8')
                marked = (working / path).read_bytes().decode('utf-8')
                if checks.clean(marked) != original:
                    file['returned_ranges'] = []
                    file['mapping_error'] = 'copy_changed_beyond_markers'
                else:
                    file['sha256'] = hashlib.sha256((repo / path).read_bytes()).hexdigest()
            except OSError:
                file['returned_ranges'] = []
        audit['recall_run'] = str(root)
        audit['copy_read_artifact'] = raw_audit.get('artifact')
        audit['measurement'] = 'tool_output_on_copy_mapped_after_exact_comment_removal'
        audit['artifact'] = str(root / 'mapped-audit.json')
        save(Path(audit['artifact']), audit)
        task.read_audit_runs.append(audit)
    run = {'root': str(root), 'repo': str(working), 'home': str(home), 'tokens': tokens,
           'controls': controls, 'items': items, 'promoted': [], 'batch_id': task.batch_id,
           'session_ids': (raw_audit or {}).get('session_ids', []), 'echo': False,
           'recording_complete': recording_complete and (raw_audit or {}).get('stream_status') == 'complete'}
    run['main_model'] = cfg['model']
    run['probe_model'] = cfg['agent'][PROBE_AGENT].get('model') or cfg['model']
    task.recall_runs.append(run)
    save(root / 'session.json', {k: run[k] for k in ('batch_id', 'session_ids', 'home')})
    ok, stdout, written = result
    for path in {task.output_path, *written}:
        if path.is_file() and path.suffix.lower() in {'.json', '.html', '.htm', '.md', '.txt'}:
            text = path.read_text(encoding='utf-8')
            run['echo'] |= any(t in text for t in tokens) or 'READ_AUDIT_CANARY' in text
            if path.suffix.lower() == '.json':
                try:
                    text = json.dumps(checks.clean(json.loads(text), tokens, [(working, repo)]), ensure_ascii=False, indent=2)
                except ValueError:
                    text = checks.clean(text, tokens, [(working, repo)])
            else:
                text = checks.clean(text, tokens, [(working, repo)])
            path.write_text(text, encoding='utf-8')
    return ok, checks.clean(stdout, tokens, [(working, repo)]), written


def _probe(task, run, message, timeout):
    from . import llm_batch
    root, working, home = Path(run['root']), Path(run['repo']), Path(run['home'])
    sessions = run['session_ids']
    if len(sessions) != 1:
        return {'text': '', 'valid': False, 'reason': 'accepted_session_unknown'}, []
    cfg, provider, upstream, key = _configuration(task, working)
    fork = root / 'forks' / uuid.uuid4().hex
    for part in ('data', 'state'):
        shutil.copytree(home / part, fork / part)
    env = _env(task, fork, working)
    try:
        with RecordingProxy(upstream, key, root / 'probe', timeout=timeout) as proxy:
            proxy.label = 'probe'
            cfg['provider'][provider]['options']['baseURL'] = proxy.base_url
            save(fork / 'config' / 'opencode' / 'opencode.json', cfg)
            cmd = [llm_batch._OPENCODE, 'run', '--session', sessions[0], '--agent', PROBE_AGENT,
                   '--format', 'json', llm_batch._opencode_message(message)]
            try:
                proc = subprocess.run(cmd, env=env, cwd=working, capture_output=True, text=True,
                                      encoding='utf-8', errors='replace', timeout=timeout, stdin=subprocess.DEVNULL)
                events, invalid = parse_events(proc.stdout or '')
                valid = (proc.returncode == 0 and not invalid and not any(e['type'] in {'tool_use', 'error'} for e in events)
                         and {e['sessionID'] for e in events} == set(sessions)
                         and any(e['type'] == 'step_finish' and e['part'].get('reason') == 'stop' for e in events))
                result = {'text': ''.join(e['part'].get('text', '') for e in events if e['type'] == 'text'),
                          'valid': valid, 'tool_calls': sum(e['type'] == 'tool_use' for e in events)}
            except subprocess.TimeoutExpired:
                result = {'text': '', 'valid': False, 'reason': 'timeout'}
        save(root / 'private' / 'probe.json', result)
    finally:
        compress_logs(root / 'probe')
        shutil.rmtree(fork, ignore_errors=True)
    return result, _records(root / 'probe')


def check_latest(task, parsed, timeout):
    audit = task.result_read_audit or {}
    run = next((r for r in reversed(task.recall_runs) if r['root'] == audit.get('recall_run')), None)
    if run is None:
        return {'status': 'unknown', 'reason': 'no_accepted_session_record', 'functions': [], 'affected': []}
    root, working = Path(run['root']), Path(run['repo'])
    manifest = json.loads((root / 'private' / 'manifest.json').read_text())
    raw = json.loads(Path(audit['copy_read_artifact']).read_text())
    maximum, sample, calls = settings()
    items = checks.inventory(working)
    controls = [i for i in items if checks.identity(i) in {checks.identity(c) for c in run['controls']}]
    selection = checks.select_functions(working, items, controls, raw, task.evidence_plan, parsed,
                                        maximum, sample, secrets.randbits(64))
    selected = {checks.identity(i): i for i in selection['selected']}
    marked = dict(manifest, targets=[t for t in manifest['targets'] if checks.identity(t) in selected])
    probes = [p for p in build_probes(marked, selection['controls']) if p['kind'] == 'canary']
    for probe in probes:
        probe['id'] += ':' + str(probe['start_line'])
    fake_paths = {p['path'] for p in probes if p['role'] == 'fake'}
    for path in sorted({i['path'] for i in selection['selected'] + selection['controls']} - fake_paths):
        names = {i['symbol'] for i in items if i['path'] == path}
        fake = 'missing_' + secrets.token_hex(8)
        while fake in names:
            fake = 'missing_' + secrets.token_hex(8)
        probes.append({'path': path, 'symbol': fake, 'start_line': None, 'end_line': None,
                       'id': f'canary:{path}:{fake}:start', 'kind': 'canary', 'role': 'fake',
                       'position': 'start', 'line': None, 'answer': None, 'needle': None})
    if calls:
        for item in selection['selected']:
            if item['critical']:
                options = sorted(set(item['calls']) | {f['symbol'] for f in items if f['path'] == item['path']})
                probes.append(dict(item, id='calls:' + checks.identity(item), kind='calls', role='target',
                                   options=options, answer=sorted(set(item['calls'])), needle=None, line=None))
    if not probes:
        return {'status': 'no_candidates', 'functions': [], 'affected': [], 'selection': selection}
    message, ids = batch_prompt(probes)
    response, probe_records = _probe(task, run, message, timeout)
    result = checks.assess(working, manifest, selection, probes, ids, response['text'], raw,
                           (_records(root / 'main') if run['recording_complete'] else []) + probe_records, response['valid'])
    result.update(batch_id=run['batch_id'], session_ids=run['session_ids'], artifact=str(root / 'check.json'),
                  report_echo=run['echo'], model=run['probe_model'], main_model=run['main_model'])
    run['promoted'] = [checks.identity(i) for i in selection['selected'] if i['critical']]
    save(root / 'check.json', result)
    return result


def verify(task, parsed, timeout):
    from . import llm_batch
    from .read_repair import _draft_for_reread
    attempts, affected = [], set()
    initial_repairs = task.read_evidence.get('repair_rounds', 0)
    task.recall_budget['coverage'] = task.recall_budget.get('coverage', 0) + initial_repairs
    used = task.recall_budget.get('recall', 0)
    budget = max(0, 2 - task.recall_budget['coverage'] - used)
    for number in range(budget + 1):
        result = check_latest(task, parsed, timeout)
        affected.update(result['affected'])
        attempts.append(result)
        targets = [r for r in result['functions'] if 'reread' in r['actions'] or r['status'] == 'unmarked']
        task.recall_evidence = dict(result, attempts=attempts, repair_rounds=number,
                                   total_repair_rounds=task.recall_budget['coverage'] + used + number, affected=sorted(affected))
        if not targets or number == budget:
            break
        locations = [{k: r[k] for k in ('path', 'symbol', 'start_line', 'end_line')} for r in targets]
        prompt = task.user_request + '\n【源码上下文补证】在新会话完整读取以下函数及其必要依赖，重新核对并写齐原任务的全部输出。不得修改评分。\n' + json.dumps(locations, ensure_ascii=False)
        prompt += '\n待核对报告：\n' + _draft_for_reread(parsed)
        prompt = checks.clean(prompt, [t for run in task.recall_runs for t in run['tokens']])
        repair = replace(task, batch_id=f'{task.batch_id}-recall-{number + 1}', user_request=prompt,
                         cache_enabled=False, recall_evidence={}, read_evidence={}, result_read_audit=None,
                         read_repair_rounds=0, required_source_ranges=task.required_source_ranges +
                         checks.read_requirements(task.repo_path, parsed))
        task.recall_budget['recall'] = used + number + 1
        candidate = llm_batch._run_batch_task(repair, timeout=timeout)
        if candidate.get('_error') or not llm_batch._delivery_valid(llm_batch._delivery_verdict(task, candidate)):
            raise RuntimeError(f'{task.batch_id} 上下文补证未形成有效报告')
        for key in ('scores', 'score', 'total_score', 'score_total', 'dimensions'):
            if key in parsed:
                candidate[key] = parsed[key]
        parsed = candidate
        task.result_read_audit = repair.result_read_audit
        task.read_evidence = repair.read_evidence
    if task.recall_evidence.get('artifact'):
        save(Path(task.recall_evidence['artifact']).with_name('recall.json'), task.recall_evidence)
    return parsed


def dependencies(evidence, requirements):
    functions = {}
    for attempt in evidence.get('attempts', [evidence]):
        for function in attempt.get('functions', []):
            key = checks.identity(function)
            if any(r['path'] == function['path'] and r['start_line'] <= function['end_line']
                   and function['start_line'] <= r['end_line'] for r in requirements):
                entry = functions.setdefault(key, {'path': function['path'], 'symbol': function['symbol'],
                                                    'start_line': function['start_line'], 'history': []})
                entry['history'].append(function['status'])
                entry['status'] = function['status']
    return list(functions.values())


def prioritize_claims(evidence, selection):
    affected = set(evidence.get('affected', []))
    all_claims = selection['selected'] + selection['unreviewed']
    selected_ids = {c['id'] for c in selection['selected']}
    forced = []
    for claim in all_claims:
        related = dependencies(evidence, claim['requirements'])
        claim['recall_dependencies'] = related
        if any(checks.identity(r) in affected for r in related):
            claim['recall_triggered'] = True
            forced.append(claim['id'])
            selected_ids.add(claim['id'])
    return dict(selection, selected=[c for c in all_claims if c['id'] in selected_ids],
                unreviewed=[c for c in all_claims if c['id'] not in selected_ids], recall_forced=forced)
