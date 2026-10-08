"""Source names and build variables must not turn into runtime SMP claims."""
from oskernel_agent.analysis.repo_facts import _probe_smp


def test_bluetooth_smp_name_does_not_confirm_multicore(tmp_path):
    path = tmp_path / 'wireless/bluetooth/bt_smp.c'
    path.parent.mkdir(parents=True)
    path.write_text('/* Bluetooth Security Manager Protocol */\nint smp_init(void) { return 0; }\n')
    facts = _probe_smp(tmp_path)
    assert facts['wakeup_present'] is True
    assert facts['evidence'] == ['wireless/bluetooth/bt_smp.c:2（smp_init）']
    assert facts['status'] == 'unverified_source_signal'
    assert '未确认' in facts['summary']
    assert facts['summary'] != '多核'


def test_inactive_architecture_name_stays_unverified(tmp_path):
    path = tmp_path / 'arch/unused/boot.c'
    path.parent.mkdir(parents=True)
    path.write_text('#if ENABLE_SECONDARY\nvoid start_secondary(void) {}\n#endif\n')
    facts = _probe_smp(tmp_path)
    assert facts['wakeup_present']
    assert '未确认' in facts['summary']
    assert '目标架构' in facts['source_note']


def test_cpu_count_does_not_confirm_runtime_topology(tmp_path):
    (tmp_path / 'Makefile').write_text('CPUS ?= 4\n')
    facts = _probe_smp(tmp_path)
    assert facts['numcpu'] == 4
    assert '未确认' in facts['summary']
    (tmp_path / 'Makefile').write_text('CPUS ?= 1\n')
    facts = _probe_smp(tmp_path)
    assert facts['numcpu'] == 1
    assert '未确认' in facts['summary']
    assert facts['summary'] != '单核'


def test_no_source_match_remains_unknown(tmp_path):
    facts = _probe_smp(tmp_path)
    assert facts['status'] == 'unknown'
    assert facts['summary'] == '未确认'
