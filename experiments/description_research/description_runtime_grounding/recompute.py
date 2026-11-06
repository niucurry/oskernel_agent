"""Offline audit of the frozen real-runtime trace. No API/config imports."""
from __future__ import annotations
import collections
import hashlib
import json
import pathlib

P = pathlib.Path(__file__).resolve().parent


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    ledger = json.loads((P / 'ledger.json').read_text())
    results = {}
    requests = {}
    issues = []
    for row in ledger:
        i = row['id']
        request = json.loads((P / 'calls' / f'{i:02d}-request.json').read_text())
        requests[i] = request
        serialized = json.dumps(request, ensure_ascii=False).encode()
        if digest(serialized) != row['request_sha256']:
            issues.append({'call': i, 'issue': 'request hash differs'})
        for message in request['messages']:
            if message.get('role') == 'tool':
                tid = message['tool_call_id']
                content = message.get('content')
                if tid in results and results[tid] != content:
                    issues.append({'tool_id': tid, 'issue': 'inconsistent repeated result'})
                results[tid] = content

    sequence = []
    counts = collections.Counter()
    usages = []
    for row in ledger:
        i = row['id']
        parts = {}
        usage = None
        content = ''
        raw = (P / 'calls' / f'{i:02d}-response.txt').read_text()
        for line in raw.splitlines():
            if not line.startswith('data: ') or line[6:] == '[DONE]':
                continue
            chunk = json.loads(line[6:])
            if chunk.get('usage'):
                usage = chunk['usage']
            for choice in chunk.get('choices', []):
                delta = choice.get('delta', {})
                content += delta.get('content') or ''
                for tool in delta.get('tool_calls', []):
                    part = parts.setdefault(tool['index'], {'id': '', 'name': '', 'arguments': ''})
                    part['id'] += tool.get('id') or ''
                    part['name'] += tool.get('function', {}).get('name') or ''
                    part['arguments'] += tool.get('function', {}).get('arguments') or ''
        if usage != row.get('usage'):
            issues.append({'call': i, 'issue': 'usage differs from raw provider response'})
        usages.append(usage)
        for _, part in sorted(parts.items()):
            counts[part['name']] += 1
            try:
                arguments = json.loads(part['arguments'])
            except ValueError:
                arguments = {'unparsed': part['arguments']}
                issues.append({'call': i, 'issue': 'unparsed tool arguments'})
            result = results.get(part['id'])
            sequence.append({'request': i, 'tool_id': part['id'], 'tool': part['name'],
                             'arguments': arguments, 'result_seen_in_later_request': result is not None,
                             'result_sha256': digest(str(result).encode()) if result is not None else None,
                             'result_prefix': str(result)[:200] if result is not None else None})

    frozen = json.loads((P / 'freeze.json').read_text())
    historical = []
    for name, expected in frozen['files'].items():
        path = pathlib.Path(name)
        actual = digest(path.read_bytes()) if path.exists() else None
        if actual != expected:
            historical.append({'path': name, 'frozen_sha256': expected, 'current_sha256': actual})
    actual_total = sum((u or {}).get('total_tokens', 0) for u in usages)
    result = {'requests': len(ledger), 'reported_tokens': actual_total,
              'unknown_requests': sum(u is None for u in usages), 'trace_integrity_issues': issues,
              'tool_counts': dict(counts), 'unique_tool_calls': len(sequence),
              'reference_initialization_error_results': sum('尚未初始化仓库' in str(x) for x in results.values()),
              'historical_source_changes_since_freeze': historical,
              'historical_source_change_note': 'Later production reference-validation fix must not rewrite the failed-run freeze.',
              'semantic_quality': 'unmeasured; partial text is retained without an assistant or model truth score',
              'sequence': sequence}
    (P / 'offline-audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    assert not issues, issues
    assert actual_total == 110727
    print(json.dumps({k: v for k, v in result.items() if k != 'sequence'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
