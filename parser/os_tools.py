from collections import defaultdict

from parser.code_parser import classify_symbol, run_ctags, generate_level1_map


class Level2Index:
    """
    第二级索引：存储所有非噪声符号的完整信息。
    不注入 Prompt，只在 LLM 调用工具时按需返回。
    """

    def __init__(self, tags: list[dict], profile: dict, structure: dict):
        self._by_name: dict[str, list[dict]] = defaultdict(list)
        self._by_file: dict[str, list[dict]] = defaultdict(list)
        self._by_subsystem: dict[str, list[dict]] = defaultdict(list)

        primary_lang = profile["primary_lang"]

        file_to_subsystem: dict[str, str] = {}
        for subsystem, files in structure["subsystem_locations"].items():
            for entry in files:
                file_to_subsystem[entry["file"]] = subsystem

        for tag in tags:
            level = classify_symbol(tag, primary_lang)
            if level == "discard":
                continue

            entry = {
                "name":      tag["name"],
                "kind":      tag.get("kind", ""),
                "file":      tag["rel_path"],
                "line":      tag.get("line", 0),
                "signature": tag.get("signature", ""),
                "typeref":   tag.get("typeref", ""),
                "scope":     tag.get("scope", ""),
                "level":     level,
            }

            self._by_name[tag["name"]].append(entry)
            self._by_file[tag["rel_path"]].append(entry)

            sub = file_to_subsystem.get(tag["rel_path"])
            if sub:
                self._by_subsystem[sub].append(entry)

    def lookup_symbol(self, name: str) -> list[dict]:
        """按名称查找符号（可能有多个同名符号在不同文件）。"""
        return self._by_name.get(name, [])

    def list_file_symbols(self, file_path: str) -> list[dict]:
        """列出某个文件的所有符号，按行号排序。"""
        return sorted(self._by_file.get(file_path, []), key=lambda s: s["line"])

    def search_symbols(self, pattern: str) -> list[dict]:
        """模糊搜索符号名，最多返回 20 个。"""
        results = []
        for name, entries in self._by_name.items():
            if pattern.lower() in name.lower():
                results.extend(entries)
        return results[:20]

    def get_subsystem_detail(self, subsystem: str) -> str:
        """
        返回某子系统的完整符号列表（第一级地图的钻取入口）。
        按文件分组，level1 符号标 * 以示区分。
        """
        entries = self._by_subsystem.get(subsystem)
        if not entries:
            return f"（{subsystem} 子系统中未找到任何符号）"

        by_file: dict[str, list[dict]] = defaultdict(list)
        for e in entries:
            by_file[e["file"]].append(e)

        lines = [f"### {subsystem} 详情\n"]
        for file_path, syms in by_file.items():
            lines.append(f"{file_path}:")
            for s in sorted(syms, key=lambda x: x["line"]):
                marker = "*" if s["level"] == "level1" else " "
                sig_str = f"  {s['signature']}" if s["signature"] else ""
                lines.append(f"  {marker} {s['kind']} {s['name']}{sig_str}  (L{s['line']})")
        return "\n".join(lines)


def build_repo_map(
    repo_path: str,
    structure: dict,
    profile: dict,
) -> tuple[str, "Level2Index"]:
    """
    第2步主入口，串联所有环节。
    返回：(第一级地图文本, 第二级索引对象)
    """
    raw_tags = run_ctags(repo_path, structure["source_roots"])

    level1_map = generate_level1_map(raw_tags, structure, profile)

    level2_index = Level2Index(raw_tags, profile, structure)

    primary_lang = profile["primary_lang"]
    total    = len(raw_tags)
    l1_count = sum(1 for t in raw_tags if classify_symbol(t, primary_lang) == "level1")
    l2_count = sum(1 for t in raw_tags if classify_symbol(t, primary_lang) == "level2")

    print(f"  符号统计：总计 {total}，"
          f"第一级 {l1_count}，第二级 {l2_count}，"
          f"丢弃 {total - l1_count - l2_count}")
    print(f"  第一级地图预估 Token：{len(level1_map) // 3}")

    return level1_map, level2_index


