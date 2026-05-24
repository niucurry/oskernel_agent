"""
Level2Index：第二级符号索引，基于 SQLite 持久化（SymbolDB）。

提供与历史内存版完全兼容的接口（lookup_symbol / list_file_symbols /
search_symbols / get_subsystem_detail / _by_name / _by_file / _by_subsystem），
使 tool_dispatcher 不需要改动即可切换到持久化后端。

build_repo_map 会先按"仓库路径 + 源文件 mtime/size 指纹"查缓存数据库，
命中时跳过 ctags 全量扫描，未命中才重新构建。
"""

from collections import defaultdict
from pathlib import Path

from parser.code_parser import classify_symbol, run_ctags, generate_level1_map
from parser.symbol_db import (
    SymbolDB,
    fingerprint_files,
    iter_source_files,
    repo_cache_path,
)

_FINGERPRINT_KEY = "source_fingerprint"
_LEVEL1_MAP_KEY  = "level1_map"


class Level2Index:
    """SQLite 持久化版的第二级索引。

    数据通过 SymbolDB 落到 data/cache/<repo_hash>.db；本类只做格式适配与
    分组渲染。_by_name / _by_file / _by_subsystem 由 SymbolDB 提供的
    SQL-backed Mapping view 实现，与原内存版 dict 行为等价。
    """

    def __init__(self, db: SymbolDB):
        self._db = db

    # 持久化 view（兼容旧代码 self.level2_index._by_xxx[k] 用法）

    @property
    def _by_name(self):
        return self._db._by_name

    @property
    def _by_file(self):
        return self._db._by_file

    @property
    def _by_subsystem(self):
        return self._db._by_subsystem

    # 查询接口

    def lookup_symbol(self, name: str) -> list[dict]:
        """按名称查找符号（可能有多个同名符号在不同文件）。"""
        return self._db.lookup_symbol(name)

    def list_file_symbols(self, file_path: str) -> list[dict]:
        """列出某个文件的所有符号，按行号排序。"""
        return self._db.list_file_symbols(file_path)

    def search_symbols(self, pattern: str) -> list[dict]:
        """模糊搜索符号名，最多返回 20 个。"""
        return self._db.search_symbols(pattern, limit=20)

    def get_subsystem_detail(self, subsystem: str) -> str:
        """返回某子系统的完整符号列表（按文件分组）。"""
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

    # SymbolDB 访问器（供 mcp_server 的 index_status / tool_dispatcher 重置时使用）

    @property
    def db(self) -> SymbolDB:
        return self._db


def build_repo_map(
    repo_path: str,
    structure: dict,
    profile: dict,
    cache_dir: str = "data/cache",
) -> tuple[str, "Level2Index"]:
    """构建第一级地图 + 第二级 SQLite 索引。

    缓存策略：
      仓库路径 → 缓存数据库（data/cache/<sha1>.db）
      源文件 mtime+size 指纹 → 命中则跳过 ctags 与 FTS 全量入库
    """
    source_files = iter_source_files(repo_path)
    fingerprint = fingerprint_files(source_files)
    db_path = repo_cache_path(repo_path, cache_dir)
    db = SymbolDB(db_path)

    cached_fp = db.get_meta(_FINGERPRINT_KEY)
    cached_l1 = db.get_meta(_LEVEL1_MAP_KEY)
    cache_hit = (
        cached_fp == fingerprint
        and cached_l1 is not None
        and db.stats()["symbols"] > 0
    )

    if cache_hit:
        # 完全命中：Layer1 地图、符号表、FTS 全部复用，跳过 ctags 子进程
        level1_map = cached_l1
        stats = db.stats()
        print(f"  [缓存命中] 复用 {db_path.name}：{stats['symbols']} 符号，"
              f"{stats['fts_rows']} FTS 行（上次索引于 {stats['indexed_at']}）")
    else:
        raw_tags = run_ctags(repo_path, structure["source_roots"])
        level1_map = generate_level1_map(raw_tags, structure, profile)

        file_to_subsystem: dict[str, str] = {}
        for subsystem, files in structure["subsystem_locations"].items():
            for entry in files:
                file_to_subsystem[entry["file"]] = subsystem

        result = db.populate(
            raw_tags,
            profile["primary_lang"],
            file_to_subsystem,
            source_files,
            repo_path,
            classify_symbol,
        )
        db.set_meta(_FINGERPRINT_KEY, fingerprint)
        db.set_meta(_LEVEL1_MAP_KEY, level1_map)
        print(f"  符号统计：总计 {result['total']}，"
              f"第一级 {result['level1']}，第二级 {result['level2']}，"
              f"丢弃 {result['discarded']}")
        print(f"  全文索引：{result['fts_rows']} 行写入 {db_path.name}")

        if result["total"] == 0:
            print("\n  !! 严重警告：符号索引为空 !!")
            print("  所有工具调用（get_call_chain / get_struct_fields / find_references / go_to_definition）")
            print('  将返回"未找到"，Agent 报告结论不可信。请检查：')
            print("  1. 是否安装了 Universal Ctags（非 Exuberant Ctags）")
            print("  2. source_roots 是否包含实际的源代码目录")

    print(f"  第一级地图预估 Token：{len(level1_map) // 3}")
    return level1_map, Level2Index(db)
