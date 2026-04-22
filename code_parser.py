import os
import sqlite3
import tree_sitter_c as tsc
from tree_sitter import Language, Parser

# 指向克隆下来的具体代码库路径
TARGET_REPO_DIR = './data/historical_repos/T202510008995695-2259'
REPO_ID = 'T202510008995695-2259' 
DB_PATH = './data/os_knowledge_graph.db'

# 初始化 C 语言解析器
C_LANGUAGE = Language(tsc.language())
parser = Parser(C_LANGUAGE)  # 直接将语言对象作为参数传入

def init_db():
    """初始化 SQLite 数据库和两张核心表"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 符号表：存储函数和结构体的具体代码
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id TEXT,
            file_path TEXT,
            symbol_type TEXT, -- 'function' 或 'struct'
            symbol_name TEXT,
            code_content TEXT,
            start_line INTEGER
        )
    ''')
    
    # 调用图表：本质上是一个有向图的邻接表关系
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS CallGraph (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id TEXT,
            caller_name TEXT,
            callee_name TEXT
        )
    ''')
    conn.commit()
    return conn

def find_function_calls(func_code_bytes):
    """局部解析函数体，提取所有内部调用的子函数名"""
    tree = parser.parse(func_code_bytes)
    calls = set() # 使用集合自动去重
    
    def traverse(node):
        if node.type == 'call_expression':
            func_node = node.child_by_field_name('function')
            if func_node and func_node.type == 'identifier':
                # 注意：tree-sitter 的字节索引必须配合 bytes 对象切片
                callee = func_code_bytes[func_node.start_byte:func_node.end_byte].decode('utf-8')
                calls.add(callee)
        for child in node.children:
            traverse(child)
            
    traverse(tree.root_node)
    return list(calls)

def parse_file_and_store(file_path, conn):
    """解析单个 C 文件并将数据入库"""
    cursor = conn.cursor()
    # 计算相对路径，方便后续展示
    rel_path = os.path.relpath(file_path, TARGET_REPO_DIR)
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            source_code = f.read()
        source_bytes = source_code.encode('utf-8')
    except Exception as e:
        print(f"⚠️ 跳过无法读取的文件 {rel_path}: {e}")
        return

    tree = parser.parse(source_bytes)
    root_node = tree.root_node

    for child in root_node.children:
        # 1. 处理结构体定义 (OS 核心数据结构，如 PCB, trapframe)
        if child.type == 'struct_specifier':
            struct_name_node = child.child_by_field_name('name')
            if struct_name_node:
                struct_name = source_bytes[struct_name_node.start_byte:struct_name_node.end_byte].decode('utf-8')
                struct_body = source_bytes[child.start_byte:child.end_byte].decode('utf-8')
                
                cursor.execute('''
                    INSERT INTO Symbols (repo_id, file_path, symbol_type, symbol_name, code_content, start_line)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (REPO_ID, rel_path, 'struct', struct_name, struct_body, child.start_point[0]))

        # 2. 处理函数定义
        elif child.type == 'function_definition':
            declarator = child.child_by_field_name('declarator')
            if declarator:
                # 拨开指针和复杂声明的外壳，找到核心标识符
                while declarator.type != 'identifier' and declarator.child_by_field_name('declarator'):
                    declarator = declarator.child_by_field_name('declarator')
                
                if declarator.type == 'identifier':
                    func_name = source_bytes[declarator.start_byte:declarator.end_byte].decode('utf-8')
                    func_body_bytes = source_bytes[child.start_byte:child.end_byte]
                    func_body_str = func_body_bytes.decode('utf-8')
                    
                    # 存入符号表
                    cursor.execute('''
                        INSERT INTO Symbols (repo_id, file_path, symbol_type, symbol_name, code_content, start_line)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (REPO_ID, rel_path, 'function', func_name, func_body_str, child.start_point[0]))
                    
                    # 3. 提取调用图 (Call Graph) 并存入关系表
                    callees = find_function_calls(func_body_bytes)
                    for callee in callees:
                        cursor.execute('''
                            INSERT INTO CallGraph (repo_id, caller_name, callee_name)
                            VALUES (?, ?, ?)
                        ''', (REPO_ID, func_name, callee))
                        
    conn.commit()

def build_knowledge_graph():
    print(f"🚀 开始构建代码知识图谱，目标仓库: {REPO_ID}")
    conn = init_db()
    
    # 遍历仓库目录下的所有 .c 文件
    processed_count = 0
    for root, dirs, files in os.walk(TARGET_REPO_DIR):
        for file in files:
            if file.endswith('.c'):
                file_path = os.path.join(root, file)
                print(f"正在解析: {os.path.relpath(file_path, TARGET_REPO_DIR)}...")
                parse_file_and_store(file_path, conn)
                processed_count += 1
                
    conn.close()
    print(f"\n✅ 解析完成！共处理了 {processed_count} 个 C 源码文件。")
    print(f"📊 图谱数据已保存至 SQLite 数据库: {DB_PATH}")

if __name__ == "__main__":
    # 运行前请确保 TARGET_REPO_DIR 指向的目录存在且包含 .c 代码
    build_knowledge_graph()