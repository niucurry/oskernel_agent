"""Record source inventory and report references without claiming code was read."""

from __future__ import annotations


def description_scope(files: list[dict], prompt_files: list[dict] | None, modules: list[dict]) -> dict:
    indexed = {str(item["path"]).replace("\\", "/") for item in files if item.get("path")}
    prompted = {str(item["path"]).replace("\\", "/") for item in (prompt_files or []) if item.get("path")}
    representatives = {
        str(path).replace("\\", "/") for module in modules for path in module.get("file_paths", [])
    }
    return {
        "indexed_files": len(indexed),
        "prompted_files": len(prompted & indexed) if prompt_files is not None else None,
        "representative_files": len(representatives & indexed),
        "unrepresented_files": len(indexed - representatives),
        "additional_representative_files": len(representatives - indexed),
        "source_read_coverage": "unknown",
    }
