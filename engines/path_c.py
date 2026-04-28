from pathlib import Path

import tree_sitter_c as tsc
import tree_sitter_rust as tsrust
from tree_sitter import Language, Parser

from engines.base import AnalysisEngine


class TreeSitterEngine(AnalysisEngine):
    """
    路径 C：纯 AST 语法分析，不需要任何编译环境
    精度低于 LSP，但永远不会失败
    """

    def __init__(self, repo_path: str, lang: str):
        self.repo_path = repo_path
        self.lang = lang

        if lang == "c":
            self.language = Language(tsc.language())
            self._func_node_type = "function_definition"
            self._struct_node_type = "struct_specifier"
            self._call_node_type = "call_expression"
            self._src_ext = ".c"
        elif lang == "rust":
            self.language = Language(tsrust.language())
            self._func_node_type = "function_item"
            self._struct_node_type = "struct_item"
            self._call_node_type = "call_expression"
            self._src_ext = ".rs"
        else:
            raise ValueError(f"不支持的语言：{lang}")

        self.parser = Parser(self.language)
        self._func_index: dict[str, dict] = {}
        self._struct_index: dict[str, dict] = {}
        self._build_index()

    def _build_index(self):
        """扫描所有源文件，建立函数级和结构体级内存索引。"""
        count = 0
        for src_file in Path(self.repo_path).rglob(f"*{self._src_ext}"):
            rel = str(src_file.relative_to(self.repo_path))
            if any(skip in rel for skip in ["vendor", "third_party", "target"]):
                continue
            try:
                source = src_file.read_bytes()
                tree = self.parser.parse(source)
                source_str = source.decode("utf-8", errors="replace")
                self._index_functions(tree.root_node, source_str, rel)
                self._index_structs(tree.root_node, source_str, rel)
                count += 1
            except Exception:
                continue

        print(f"[路径C] tree-sitter 索引完成：{count} 个文件，"
              f"{len(self._func_index)} 个函数，"
              f"{len(self._struct_index)} 个结构体")

    def _index_functions(self, root_node, source: str, file_path: str):
        """遍历 AST 提取所有函数定义。"""
        def traverse(node):
            if node.type == self._func_node_type:
                name = self._extract_func_name(node, source)
                if name:
                    body = source[node.start_byte:node.end_byte]
                    calls = self._extract_calls(node, source)
                    # 同名函数用 name@file 的 key 去重，查询时通过 go_to_definition 消歧
                    key = name if name not in self._func_index else f"{name}@{file_path}"
                    self._func_index[key] = {
                        "name":       name,
                        "file":       file_path,
                        "start_line": node.start_point[0] + 1,
                        "end_line":   node.end_point[0] + 1,
                        "body":       body,
                        "calls":      calls,
                    }
            for child in node.children:
                traverse(child)

        traverse(root_node)

    def _extract_func_name(self, func_node, source: str) -> str | None:
        """从函数定义节点中提取函数名（C 和 Rust 的 AST 结构不同）。"""
        if self.lang == "c":
            declarator = func_node.child_by_field_name("declarator")
            if declarator:
                # 穿透 pointer_declarator（如 int *func(...)）
                while declarator and declarator.type == "pointer_declarator":
                    declarator = declarator.child_by_field_name("declarator")
                if declarator and declarator.type == "function_declarator":
                    name_node = declarator.child_by_field_name("declarator")
                    if name_node:
                        return source[name_node.start_byte:name_node.end_byte]
            return None

        elif self.lang == "rust":
            name_node = func_node.child_by_field_name("name")
            if name_node:
                return source[name_node.start_byte:name_node.end_byte]
            return None

    def _extract_calls(self, func_node, source: str) -> list[str]:
        """
        提取函数体内所有被调用的函数名。
        基于文本匹配，不做类型分析：a.method() 提取为 "method"，
        可能和其他同名符号混淆，已在 get_engine_info 中注明限制。
        """
        calls: set[str] = set()

        def find_calls(node):
            if node.type == self._call_node_type:
                func_part = node.child_by_field_name("function")
                if func_part:
                    call_text = source[func_part.start_byte:func_part.end_byte]
                    # a.method() → "method"
                    if "." in call_text:
                        call_text = call_text.rsplit(".", 1)[-1]
                    # module::func() → "func"
                    if "::" in call_text:
                        call_text = call_text.rsplit("::", 1)[-1]
                    # (*func_ptr)() → 跳过
                    if not call_text.startswith("("):
                        calls.add(call_text)

            # Rust 宏调用：println!() vec![] 等
            if self.lang == "rust" and node.type == "macro_invocation":
                macro_node = node.child(0)
                if macro_node:
                    calls.add(source[macro_node.start_byte:macro_node.end_byte])

            for child in node.children:
                find_calls(child)

        find_calls(func_node)
        return list(calls)

    def _index_structs(self, root_node, source: str, file_path: str):
        """提取结构体定义和字段。"""
        def traverse(node):
            if node.type == self._struct_node_type:
                name = self._extract_struct_name(node, source)
                if name:
                    body = source[node.start_byte:node.end_byte]
                    self._struct_index[name] = {
                        "name":   name,
                        "file":   file_path,
                        "line":   node.start_point[0] + 1,
                        "body":   body,
                        "fields": self._parse_struct_fields_from_node(node, source),
                    }
            for child in node.children:
                traverse(child)

        traverse(root_node)

    def _extract_struct_name(self, struct_node, source: str) -> str | None:
        """C 和 Rust 的 struct 节点都用 'name' 字段存名称。"""
        name_node = struct_node.child_by_field_name("name")
        if name_node:
            return source[name_node.start_byte:name_node.end_byte]
        return None

    def _parse_struct_fields_from_node(self, struct_node, source: str) -> list[dict]:
        """直接从 AST 节点提取字段，比正则更准确。"""
        fields = []
        body_node = struct_node.child_by_field_name("body")
        if not body_node:
            return fields

        if self.lang == "c":
            for child in body_node.children:
                if child.type != "field_declaration":
                    continue
                type_node = child.child_by_field_name("type")
                type_str = source[type_node.start_byte:type_node.end_byte].strip() if type_node else "?"
                # 收集该 field_declaration 下所有 field_identifier
                for ident in self._collect_field_identifiers(child, source):
                    fields.append({"name": ident, "type": type_str})

        elif self.lang == "rust":
            for child in body_node.children:
                if child.type != "field_declaration":
                    continue
                name_node = child.child_by_field_name("name")
                type_node = child.child_by_field_name("type")
                if name_node and type_node:
                    fields.append({
                        "name": source[name_node.start_byte:name_node.end_byte],
                        "type": source[type_node.start_byte:type_node.end_byte],
                    })

        return fields

    def _collect_field_identifiers(self, node, source: str) -> list[str]:
        """递归收集节点下所有 field_identifier 的文本。"""
        result = []
        if node.type == "field_identifier":
            result.append(source[node.start_byte:node.end_byte])
        for child in node.children:
            result.extend(self._collect_field_identifiers(child, source))
        return result

    # ── 统一接口实现 ──

    def go_to_definition(self, symbol_name: str) -> dict | None:
        """
        按名称查找定义。
        注意：tree-sitter 无法区分同名符号，存在多个时返回第一个。
        """
        entry = self._func_index.get(symbol_name)
        if entry:
            return {
                "file":       entry["file"],
                "start_line": entry["start_line"],
                "end_line":   entry["end_line"],
                "body":       entry["body"],
            }

        struct = self._struct_index.get(symbol_name)
        if struct:
            return {
                "file":       struct["file"],
                "start_line": struct["line"],
                "end_line":   struct["line"] + struct["body"].count("\n"),
                "body":       struct["body"],
            }

        # 模糊匹配：处理同名去重时生成的 "name@file" key
        for key, entry in self._func_index.items():
            if key.startswith(symbol_name + "@"):
                return {
                    "file":         entry["file"],
                    "start_line":   entry["start_line"],
                    "end_line":     entry["end_line"],
                    "body":         entry["body"],
                    "_fuzzy_match": True,
                }

        return None

    def find_references(self, symbol_name: str) -> list[dict]:
        """
        遍历所有函数的 calls 列表查找引用。
        精度低于 LSP：只能找到函数调用，找不到变量引用和类型引用。
        """
        return [
            {
                "caller":     entry["name"],
                "file":       entry["file"],
                "line":       entry["start_line"],
                "_precision": "name_match_only",
            }
            for entry in self._func_index.values()
            if symbol_name in entry.get("calls", [])
        ]

    def get_call_chain(self, entry_func: str, max_depth: int = 3) -> dict:
        """从入口函数展开调用树（使用预建的 calls 列表，无需实时解析）。"""
        def _expand(func_name: str, depth: int, visited: set) -> dict:
            if depth == 0 or func_name in visited:
                return {}
            visited.add(func_name)
            entry = self._func_index.get(func_name)
            if not entry:
                return {"__not_found__": True}
            return {callee: _expand(callee, depth - 1, visited)
                    for callee in entry.get("calls", [])}

        return {entry_func: _expand(entry_func, max_depth, set())}

    def get_struct_fields(self, struct_name: str) -> dict | None:
        struct = self._struct_index.get(struct_name)
        if not struct:
            return None
        return {
            "name":   struct_name,
            "file":   struct["file"],
            "line":   struct["line"],
            "fields": struct["fields"],
        }

    def get_engine_info(self) -> dict:
        return {
            "engine":       "tree-sitter",
            "path":         "C（降级方案）",
            "precision":    "medium",
            "capabilities": [
                "函数定义提取",
                "调用关系分析（基于函数名文本匹配）",
                "结构体字段提取",
            ],
            "limitations": [
                "无法做类型推断",
                "同名函数无法区分（返回第一个匹配）",
                "方法调用可能与同名自由函数混淆",
                "无法跟踪函数指针和宏展开后的调用",
            ],
        }
