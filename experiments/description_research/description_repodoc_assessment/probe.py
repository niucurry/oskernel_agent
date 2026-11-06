"""Run the unchanged author C component extractor, not RepoDoc generation.

The author's absolute `src` import is resolved to its downloaded original core
module. Neither the extractor nor its model classes are edited. Relationships
produced internally are discarded; this study only inspects description inputs.
"""
from __future__ import annotations
import pathlib, json, hashlib, importlib.util, sys, types, subprocess, time, resource

ROOT = pathlib.Path(__file__).resolve().parent
SOURCES = pathlib.Path('/home/niu/agent-research/description_responsibility_budget/sources')

def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')

def load_original(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'author' / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def main():
    protocol = json.loads((ROOT / 'protocol.json').read_text())
    paths = [ROOT / 'protocol.json', ROOT / 'probe.py', ROOT / 'retrieval.json']
    paths += sorted((ROOT / 'author').rglob('*.py'))
    dump(ROOT / 'freeze.json', {'files': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}, 'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    for name in ['src', 'src.analysis', 'src.analysis.models']:
        package = types.ModuleType(name)
        package.__path__ = []
        sys.modules[name] = package
    load_original('src.analysis.models.core', 'repodoc/src/analysis/models/core.py')
    analyzer = load_original('original_repodoc_c', 'repodoc/src/analysis/analyzers/c.py').TreeSitterCAnalyzer
    rows = []
    start = time.monotonic()
    for repo in protocol['repositories']:
        base = SOURCES / repo
        files = sorted(base.rglob('*.c'))
        reference = subprocess.run(['ctags', '--output-format=json', '--fields=+neK', '--kinds-C=f', '-f', '-'] + [str(p) for p in files], capture_output=True, text=True, check=True, timeout=60)
        expected = set()
        for line in reference.stdout.splitlines():
            tag = json.loads(line)
            if tag.get('kind') == 'function' and tag.get('end'):
                expected.add((str(pathlib.Path(tag['path']).relative_to(base)), tag['name'], tag['line']))
        extracted = set()
        for path in files:
            instance = analyzer(str(path), path.read_text(), str(base))
            for node in instance.nodes:
                if node.component_type == 'function':
                    extracted.add((node.relative_path, node.name, node.start_line))
        hit = expected & extracted
        rows.append({'repository': repo, 'c_files': len(files), 'ctags_definition_locations': len(expected), 'author_function_locations': len(extracted), 'intersection': len(hit), 'ctags_only': sorted(expected - extracted), 'author_only': sorted(extracted - expected), 'agreement_recall': len(hit) / max(1, len(expected)), 'agreement_precision': len(hit) / max(1, len(extracted))})
        print(repo, 'Ctags', len(expected), 'author', len(extracted), 'intersection', len(hit), flush=True)
    own = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = {'rows': rows, 'model_requests': 0, 'wall_seconds': time.monotonic() - start, 'cpu_seconds_self_and_children': own.ru_utime + own.ru_stime + child.ru_utime + child.ru_stime, 'scope': protocol['scope'], 'truth_boundary': protocol['truth_boundary'], 'generation_quality': 'unmeasured'}
    dump(ROOT / 'results.json', result)

if __name__ == '__main__':
    main()
