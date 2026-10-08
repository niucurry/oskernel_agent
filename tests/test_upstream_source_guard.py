import copy
import pytest

from oskernel_agent.comparison.metadata.config import MetadataSettings
from oskernel_agent.comparison.metadata.runner import channel_baseline
from oskernel_agent.comparison.normalize.source_identity import (
    complete_source_reference, reference_covers_query, source_token_fingerprint,
)
from oskernel_agent.comparison.report import upstream_baselines as UB
from oskernel_agent.comparison.report import semantic_compare as SC


def pair(code='fn check() -> i32 { -38 }', baseline=False):
    q=dict(repo_id='2026/query', file_path='arceos/modules/axhal/x.rs',
           start_line=1, end_line=3, func_name='check', lang='rust', raw_code=code)
    c=dict(q, repo_id='0/baseline_arceos' if baseline else '2025/team')
    return dict(query_func=q, candidate_func=c, tier='confirmed', final_score=1.0,
        evidence=dict(line_similarity=1.0, exact_match_lines=3,
                      function_identity_relation='exact_counterpart'))


class NoDiscovery:
    def match(self, code):return None, 0.0


def test_matching_paths_and_peer_code_do_not_prove_upstream():
    history=pair()
    UB.tag_upstream_baselines([history])
    assert history['upstream_path_hint']=='arceos'
    assert not history.get('upstream_vendored')
    assert not SC._is_excluded_pair(history)


def test_explicit_unchanged_upstream_retains_exclusion_and_actual_source():
    history=pair();base=pair(baseline=True)
    UB.tag_upstream_baselines([history,base])
    assert history['upstream_vendored']=='arceos'
    rows=UB.upstream_baseline_stats([history,base])
    assert len(rows)==1
    assert rows[0]['source']['repo']=='0/baseline_arceos'


def test_literal_change_survives_even_with_normalized_fingerprint_flag():
    history=pair('fn check() -> i32 { 0 }')
    base=pair(baseline=True);base['query_func']=copy.deepcopy(history['query_func'])
    base['evidence']['normalized_fingerprint_match']=True
    channel_baseline({'suspects':[history,base]},NoDiscovery(),MetadataSettings())
    SC._tag_query_level_baselines([history,base]);UB.tag_upstream_baselines([history,base])
    assert history['tier']=='confirmed'
    assert history['evidence']['baseline_coverage_incomplete']
    assert not SC._is_excluded_pair(history)


def test_old_partial_baseline_exclusion_is_recovered_for_review():
    history=pair('fn check() -> i32 { 0 }');history['tier']='baseline_derived'
    history['evidence'].update(baseline_flag=True,baseline_query_scope=True)
    assert SC._tag_query_level_baselines([history])==1
    assert history['tier']=='review'
    assert history['evidence']['baseline_coverage_incomplete']
    assert not history['evidence']['baseline_query_scope']


def test_bound_reference_cannot_clear_a_changed_query():
    base=pair(baseline=True)
    reference=complete_source_reference(base)
    assert reference_covers_query(base['query_func'],reference)
    altered=pair('fn check() -> i32 { 0 }')
    assert not reference_covers_query(altered['query_func'],reference)


@pytest.mark.parametrize('legacy', [False, True])
def test_reference_for_another_body_at_same_location_cannot_clear_query(legacy):
    base=pair(baseline=True)
    history=pair('fn check() -> i32 { 0 }')
    if legacy:
        history['tier']='baseline_derived'
        history['evidence'].update(baseline_flag=True,baseline_query_scope=True)
    channel_baseline({'suspects':[history,base]},NoDiscovery(),MetadataSettings())
    SC._tag_query_level_baselines([history,base])
    UB.tag_upstream_baselines([history,base])
    assert history['tier']==('review' if legacy else 'confirmed')
    assert not SC._is_excluded_pair(history)


def test_path_rule_preserves_prior_incremental_evidence():
    history=pair();base=pair(baseline=True)
    history['evidence']['baseline_incremental_evidence']=True
    UB.tag_upstream_baselines([history,base])
    assert not history.get('upstream_vendored')


@pytest.mark.parametrize('lang,original,cosmetic,altered',[
    ('c','int f(void) { return 0; }','/* source */ int f( void ){ return 0; }','int f(void) { return 1; }'),
    ('rust','fn f() { log!("// literal"); }','/* outer /* inner */ end */ fn f(){log!("// literal");}',
     'fn f() { log!("// changed"); }'),
])
def test_source_identity_preserves_literals_and_understands_comments(lang,original,cosmetic,altered):
    first=source_token_fingerprint(original,lang)
    assert first is not None and first==source_token_fingerprint(cosmetic,lang)
    assert first!=source_token_fingerprint(altered,lang)


@pytest.mark.parametrize('code,lang,name',[
    ('fn check() {','rust','check'),
    ('fn other() {}','rust','check'),
    ('int value;','c','value'),
    ('fn check() {} fn other() {}','rust','check'),
    ('void check() {}','cpp','check'),
])
def test_partial_mismatched_or_unsupported_source_is_not_complete(code,lang,name):
    candidate=pair(baseline=True)
    for key in ['query_func','candidate_func']:
        candidate[key].update(raw_code=code,lang=lang,func_name=name)
    assert complete_source_reference(candidate) is None


def test_report_displays_unresolved_upstream_evidence():
    history=pair();UB.tag_upstream_baselines([history])
    candidate=SC._candidate_from_suspect(history)
    html=SC._candidates_cell({'candidates':[candidate]},None)
    assert '存在上游线索，完整来源待核对' in html


def test_assembly_reference_requires_exact_named_segment():
    base=pair(baseline=True)
    for key in ['query_func','candidate_func']:
        base[key].update(lang='asm',func_name='_entry',raw_code='_entry:\n li a0, 0\n ret\n')
    ref=complete_source_reference(base)
    assert ref and reference_covers_query(base['query_func'],ref)
    changed=dict(base['query_func'],raw_code='_entry:\n li a0, 1\n ret\n')
    assert not reference_covers_query(changed,ref)
    combined=copy.deepcopy(base)
    for key in ['query_func','candidate_func']:
        combined[key]['raw_code']+='another:\n ret\n'
    assert complete_source_reference(combined) is None
