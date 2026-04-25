import os
import sqlite3
from pathlib import Path

import tree_sitter_c as tsc
import tree_sitter_rust as tsr
from tree_sitter import Language, Parser

# 指向克隆下来的具体代码库路径
TARGET_REPO_DIR = './data/historical_repos/T202510008995695-2259'
REPO_ID = 'T202510008995695-2259' 
DB_PATH = './data/os_knowledge_graph.db'

# 1. 初始化 C 语言和 Rust 语言解析器
c_parser = Parser(Language(tsc.language()))
rust_parser = Parser(Language(tsr.language()))

def init_db():
    """初始化 SQLite 数据库"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id TEXT,
            file_path TEXT,
            symbol_type TEXT,
            symbol_name TEXT,
            code_content TEXT,
            start_line INTEGER
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS CallGraph (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id TEXT,
            caller_name TEXT,
            callee_name TEXT
        )
    ''')
    
    # 为了防止重复运行导致数据叠加，每次运行时先清理当前项目的数据
    cursor.execute("DELETE FROM Symbols WHERE repo_id=?", (REPO_ID,))
    cursor.execute("DELETE FROM CallGraph WHERE repo_id=?", (REPO_ID,))
    
    conn.commit()
    return conn

def find_function_calls(func_code_bytes, lang_type):
    """局部解析，提取内部调用的子函数 (兼容 C 和 Rust)"""
    parser = c_parser if lang_type == 'c' else rust_parser
    tree = parser.parse(func_code_bytes)
    calls = set()
    
    def traverse(node):
        # 普通函数调用 a() 或 a.b() 或 a::b()
        if node.type == 'call_expression':
            func_node = node.child_by_field_name('function')
            if func_node:
                callee = func_code_bytes[func_node.start_byte:func_node.end_byte].decode('utf-8')
                calls.add(callee)
                
        # Rust 特有的宏调用，比如 println!()
        elif lang_type == 'rust' and node.type == 'macro_invocation':
            macro_node = node.child(0) # 获取宏名称节点
            if macro_node:
                macro_name = func_code_bytes[macro_node.start_byte:macro_node.end_byte].decode('utf-8')
                calls.add(macro_name)
                
        for child in node.children:
            traverse(child)
            
    traverse(tree.root_node)
    return list(calls)

def get_nodes_of_types(node, target_types):
    """递归获取所有指定类型的节点，解决 Rust 中函数包裹在 impl 块内的问题"""
    result = []
    if node.type in target_types:
        result.append(node)
    for child in node.children:
        result.extend(get_nodes_of_types(child, target_types))
    return result

def parse_file_and_store(file_path, conn):
    """解析单个 C/Rust 文件并将数据入库"""
    cursor = conn.cursor()
    rel_path = os.path.relpath(file_path, TARGET_REPO_DIR)
    ext = os.path.splitext(file_path)[1]
    
    # 动态匹配语言解析策略
    if ext == '.c':
        parser, lang_type = c_parser, 'c'
        struct_type, func_type = 'struct_specifier', 'function_definition'
    elif ext == '.rs':
        parser, lang_type = rust_parser, 'rust'
        struct_type, func_type = 'struct_item', 'function_item'
    else:
        return

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            source_code = f.read()
        source_bytes = source_code.encode('utf-8')
    except Exception as e:
        print(f" 跳过无法读取的文件 {rel_path}: {e}")
        return

    tree = parser.parse(source_bytes)
    
    # 递归提取所有目标节点
    target_nodes = get_nodes_of_types(tree.root_node, [struct_type, func_type])

    for child in target_nodes:
        # 1. 处理结构体定义
        if child.type == struct_type:
            struct_name_node = child.child_by_field_name('name')
            if struct_name_node:
                struct_name = source_bytes[struct_name_node.start_byte:struct_name_node.end_byte].decode('utf-8')
                struct_body = source_bytes[child.start_byte:child.end_byte].decode('utf-8')
                
                cursor.execute('''
                    INSERT INTO Symbols (repo_id, file_path, symbol_type, symbol_name, code_content, start_line)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (REPO_ID, rel_path, 'struct', struct_name, struct_body, child.start_point[0]))

        # 2. 处理函数定义
        elif child.type == func_type:
            func_name = None
            
            if lang_type == 'c':
                declarator = child.child_by_field_name('declarator')
                if declarator:
                    while declarator.type != 'identifier' and declarator.child_by_field_name('declarator'):
                        declarator = declarator.child_by_field_name('declarator')
                    if declarator.type == 'identifier':
                        func_name = source_bytes[declarator.start_byte:declarator.end_byte].decode('utf-8')
                        
            elif lang_type == 'rust':
                name_node = child.child_by_field_name('name')
                if name_node:
                    func_name = source_bytes[name_node.start_byte:name_node.end_byte].decode('utf-8')
            
            if func_name:
                func_body_bytes = source_bytes[child.start_byte:child.end_byte]
                func_body_str = func_body_bytes.decode('utf-8')
                
                cursor.execute('''
                    INSERT INTO Symbols (repo_id, file_path, symbol_type, symbol_name, code_content, start_line)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (REPO_ID, rel_path, 'function', func_name, func_body_str, child.start_point[0]))
                
                # 3. 提取调用图
                callees = find_function_calls(func_body_bytes, lang_type)
                for callee in callees:
                    cursor.execute('''
                        INSERT INTO CallGraph (repo_id, caller_name, callee_name)
                        VALUES (?, ?, ?)
                    ''', (REPO_ID, func_name, callee))
                    
    conn.commit()

def deduplicate_parent_paths(paths: list[str]) -> list[str]:
    """如果 A 是 B 的父路径，去掉 A，保留更精确的 B"""
    paths = sorted(paths, key=len, reverse=True)
    result = []
    for p in paths:
        if not any(kept.startswith(p + "/") for kept in result):
            result.append(p)
    return result


def find_source_roots(repo_path: Path) -> list[str]:
    """找到仓库中源代码文件最密集的目录"""
    SKIP_DIRS = {".git", "target", "build", "node_modules", "__pycache__", ".cargo", "vendor"}
    SRC_EXTS = {".c", ".rs", ".h", ".S", ".asm"}
    KNOWN_NAMES = {"src", "kernel", "kern", "os", "core", "code"}

    candidates = []
    for entry in repo_path.rglob("*"):
        if not entry.is_dir():
            continue
        if any(skip in entry.parts for skip in SKIP_DIRS):
            continue

        src_count = sum(
            1 for f in entry.iterdir()
            if f.is_file() and f.suffix in SRC_EXTS
        )
        if src_count > 0:
            rel = str(entry.relative_to(repo_path))
            candidates.append((rel, src_count, entry.name.lower()))

    candidates.sort(key=lambda x: (x[2] not in KNOWN_NAMES, -x[1]))

    return deduplicate_parent_paths([c[0] for c in candidates[:8]])


def build_knowledge_graph(repo_path: str | None = None):
    target = repo_path or TARGET_REPO_DIR
    source_roots = find_source_roots(Path(target))

    if source_roots:
        rt = Path(target)
        scan_dirs = [str(rt / r) for r in source_roots]
        print(f"  源码根目录检测结果: {source_roots}")
    else:
        scan_dirs = [target]

    print(f" 开始构建代码知识图谱 (支持 C/Rust)，目标仓库: {REPO_ID}")
    conn = init_db()

    processed_count = 0
    for scan_dir in scan_dirs:
        for root, dirs, files in os.walk(scan_dir):
            for file in files:
                if file.endswith(('.c', '.rs')):
                    file_path = os.path.join(root, file)
                    print(f"正在解析: {os.path.relpath(file_path, target)}...")
                    parse_file_and_store(file_path, conn)
                    processed_count += 1

    conn.close()
    print(f"\n 解析完成！共处理了 {processed_count} 个 C/Rust 源码文件。")
    print(f" 图谱数据已保存至 SQLite 数据库: {DB_PATH}")

if __name__ == "__main__":
    build_knowledge_graph()