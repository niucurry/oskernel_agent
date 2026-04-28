from abc import ABC, abstractmethod


class AnalysisEngine(ABC):
    """
    三条路径的统一抽象接口
    MCP 工具只调用这个接口，不关心底层实现
    """

    @abstractmethod
    def go_to_definition(self, symbol_name: str) -> dict | None:
        """
        查找符号的定义位置，返回完整源码
        返回：{
          "file": "os/src/task/task.rs",
          "start_line": 18,
          "end_line": 45,
          "body": "pub struct TaskControlBlock { ... }"
        }
        """
        ...

    @abstractmethod
    def find_references(self, symbol_name: str) -> list[dict]:
        """
        查找所有引用了该符号的位置
        返回：[
          {"caller": "sys_fork", "file": "syscall/process.rs", "line": 28},
          {"caller": "run_tasks", "file": "task/mod.rs", "line": 62},
        ]
        """
        ...

    @abstractmethod
    def get_call_chain(self, entry_func: str, max_depth: int = 3) -> dict:
        """
        从入口函数展开调用树
        返回嵌套字典：{
          "do_fork": {
            "copy_mm": {"alloc_page": {}, "copy_page_table": {}},
            "alloc_pid": {},
            "wake_up_new_task": {"add_task": {}}
          }
        }
        """
        ...

    @abstractmethod
    def get_struct_fields(self, struct_name: str) -> dict | None:
        """
        获取结构体的字段列表
        返回：{
          "name": "TaskControlBlock",
          "file": "task/task.rs",
          "line": 18,
          "fields": [
            {"name": "pid", "type": "PidHandle"},
            {"name": "inner", "type": "UPSafeCell<TaskControlBlockInner>"},
          ]
        }
        """
        ...

    @abstractmethod
    def get_engine_info(self) -> dict:
        """返回引擎元信息，注入 Prompt 让 LLM 知道当前精度"""
        ...
