"""Fetch small primary-source records only; never imports model config."""
from __future__ import annotations
import concurrent.futures
import base64
import hashlib
import json
import pathlib
import urllib.request

P = pathlib.Path(__file__).resolve().parent / 'literature'
P.mkdir(exist_ok=True)
SOURCES = [
    ('docsearch-paper.pdf', 'https://people.cs.vt.edu/penggao/papers/escaping-icml26.pdf'),
    ('controlled-documentation.html', 'https://arxiv.org/html/1311.2702'),
    ('refocusing.html', 'https://onlinelibrary.wiley.com/doi/10.1002/smr.70157'),
]


def fetch(item):
    name, url = item
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'OS-description-research/1.0'})
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read(4_000_001)
            assert len(raw) <= 4_000_000, 'size ceiling'
        (P / name).write_bytes(raw)
        return {'name': name, 'url': url, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    except Exception as exc:
        return {'name': name, 'url': url, 'failure_type': type(exc).__name__, 'failure': str(exc)[:240]}


def github_documents(repo, paths):
    rows = []
    try:
        commit = json.loads(urllib.request.urlopen('https://api.github.com/repos/' + repo + '/git/ref/heads/main', timeout=25).read())['object']['sha']
        prefix = repo.replace('/', '--')
        def one(path):
            url = 'https://api.github.com/repos/' + repo + '/contents/' + path + '?ref=' + commit
            try:
                item = json.loads(urllib.request.urlopen(url, timeout=25).read(1_500_001))
                assert item.get('encoding') == 'base64'
                raw = base64.b64decode(item['content'])
                assert len(raw) <= 1_000_000
                git_sha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
                assert git_sha == item['sha']
                name = prefix + '/' + path
                target = P / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                return {'name': name, 'url': url, 'repository': repo, 'commit': commit, 'git_blob_sha': git_sha, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
            except Exception as exc:
                return {'repository': repo, 'commit': commit, 'path': path, 'failure_type': type(exc).__name__, 'failure': str(exc)[:240]}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows.extend(pool.map(one, paths))
    except Exception as exc:
        rows.append({'repository': repo, 'failure_type': type(exc).__name__, 'failure': str(exc)[:240]})
    return rows


def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(fetch, SOURCES))
    rows += github_documents('ccsnow127/docsearch', ['README.md', 'docs/architecture.md', 'docs/languages.md', 'pyproject.toml'])
    rows += github_documents('Qinyun718/repoprobe', ['README.md', 'evaluator.py', 'scorer.py', 'dataset_loader.py', 'prompt_templates_en.py', 'repos_info.json'])
    rows += github_documents('apcl-research/improving_summary', ['README.md'])
    (P.parent / 'recent-primary-retrieval.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'records': len(rows), 'failures': [r for r in rows if 'failure_type' in r], 'bytes': sum(r.get('bytes', 0) for r in rows)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
