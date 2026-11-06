"""Restore the actually executed official CodeWiki extractor; no model requests."""
import hashlib,json,pathlib,urllib.request
ROOT=pathlib.Path(__file__).resolve().parent
DEST=pathlib.Path('/home/niu/.cache/oskernel-description-interface-witness/literature');DEST.mkdir(parents=True,exist_ok=True)
REV='64c2b60f23013904e495b0c12acba9b91ceb5746'
URLS={'codewiki-c.py':f'https://raw.githubusercontent.com/FSoft-AI4Code/CodeWiki/{REV}/codewiki/src/be/dependency_analyzer/analyzers/c.py','codewiki-core.py':f'https://raw.githubusercontent.com/FSoft-AI4Code/CodeWiki/{REV}/codewiki/src/be/dependency_analyzer/models/core.py','codewiki-leaf_selection.py':f'https://raw.githubusercontent.com/FSoft-AI4Code/CodeWiki/{REV}/codewiki/src/be/dependency_analyzer/leaf_selection.py','codewiki.pdf':'https://aclanthology.org/2026.findings-acl.288.pdf','referee.pdf':'https://aclanthology.org/2026.acl-long.653.pdf'}
manifest=json.loads((ROOT/'literature-artifacts.json').read_text())
expected={pathlib.Path(e['file']).name:e['sha256'] for e in manifest['artifacts']}
for name,url in URLS.items():
 p=DEST/name
 if not p.exists():p.write_bytes(urllib.request.urlopen(url,timeout=60).read())
 if hashlib.sha256(p.read_bytes()).hexdigest()!=expected[name]:raise ValueError('artifact version mismatch '+name)
for name,source,sub in [('c.py','codewiki-c.py','analyzers'),('core.py','codewiki-core.py','models')]:
 p=DEST/'vendor/codewiki/src/be/dependency_analyzer'/sub/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((DEST/source).read_bytes())
print('Pinned official artifacts verified; no calls made.')
