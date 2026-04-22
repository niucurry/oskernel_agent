import sqlite3
import json

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
            # 【关键修改】：找不到时给出明确的文字警告，阻止它乱猜
            return f"警告：在数据库中未找到名为 '{function_name}' 的函数，可能是函数名错误，或该文件未被解析。"
        return [row[0] for row in results]

    def search_keyword_in_docs(self, repo_id: str, keyword: str) -> str:
        """在文档中检索关键字（占位：后续可接入向量数据库）"""
        return f"假设在此处返回了 {repo_id} 中关于 {keyword} 的文档描述。"