"""Executable suffix dispatch is a review clue, with explicit inference limits."""
from oskernel_agent.finals.integrity import scan_hardcode_signals


def test_suffix_dispatch_is_a_review_clue(tmp_path):
    source = tmp_path / "loader.rs"
    source.write_text('''fn load_file(path: &str) {
    if path.ends_with(".sh") {
        let new_args = vec!["/musl/busybox", "sh", path];
        return load_file(None, &new_args);
    }
    let bytes = read(path);
    parse_elf(bytes);
}
''')
    findings = scan_hardcode_signals(tmp_path)["findings"]
    suffix = [item for item in findings if "ELF 内容识别" in item["analysis"]]
    assert len(suffix) == 1
    assert suffix[0]["line"] == 2
    assert suffix[0]["path"] == "loader.rs"
    assert "不证明测试特化或违规" in suffix[0]["analysis"]


def test_suffix_logging_does_not_imply_interpreter_dispatch(tmp_path):
    (tmp_path / "loader.rs").write_text('''fn load_file(path: &str) {
    if path.ends_with(".sh") { log_name(path); }
    parse_elf(read(path));
}
''')
    assert not scan_hardcode_signals(tmp_path)["findings"]


def test_content_selected_interpreter_is_not_the_suffix_clue(tmp_path):
    (tmp_path / "loader.rs").write_text('''fn load_file(path: &str) {
    let bytes = read(path);
    if bytes.starts_with(b"#!") {
        let new_args = vec!["/bin/sh", path];
        return load_file(None, &new_args);
    }
    parse_elf(bytes);
}
''')
    assert not scan_hardcode_signals(tmp_path)["findings"]
