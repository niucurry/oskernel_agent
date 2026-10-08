import sqlite3

import pytest

from oskernel_agent.comparison.exact.matcher import normalized_file_hash, raw_file_hash
from oskernel_agent.comparison.fastpath.scan import scan_repo
from oskernel_agent.comparison.normalize.runner import normalize_repo
from oskernel_agent.comparison.normalize.store import FunctionStore
from oskernel_agent.comparison.report import upstream_baselines as UB
from oskernel_agent.comparison.report import semantic_compare as SC


SOURCES = {
    'rust': 'fn probe() -> i32 {\n let url = "https://upstream.example/old";\n let a = 1;\n let b = 2;\n a + b\n}\n',
    'c': 'int probe(void) {\n const char *url = "https://upstream.example/old";\n int a = 1;\n int b = 2;\n return a + b;\n}\n',
}


@pytest.mark.parametrize('lang', ['c', 'rust'])
def test_legacy_regex_collision_cannot_hide_changed_url(tmp_path, lang):
    original = SOURCES[lang]
    changed = original.replace('upstream.example/old', 'team.example/new')
    assert normalized_file_hash(original, lang) == normalized_file_hash(changed, lang)
    relative = 'arceos/modules/axhal/probe.' + ('c' if lang=='c' else 'rs')
    db = tmp_path/'functions.db'
    with FunctionStore(db) as store:
        for name, source, repo_id in [('base', original, '0/baseline_arceos'),
                                      ('peer', changed, '2025/peer')]:
            repo = tmp_path/name
            target = repo/relative
            target.parent.mkdir(parents=True)
            target.write_text(source)
            normalize_repo(repo, store, repo_id=repo_id)
    query = tmp_path/'query'
    target = query/relative
    target.parent.mkdir(parents=True)
    target.write_text(changed)
    result = scan_repo(query, repo_id='2026/query', db_path=db, output_dir=tmp_path/'out')
    assert result['common_files'] == 0
    assert result['rejected_hash_candidates'] == 1
    assert len(result['matched_files']) == 1
    assert [m['repo_id'] for m in result['matched_files'][0]['matches']] == ['2025/peer']
    assert UB.is_excluded_file_path(relative) is None
    metrics = SC._historical_source_metrics([], result['matched_files'])
    assert metrics[0]['repo'] == '2025/peer' and metrics[0]['exact_files'] == 1


@pytest.mark.parametrize('cosmetic', [False, True])
def test_legacy_index_accepts_raw_identity_and_retains_unverified_format(tmp_path, cosmetic):
    original = SOURCES['rust']
    code = ('// new comment\n' + original) if cosmetic else original
    db = tmp_path/'functions.db'
    with FunctionStore(db) as store:
        store.add_file('0/baseline_arceos', 'src/probe.rs', 'rust', 7, 1,
                       normalized_file_hash(original, 'rust'), raw_file_hash(original))
        store.conn.commit()
    query = tmp_path/'query'
    (query/'src').mkdir(parents=True)
    (query/'src/probe.rs').write_text(code)
    result = scan_repo(query, repo_id='2026/query', db_path=db, output_dir=tmp_path/'out')
    assert result['common_files'] == int(not cosmetic)
    assert result['unverified_hash_candidates'] == int(cosmetic)
    assert bool(result['skip_files']) is (not cosmetic)


def test_old_file_schema_migrates_without_inventing_source_identity(tmp_path):
    db = tmp_path/'functions.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE files (id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id TEXT NOT NULL, '
                     'file_path TEXT NOT NULL, lang TEXT NOT NULL, line_count INTEGER NOT NULL, '
                     'func_count INTEGER NOT NULL, norm_hash TEXT NOT NULL, raw_hash TEXT NOT NULL)')
        conn.execute("INSERT INTO files VALUES(1, '0/baseline_arceos', 'src/x.rs', 'rust', 7, 1, 'norm', 'raw')")
    for _ in range(2):
        with FunctionStore(db) as store:
            rows = store.find_files_by_norm_hash('norm')
            assert len(rows) == 1 and rows[0]['raw_hash'] == 'raw'
            assert rows[0]['source_hash'] is None
