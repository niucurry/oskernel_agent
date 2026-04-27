import sqlite3
import json
from collections import defaultdict

from code_parser import classify_symbol


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


class OSCodeTools:
    def __init__(self, db_path="./data/os_knowledge_graph.db"):
        self.conn = sqlite3.connect(db_path)

    def get_struct_definition(self, repo_id: str, struct_name: str) -> str:
        """获取某个 OS 核心数据结构（如 PCB、trapframe）的完整定义代码"""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT code_content FROM Symbols WHERE repo_id=? AND symbol_type='struct' AND symbol_name=?", 
            (repo_id, struct_name)
        )
        result = cursor.fetchone()
        return result[0] if result else f"未在 {repo_id} 中找到 {struct_name} 的定义。"

    def get_callees(self, repo_id: str, function_name: str) -> list | str:
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT callee_name FROM CallGraph WHERE repo_id=? AND caller_name=?", 
            (repo_id, function_name)
        )
        results = cursor.fetchall()
        if not results:
            return f"警告：在数据库中未找到名为 '{function_name}' 的函数，可能是函数名错误，或该文件未被解析。"
        return [row[0] for row in results]

    def search_keyword_in_docs(self, repo_id: str, keyword: str) -> str:
        """在文档中检索关键字（占位：后续可接入向量数据库）"""
        return f"假设在此处返回了 {repo_id} 中关于 {keyword} 的文档描述。"