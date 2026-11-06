"""Compiler-symbol sanity check of the original extractor, no model calls."""
import pathlib, sys, types, json, hashlib, subprocess, importlib.util
from oskernel_agent.analysis.source_inventory import quick_symbols

P = pathlib.Path(__file__).resolve().parent
for name in ['src', 'src.analysis', 'src.analysis.models']:
    package = types.ModuleType(name)
    package.__path__ = []
    sys.modules[name] = package

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

load('src.analysis.models.core', P / 'author/repodoc/src/analysis/models/core.py')
analyzer = load('author_c', P / 'author/repodoc/src/analysis/analyzers/c.py').TreeSitterCAnalyzer
source = P / 'fixture.c'
commands = [['/usr/bin/gcc-12', '-std=c11', '-Werror', '-c', str(source), '-o', str(P / 'fixture.o')], ['nm', '--defined-only', '--format=posix', str(P / 'fixture.o')]]
records = []
for command in commands:
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    records.append({'command': command, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
    if result.returncode: break
if len(records) == 2 and records[-1]['returncode'] == 0:
    compiler_names = sorted(line.split()[0] for line in records[-1]['stdout'].splitlines() if line.split()[1] in ['T', 't'])
else:
    compiler_names = None
instance = analyzer(str(source), source.read_text(), str(P))
official = sorted(n.name for n in instance.nodes if n.component_type == 'function')
inventory = quick_symbols(source.read_text(), '.c')
result = {'compiler_symbols': compiler_names, 'original_author_function_names': official, 'production_inventory': inventory, 'commands': records, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'model_calls': 0, 'boundary': 'Only this valid C fixture was compiled; public OS slices were not built. This verifies a structural failure, not prose quality.'}
(P / 'fixture-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False, indent=2))
