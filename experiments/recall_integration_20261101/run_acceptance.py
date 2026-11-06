import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = Path('/home/niu/agent-recall-probe-20261029/original/T202510486995194-334')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true', help='调用付费 API；仅在人工确认后使用')
    parser.add_argument('--model', default='deepseek-flash')
    args = parser.parse_args()
    plan = {'version': 1, 'plan_id': 'sleeplock-recall-acceptance', 'items': [{
        'id': 'lock-owner-state', 'dimension': '功能性', 'score_impact': 20,
        'question': '获取、释放和检查睡眠锁时，如何更新和读取锁状态与持有者信息？',
        'scope': 'kernel/sleeplock.c 的睡眠锁状态管理源码路径',
        'decision_rule': '区分源码事实、上下文保留与运行行为；没有执行测试时不宣称运行验证通过。',
        'score_effect': '本次不计分。', 'subsystems': ['同步机制'],
        'evidence': [{'id': name, 'purpose': '核对状态更新及调用关系', 'path': 'kernel/sleeplock.c', 'symbol': name}
                     for name in ['initsleeplock', 'acquiresleep', 'releasesleep', 'holdingsleep']]}]}
    (ROOT / 'acceptance-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    from oskernel_agent.analysis.evidence_plan import load_evidence_plan
    snapshot = load_evidence_plan(REPO, ROOT / 'acceptance-plan.json')
    (ROOT / 'acceptance-plan.snapshot.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    spec = {'repo': str(REPO), 'subsystem': '同步机制', 'files': ['kernel/sleeplock.c', 'kernel/spinlock.c'],
            'model': args.model, 'maximum_probe_functions': 12, 'maximum_additional_recall_rereads': 2,
            'sample_ratio': 0.15, 'calls_probe': True, 'paid_api_requires_confirmation': True,
            'normal_path': '1 个 SUBSYS 报告会话、1 次批量提问、2 个源码复核会话；每个会话可能包含多次 API 请求',
            'conditional_cost': '缺证据和召回异常可补读；语义分歧可追加复核，报告最多修订一次；供应商实际计费，未预设金额上限'}
    (ROOT / 'acceptance-spec.json').write_text(json.dumps(spec, ensure_ascii=False, indent=2))
    if not args.run:
        print(json.dumps(spec, ensure_ascii=False, indent=2))
        return
    from oskernel_agent import config
    from oskernel_agent.cli.recall_probe import _opencode_config
    from oskernel_agent.cli.setup_opencode import _build_session_prompt
    from oskernel_agent.prompts.builder import SessionType
    from oskernel_agent.analysis.claim_review import REVIEW_PROMPT
    from oskernel_agent.engines import llm_batch
    from oskernel_agent.engines.request_recorder import RecordingProxy, compress_logs
    from oskernel_agent.pipeline.tree_builder import run_subsys_stage
    from oskernel_agent.reports.html_tree import _render_description_scope, _render_claim_reviews
    if not config.api.get('key'):
        raise RuntimeError('本机 config.toml 缺少 API 配置')
    run = ROOT / ('acceptance-' + uuid.uuid4().hex[:8])
    run.mkdir(mode=0o700)
    work = run / 'work'
    work.mkdir()
    config_dir = run / 'config'
    config_dir.mkdir()
    (run / 'tmp').mkdir()
    before = {str(p.relative_to(REPO)): __import__('hashlib').sha256(p.read_bytes()).hexdigest()
              for p in REPO.rglob('*') if p.is_file() and not p.is_symlink() and '.git' not in p.relative_to(REPO).parts}
    with RecordingProxy(config.api['base_url'], config.api['key'], run / 'all-model-requests') as proxy:
        cfg = _opencode_config(proxy.base_url, args.model, REPO, 65536, 8192, 'auto')
        permissions = cfg['agent']['read-audit-trial']['permission']
        cfg['agent'] = {'os-kernel-subsys': {'mode': 'primary', 'prompt': _build_session_prompt(SessionType.SUBSYS), 'permission': permissions},
                        'os-kernel-claim-review': {'mode': 'primary', 'prompt': REVIEW_PROMPT, 'permission': permissions}}
        (config_dir / 'opencode.json').write_text(json.dumps(cfg, ensure_ascii=False))
        (config_dir / 'opencode.json').chmod(0o600)
        os.environ.update(AGENT_RECALL_CHECK='1', AGENT_RECALL_DIR=str(run / 'recall'),
                          AGENT_RECALL_MAX_FUNCTIONS='12', AGENT_RECALL_SAMPLE_RATIO='0.15', AGENT_RECALL_CALLS='1',
                          AGENT_EVIDENCE_PLAN=str(ROOT / 'acceptance-plan.json'),
                          AGENT_OPENCODE_SOURCE_CONFIG_DIR=str(config_dir), AGENT_OPENCODE_SOURCE_DATA_DIR=str(run / 'auth'),
                          AGENT_OPENCODE_DATA_ROOT=str(run / 'review-data'), AGENT_READ_AUDIT_DIR=str(run / 'reads'),
                          AGENT_CLAIM_REVIEW_DIR=str(run / 'reviews'), TMPDIR=str(run / 'tmp'),
                          OPENCODE_DISABLE_PROJECT_CONFIG='true', OPENCODE_DISABLE_DEFAULT_PLUGINS='true',
                          OPENCODE_DISABLE_AUTOUPDATE='true', OPENCODE_DISABLE_MODELS_FETCH='true')
        llm_batch._OPENCODE_DATA_ROOT = None
        files = [{'path': p, 'name': Path(p).name, 'lang': 'c', 'size': (REPO / p).stat().st_size,
                  'mtime': (REPO / p).stat().st_mtime_ns} for p in spec['files']]
        tree = {'type': 'root', 'name': REPO.name, 'children': [
            {'type': 'subsystem', 'name': '同步机制', 'path': '<subsys>/同步机制', 'files': files, 'children': []}]}
        try:
            run_subsys_stage(tree, REPO, work, None)
            (run / 'tree.json').write_text(json.dumps(tree, ensure_ascii=False, indent=2))
            html = '<!doctype html><meta charset="utf-8"><title>睡眠锁 SUBSYS 召回验收</title><style>body{font:16px/1.6 sans-serif;margin:30px}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:8px;vertical-align:top}</style>'
            html += tree['children'][0].get('content', '') + _render_description_scope({'tree': tree}) + _render_claim_reviews({'tree': tree})
            (run / 'report.html').write_text(html)
        finally:
            shutil.rmtree(run / 'tmp', ignore_errors=True)
            for path in (run / 'review-data').glob('*/config/opencode/node_modules'):
                shutil.rmtree(path, ignore_errors=True)
    compress_logs(run / 'all-model-requests')
    after = {name: __import__('hashlib').sha256((REPO / name).read_bytes()).hexdigest() for name in before}
    (run / 'source-check.json').write_text(json.dumps({'unchanged': before == after}, indent=2))
    print(str(run))


if __name__ == '__main__':
    main()
