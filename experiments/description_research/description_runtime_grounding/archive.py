"""Preserve this run, scan for the private credential, and read/hash every member."""
from __future__ import annotations
import datetime
import hashlib
import json
import pathlib
import tarfile

P = pathlib.Path(__file__).resolve().parent


def main():
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = pathlib.Path('/home/niu/agent-archives') / ('description-runtime-grounding-' + stamp)
    target.mkdir()
    key = json.loads(pathlib.Path('/home/niu/.config/oskernel-research/deepseek.json').read_text())['api_key'].encode()
    rows = []
    files = []
    for q in sorted(P.rglob('*')):
        if not q.is_file() or '__pycache__' in q.parts or q.name == 'preservation.json':
            continue
        raw = q.read_bytes()
        assert key not in raw, 'private credential detected; abort preservation'
        rel = q.relative_to(P).as_posix()
        rows.append({'path': rel, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        files.append(q)
    archive = target / 'experiment.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for q in files:
            tar.add(q, arcname=q.relative_to(P).as_posix(), recursive=False)
    mapping = {r['path']: r for r in rows}
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        assert len(members) == len(rows)
        for member in members:
            assert member.isfile()
            stream = tar.extractfile(member)
            assert stream is not None
            raw = stream.read()
            assert len(raw) == mapping[member.name]['bytes']
            assert hashlib.sha256(raw).hexdigest() == mapping[member.name]['sha256']
            assert key not in raw
    manifest = {'files': rows, 'archive': str(archive), 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'archive_bytes': archive.stat().st_size, 'verified_members': len(rows),
                'private_credential_absent': True, 'runtime_cpu_seconds': 9.577827,
                'semantic_quality': 'unmeasured; failed complete delivery retained',
                'budget': {'requests': 10, 'reported_tokens': 110727, 'unknown_requests': 0}}
    (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    receipt = {k: v for k, v in manifest.items() if k != 'files'}
    (P / 'preservation.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
