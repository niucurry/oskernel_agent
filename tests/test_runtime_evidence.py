from oskernel_agent.finals.__main__ import main
from oskernel_agent.finals.integrity import analyze_log
from oskernel_agent.finals.runtime_evidence import audit_runtime_log, parse_utest_log


def run_log(*, failure=False, count=1, complete=True, name="kernel.thread"):
    lines = ["[==========] [ utest ] started", f"utest unit name: ({name}_unit)"]
    if failure:
        lines.append("[ ASSERT ] [ unit ] at (thread.c); func: (create:65); msg: (false)")
    if count:
        lines.append(f"[ PASSED ] [ result ] testcase ({name})")
    lines.append(f"[==========] [ utest ] {count} tests from 10 testcase ran.")
    if complete:
        lines.append("[==========] [ utest ] finished")
    return "\n".join(lines) + "\n"


def test_failure_cannot_be_erased_by_passing_summary():
    result = parse_utest_log(run_log(failure=True))
    assert result["status"] == "inconsistent"
    assert result["reported_status"] == "passed"
    assert result["failure_evidence"][0]["line"] == 3


def test_new_complete_run_has_separate_scope():
    result = parse_utest_log(run_log(failure=True) + run_log())
    assert result["status"] == "passed"
    assert result["earlier_runs"] == 1
    assert not result["failure_evidence"]


def test_incomplete_latest_run_is_unknown():
    assert parse_utest_log(run_log() + run_log(complete=False))["status"] == "unknown"


def test_zero_cases_and_missing_count_are_unknown():
    assert parse_utest_log(run_log(count=0))["status"] == "unknown"
    assert parse_utest_log(run_log().replace("1 tests from 10 testcase ran.", ""))["status"] == "unknown"


def test_impossible_count_or_duplicate_result_is_unknown():
    assert parse_utest_log(run_log().replace("from 10 testcase", "from 0 testcase"))["status"] == "unknown"
    line = "[ PASSED ] [ result ] testcase (kernel.thread)\n"
    assert parse_utest_log(run_log().replace(line, line + line))["status"] == "unknown"


def test_failure_from_outside_this_run_does_not_count():
    assert parse_utest_log("[ ASSERT ] [ unit ] old diagnostic\n" + run_log())["status"] == "passed"


def test_terminal_color_does_not_hide_assertion():
    text = run_log(failure=True).replace("[ ASSERT ]", "\x1b[31m[ ASSERT ]\x1b[0m")
    assert parse_utest_log(text)["status"] == "inconsistent"


def test_final_failure_is_failed():
    assert parse_utest_log(run_log(failure=True).replace("[ PASSED ]", "[ FAILED ]"))["status"] == "failed"


def test_cli_writes_safe_report_and_preserves_source(tmp_path):
    log = tmp_path / "run.log"
    text = run_log(failure=True, name="<script>alert(1)</script>")
    log.write_text(text)
    output = tmp_path / "audit.html"
    assert main(["audit-run", "--log", str(log), "--output", str(output)]) == 1
    assert "<script>" not in output.read_text()
    assert "&lt;script&gt;" in output.read_text()
    assert log.read_text() == text
    assert audit_runtime_log(log)["status"] == "inconsistent"
    assert analyze_log(log, kind="run")["status"] == "inconsistent"


def test_unrecognized_log_does_not_establish_success(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("all tests passed\n")
    assert audit_runtime_log(log)["status"] == "unknown"


def test_cli_returns_unknown_and_preserves_log_when_output_is_input(tmp_path):
    log = tmp_path / "run.log"
    text = run_log()
    log.write_text(text)
    assert main(["audit-run", "--log", str(log), "--output", str(log)]) == 2
    assert log.read_text() == text
    assert main(["audit-run", "--log", str(log), "--output", str(tmp_path / "ok.json")]) == 0
    log.write_text(run_log(count=0))
    assert main(["audit-run", "--log", str(log), "--output", str(tmp_path / "unknown.json")]) == 2


def test_cli_cannot_overwrite_input_via_hardlink(tmp_path):
    log = tmp_path / "run.log"
    text = run_log()
    log.write_text(text)
    alias = tmp_path / "alias.json"
    alias.hardlink_to(log)
    assert main(["audit-run", "--log", str(log), "--output", str(alias)]) == 2
    assert log.read_text() == text
