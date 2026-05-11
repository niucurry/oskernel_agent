"""
OSKernelMCPTools：MCP 工具集的统一入口。

继承 ToolDispatcher（T2–T6 的实现），并补充：
  - read_file()  T1 的实例方法封装
  - execute()    统一路由：工具名 → 对应方法

设计原则：
  1. 工具是 LLM 与代码之间的唯一通道（不直接接触源文件）
  2. 返回格式面向"引用"设计（带文件名 + 行号，LLM 可直接引用）
  3. 工具内部主动限制返回量（截断 + 摘要）
"""

from tools.tool_dispatcher import ToolDispatcher
from tools.tool_handlers import read_file as _read_file
from tools.tool_handlers import search_code as _search_code
from tools.reference_db import ReferenceOSDatabase


class OSKernelMCPTools(ToolDispatcher):
    """
    MCP 工具集的统一入口。
    管理全部 6 个工具的注册和调用分发。
    """

    def __init__(
        self,
        repo_path: str,
        engine,
        level2_index,
        profile: dict,
        structure: dict | None,
        ref_database: ReferenceOSDatabase,
    ):
        # ToolDispatcher.__init__(engine, level2_index, repo_path, profile, structure, ref_db_dir)
        super().__init__(
            engine, level2_index, repo_path, profile, structure or {}
        )
        # 覆盖父类创建的默认 ReferenceOSDatabase，使用调用方传入的实例
        self.ref_database = ref_database

        # 确定内核 crate 目录，供 _disambiguate 加分（父类检查 getattr 时生效）
        self._kernel_crate_dirs: list[str] = [
            c["rel_dir"]
            for c in profile.get("crates", [])
            if c.get("role") in ("kernel", "kernel_lib")
        ]

    #T1

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        return _read_file(self.repo_path, path, start_line, end_line)

    def search_code(
        self,
        pattern: str,
        file_glob: str | None = None,
        case_sensitive: bool = False,
        max_results: int = 50,
    ) -> str:
        return _search_code(self.repo_path, pattern, file_glob, case_sensitive, max_results)

    #统一路由

    def execute(self, tool_name: str, arguments: dict) -> str:
        """将 LLM 请求的工具名分发到对应方法，统一处理参数和异常。"""
        dispatch: dict = {
            "read_file":                 self.read_file,
            "search_code":               self.search_code,
            "find_symbol_definition":    self.find_symbol_definition,
            "find_symbol_references":    self.find_symbol_references,
            "list_implemented_syscalls": self.list_implemented_syscalls,
            "get_subsystem_call_chain":  self.get_subsystem_call_chain,
            "compare_with_reference_os": self.compare_with_reference_os,
        }

        func = dispatch.get(tool_name)
        if func is None:
            return f"[错误] 未知工具：{tool_name}"

        try:
            result = func(**arguments)
            # 非 str 结果（引擎原生 dict/list/None）统一转换
            if result is None:
                return f"[未找到] {tool_name} 未返回结果"
            return result if isinstance(result, str) else str(result)
        except Exception as exc:
            return f"[工具执行错误] {tool_name}: {exc}\n请检查参数是否正确。"
