from __future__ import annotations

import hashlib
import json
import math
import random
import re
from pathlib import Path

from .read_audit import _returned_lines, _tool_kind, merge_ranges
from .read_canary import MARKER
from .read_coverage import check_requirements, read_requirements, source_functions
from .recall_probe import locate, message_texts, parse_batch

_COMMENT = re.compile(r" ?/\*\s*READ_AUDIT_CANARY:[0-9a-fA-F]{32}\s*\*/ ?")
_TOKEN = re.compile(r"READ_AUDIT_CANARY\s*:\s*[0-9a-fA-F]{1,32}")


def identity(item):
    return f"{item['path']}:{item['start_line']}:{item['symbol']}"


def clean(value, tokens=(), paths=()):
    if isinstance(value, str):
        value = _COMMENT.sub('', value)
        value = _TOKEN.sub('', value).replace(MARKER, '')
        value = value.replace('READ_AUDIT_CANARY', '')
        for token in tokens:
            value = value.replace(token, '')
        for old, new in paths:
            value = value.replace(str(old), str(new))
        return value
    if isinstance(value, list):
        return [clean(v, tokens, paths) for v in value]
    if isinstance(value, dict):
        return {clean(k, tokens, paths): clean(v, tokens, paths) for k, v in value.items()}
    return value


def without_probe_results(value):
    if isinstance(value, dict):
        return {k: without_probe_results(v) for k, v in value.items()
                if k not in {'recall_check', 'recall_dependencies', 'canary_observations'}}
    if isinstance(value, list):
        return [without_probe_results(v) for v in value]
    return value


def inventory(repo):
    items = []
    for path in sorted(repo.rglob('*')):
        if path.suffix.lower() not in {'.c', '.h', '.cc', '.cpp', '.hpp', '.rs'}:
            continue
        if any(p in {'.git', 'node_modules', '__pycache__', 'target', 'build'} for p in path.relative_to(repo).parts):
            continue
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            continue
        inv = source_functions(repo, str(path.relative_to(repo)))
        if inv['status'] == 'parsed':
            items.extend(dict(f, path=inv['path'], sha256=inv['sha256'], scope='function') for f in inv['functions'])
    return items


def priorities(repo, plan, report):
    result = {}
    for item in (plan or {}).get('items', []):
        impact = item.get('score_impact', 0)
        for evidence in item['evidence']:
            for req in evidence['requirements']:
                for f in source_functions(repo, req['path'])['functions']:
                    if f['start_line'] <= req['end_line'] and req['start_line'] <= f['end_line']:
                        key = identity(dict(f, path=req['path']))
                        result[key] = max(result.get(key, (0, 0)), (impact, 2))
    for req in read_requirements(repo, report):
        if req['scope'] == 'function':
            result.setdefault(identity(req), (0, 1))
    return result


def select_functions(repo, items, controls, audit, plan, report, limit=12, sample=0.15, seed=0):
    ranks = priorities(repo, plan, report)
    checked = {identity(r): r for r in check_requirements(repo, audit, items)}
    critical = sorted((i for i in items if identity(i) in ranks),
                      key=lambda i: (tuple(-v for v in ranks[identity(i)]), identity(i)))
    other = [i for i in items if identity(i) not in ranks and checked[identity(i)]['status'] == 'returned'
             and identity(i) not in {identity(c) for c in controls}]
    random.Random(seed).shuffle(other)
    sampled = other[:math.ceil(len(other) * sample)]
    available_controls = [c for c in controls if checked[identity(c)]['status'] == 'returned'
                          and identity(c) not in ranks][:2]
    selected, files, omitted = [], {c['path'] for c in available_controls}, []
    for item in critical + sampled:
        cost = len(selected) + len(available_controls) + len(files | {item['path']}) + 1
        if cost > limit:
            omitted.append(dict(path=item['path'], symbol=item['symbol'], start_line=item['start_line'], reason='function_budget'))
        else:
            selected.append(dict(item, critical=identity(item) in ranks, coverage=checked[identity(item)]['status']))
            files.add(item['path'])
    return {'selected': selected, 'controls': available_controls, 'omitted': omitted,
            'critical_count': len(critical), 'sample_pool': len(other), 'sample_requested': len(sampled),
            'control_shortfall': 2 - len(available_controls), 'seed': seed, 'limit': limit}


def context_ranges(messages, repo):
    calls, lines = {}, {}
    for message in messages:
        for call in message.get('tool_calls') or []:
            try:
                fn = call.get('function') or {}
                calls[call['id']] = (fn.get('name', ''), json.loads(fn.get('arguments') or '{}'))
            except (KeyError, ValueError, TypeError):
                pass
        if message.get('role') != 'tool':
            continue
        tool, args = calls.get(message.get('tool_call_id'), ('', {}))
        output = message.get('content')
        if not isinstance(output, str):
            continue
        rows, _ = _returned_lines(_tool_kind(tool), args, output)
        for raw, number, text in rows:
            try:
                path = (repo / raw).resolve()
                relative = path.relative_to(repo.resolve()).as_posix()
                source = path.read_text().splitlines()
                if 1 <= number <= len(source) and source[number - 1] == text:
                    lines.setdefault(relative, []).append(number)
            except (OSError, ValueError):
                pass
    return [{'path': path, 'sha256': hashlib.sha256((repo / path).read_bytes()).hexdigest(),
             'returned_ranges': merge_ranges(values)} for path, values in lines.items()]


def classify(present, value, expected, owners, owner, *, returned=True, valid=True, parsed=True):
    if expected is None:
        return 'control_correct' if valid and parsed and value is None else 'confabulation' if valid and parsed else 'unknown'
    if not returned:
        return 'not_observed'
    if present is False:
        return 'dropped'
    if not valid or not parsed:
        return 'unknown'
    if present is None:
        return 'unknown'
    if isinstance(value, str) and value == expected:
        return 'retained_recalled'
    if isinstance(value, str) and value in owners and owners[value] != owner:
        return 'misattributed'
    return 'present_not_recalled'


def source_present(repo, item, messages, retained):
    if retained is None:
        return None
    if check_requirements(repo, retained, [dict(item, scope='function')])[0]['status'] == 'returned':
        return True
    lines = (repo / item['path']).read_text().splitlines()
    source = clean('\n'.join(lines[item['start_line'] - 1:item['end_line']]))
    return any(source in clean(text) for _, text in message_texts(messages))


def assess(repo, manifest, selection, probes, ids, answer, audit, records, valid):
    main = [r for r in records if r.get('label') == 'main' and (r.get('response') or {}).get('status') == 200]
    generating = [r for r in main if (r.get('body') or {}).get('tools')]
    main = generating or main
    request = main[-1] if main else None
    messages = (request or {}).get('body', {}).get('messages', [])
    probe_requests = [r for r in records if r.get('label') == 'probe']
    probe_body = (probe_requests[0].get('body') or {}) if probe_requests else None
    probe_messages = (probe_body or {}).get('messages', [])
    if any((r.get('body') or {}).get('tools') or (r.get('response') or {}).get('tool_calls') for r in probe_requests):
        valid = False
    owners = {m['token']: identity(t) for t in manifest['targets'] for m in t['markers']}
    values = parse_batch(answer, ids)
    files = {f['path']: f for f in (audit or {}).get('files', [])}
    retained = {'files': context_ranges(messages, repo)} if request else None
    output, affected, calls = [], set(), []
    items = {identity(t): t for t in selection['selected']}
    for key, item in items.items():
        source = check_requirements(repo, retained, [dict(item, scope='function')])[0]
        row = {k: item[k] for k in ('path', 'symbol', 'start_line', 'end_line', 'critical', 'coverage')}
        row.update(source_present=source_present(repo, item, messages, retained),
                   source_missing_ranges=source['missing_ranges'], markers=[], actions=[])
        output.append(row)
    by_key = {identity(r): r for r in output}
    controls = []
    for probe in probes:
        value, parsed = values[probe['id']]
        key = identity(probe)
        if probe['kind'] == 'calls':
            correct = valid and parsed and isinstance(value, list) and all(isinstance(v, str) for v in value) and sorted(value) == probe['answer']
            calls.append({'path': probe['path'], 'symbol': probe['symbol'], 'start_line': probe['start_line'],
                          'correct': bool(correct), 'valid': valid and parsed})
            if valid and parsed and not correct:
                affected.add(key)
            continue
        presence = locate(messages, probe['needle']) if request else {'present': None}
        at_probe = locate(probe_messages, probe['needle']) if probe_body is not None else {'present': None}
        returned = bool(any(a <= probe['line'] <= b for a, b in files.get(probe['path'], {}).get('returned_ranges', []))) if probe['line'] else False
        state = classify(presence['present'], value, probe['answer'], owners, key, returned=returned, valid=valid, parsed=parsed)
        if probe['answer'] is None:
            controls.append({'path': probe['path'], 'symbol': probe['symbol'], 'role': probe['role'], 'status': state})
            continue
        row = by_key[key]
        context_changed = presence['present'] is True and at_probe['present'] is False
        if context_changed and state != 'not_observed':
            state = 'unknown'
        correct = bool(valid and parsed and value == probe['answer'])
        row['markers'].append({'position': probe['position'], 'present': presence['present'],
                               'probe_present': at_probe['present'], 'status': state, 'correct': correct,
                               'returned': returned, 'echo': 'assistant' in presence.get('roles', []),
                               'probe_context_changed': context_changed})
        if state in {'dropped', 'present_not_recalled', 'misattributed'}:
            affected.add(key)
        if state == 'misattributed':
            affected.add(owners[value])
    for key, row in by_key.items():
        states = {m['status'] for m in row['markers']}
        row['status'] = next((s for s in ('misattributed', 'dropped', 'present_not_recalled', 'not_observed', 'unknown') if s in states),
                             'retained_recalled' if states else 'unmarked')
        if any(m['status'] in {'dropped', 'present_not_recalled'} for m in row['markers']):
            row['actions'].append('reread')
        if key in affected:
            row['actions'].append('claim_review')
    for item in manifest['targets']:
        key = identity(item)
        if key in affected and key not in by_key:
            coverage = check_requirements(repo, audit, [item])[0]
            source = check_requirements(repo, retained, [item])[0]
            output.append({**{k: item[k] for k in ('path', 'symbol', 'start_line', 'end_line')},
                           'critical': False, 'coverage': coverage['status'], 'markers': [],
                           'source_present': source_present(repo, item, messages, retained),
                           'source_missing_ranges': source['missing_ranges'], 'status': 'attribution_target',
                           'actions': ['claim_review']})
    markers = [m for r in output for m in r['markers']]
    present = [m for m in markers if m['present'] is True and m['returned']]
    recallable = [m for m in present if m['probe_present'] is True]
    measured = [m for m in markers if m['present'] is not None and m['returned']]
    usage = [((r.get('response') or {}).get('usage') or {}) for r in probe_requests]
    full = [r for r in output if r['coverage'] == 'returned' and r['source_present'] is not None]
    return {'status': 'checked' if valid else 'probe_invalid', 'functions': output, 'controls': controls,
            'calls': calls, 'affected': sorted(affected), 'selection': {k: v for k, v in selection.items() if k not in {'selected', 'controls'}},
            'metrics': {'retention_rate': len(present) / len(measured) if measured else None,
                        'function_retention_rate': sum(r['source_present'] for r in full) / len(full) if full else None,
                        'recall_rate': sum(m['correct'] for m in recallable) / len(recallable) if recallable and valid else None,
                        'answer_accuracy': sum(m['correct'] for m in markers) / len(markers) if markers and valid else None,
                        'retention_denominator': len(measured), 'recall_denominator': len(recallable),
                        'probe_context_changed': sum(m['probe_context_changed'] for m in markers),
                        'misattributed': sum(m['status'] == 'misattributed' for m in markers),
                        'confabulations': sum(c['status'] == 'confabulation' for c in controls),
                        'prompt_tokens': sum(u['prompt_tokens'] for u in usage) if usage and all('prompt_tokens' in u for u in usage) else None,
                        'completion_tokens': sum(u['completion_tokens'] for u in usage) if usage and all('completion_tokens' in u for u in usage) else None,
                        'cached_tokens': sum((u.get('prompt_tokens_details') or {}).get('cached_tokens', u.get('prompt_cache_hit_tokens', 0)) for u in usage)
                        if usage and all('cached_tokens' in (u.get('prompt_tokens_details') or {}) or 'prompt_cache_hit_tokens' in u for u in usage) else None},
            'request_seq': (request or {}).get('seq'), 'request_log_available': bool(request),
            'limitations': ['marker_presence_not_full_function', 'recall_not_semantic_correctness', 'cache_hits_provider_dependent']}
