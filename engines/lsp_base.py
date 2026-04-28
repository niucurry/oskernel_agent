import os
import json
import queue
import threading
import subprocess
from abc import abstractmethod

from engines.base import AnalysisEngine


class LspEngine(AnalysisEngine):
    """
    LSP 通信基础设施，供路径 A（rust-analyzer）和路径 B（clangd）复用。
    子类只需实现语言相关的部分，不需要关心 JSON-RPC 通信细节。
    """

    def __init__(self, repo_path: str, level2_index=None):
        self.repo_path = os.path.abspath(repo_path)
        self._level2_index = level2_index
        self._request_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._indexing_done = threading.Event()
        self._process: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None

    #子类必须实现

    @abstractmethod
    def initialize(self) -> bool:
        """启动 LSP 服务器并完成握手"""
        ...

    @abstractmethod
    def get_engine_info(self) -> dict:
        ...

    @abstractmethod
    def _open_document(self, uri: str, file_path: str):
        """通知 LSP 服务器打开文档（需指定正确的 languageId）"""
        ...

    @abstractmethod
    def _find_enclosing_function(self, file_path: str, line: int) -> str | None:
        """找出某行所在的函数名（不同语言语法不同）"""
        ...

    @abstractmethod
    def _extract_callees_from_body(self, body: str) -> list[str]:
        """从函数体提取被调用的函数名"""
        ...

    @abstractmethod
    def _parse_struct_fields(self, body: str) -> list[dict]:
        """从结构体定义解析字段列表"""
        ...

    #LSP 通信底层

    def _send_request(self, method: str, params: dict, timeout: int = 30) -> dict | None:
        with self._pending_lock:
            self._request_id += 1
            req_id = self._request_id
            resp_queue: queue.Queue = queue.Queue()
            self._pending[req_id] = resp_queue

        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        self._write_message(msg)

        try:
            response = resp_queue.get(timeout=timeout)
            return response.get("result")
        except queue.Empty:
            return None
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

    def _send_notification(self, method: str, params: dict):
        self._write_message({"jsonrpc": "2.0", "method": method, "params": params})

    def _write_message(self, msg: dict):
        body = json.dumps(msg).encode()
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        with self._write_lock:
            self._process.stdin.write(header + body)
            self._process.stdin.flush()

    def _read_responses(self):
        while self._process and self._process.poll() is None:
            try:
                header = b""
                while not header.endswith(b"\r\n\r\n"):
                    byte = self._process.stdout.read(1)
                    if not byte:
                        return
                    header += byte

                length = int(header.decode().split("Content-Length: ")[1].split("\r\n")[0])
                body = self._process.stdout.read(length)
                msg = json.loads(body)

                if "id" in msg and "method" not in msg:
                    with self._pending_lock:
                        resp_queue = self._pending.get(msg["id"])
                    if resp_queue:
                        resp_queue.put(msg)
                elif msg.get("method") == "$/progress":
                    if self._check_indexing_complete(msg):
                        self._indexing_done.set()

            except Exception:
                return

    def _check_indexing_complete(self, msg: dict) -> bool:
        """判断 $/progress 是否代表索引完成，子类可按需重写。"""
        value = msg.get("params", {}).get("value", {})
        return value.get("kind") == "end" and "Indexing" in value.get("message", "")

    def _wait_for_indexing(self, timeout: int):
        label = self.__class__.__name__
        print(f"[{label}] 等待索引建立（最多 {timeout}s）...")
        if self._indexing_done.wait(timeout=timeout):
            print(f"[{label}] 索引建立完成")
        else:
            print(f"[{label}] 索引等待超时，继续尝试")

    #统一接口实现（所有 LSP 引擎共享）

    def go_to_definition(self, symbol_name: str) -> dict | None:
        locations = self._find_symbol_locations(symbol_name)
        if not locations:
            return None

        loc = locations[0]
        file_uri = f"file://{loc['file']}"
        self._open_document(file_uri, loc["file"])

        result = self._send_request("textDocument/definition", {
            "textDocument": {"uri": file_uri},
            "position": {"line": loc["line"] - 1, "character": loc["character"]},
        })
        if not result:
            return None

        target = result[0] if isinstance(result, list) else result
        target_file = target["uri"].replace("file://", "")
        target_line = target["range"]["start"]["line"] + 1

        body = self._extract_code_block(target_file, target_line)
        return {
            "file": os.path.relpath(target_file, self.repo_path),
            "start_line": target_line,
            "end_line": target_line + body.count("\n"),
            "body": body,
        }

    def find_references(self, symbol_name: str) -> list[dict]:
        locations = self._find_symbol_locations(symbol_name)
        if not locations:
            return []

        loc = locations[0]
        file_uri = f"file://{loc['file']}"
        self._open_document(file_uri, loc["file"])

        result = self._send_request("textDocument/references", {
            "textDocument": {"uri": file_uri},
            "position": {"line": loc["line"] - 1, "character": loc["character"]},
            "context": {"includeDeclaration": False},
        })
        if not result:
            return []

        refs = []
        for ref_loc in result:
            ref_file = ref_loc["uri"].replace("file://", "")
            ref_line = ref_loc["range"]["start"]["line"] + 1
            caller = self._find_enclosing_function(ref_file, ref_line)
            refs.append({
                "caller": caller or "[顶层作用域]",
                "file": os.path.relpath(ref_file, self.repo_path),
                "line": ref_line,
            })
        return refs

    def get_call_chain(self, entry_func: str, max_depth: int = 3) -> dict:
        def _expand(func_name: str, depth: int, visited: set) -> dict:
            if depth == 0 or func_name in visited:
                return {}
            visited.add(func_name)
            defn = self.go_to_definition(func_name)
            if not defn:
                return {"__not_found__": True}
            callees = self._extract_callees_from_body(defn["body"])
            return {callee: _expand(callee, depth - 1, visited) for callee in callees}

        return {entry_func: _expand(entry_func, max_depth, set())}

    def get_struct_fields(self, struct_name: str) -> dict | None:
        defn = self.go_to_definition(struct_name)
        if not defn:
            return None
        return {
            "name": struct_name,
            "file": defn["file"],
            "line": defn["start_line"],
            "fields": self._parse_struct_fields(defn["body"]),
        }

    #共享辅助方法

    def _find_symbol_locations(self, symbol_name: str) -> list[dict]:
        """通过 level2_index 查找符号的文件/行号，再定位列位置供 LSP 使用。"""
        if self._level2_index is None:
            return []

        entries = self._level2_index.lookup_symbol(symbol_name)
        if not entries:
            return []

        locations = []
        for entry in entries:
            file_path = os.path.join(self.repo_path, entry["file"])
            line = entry["line"]
            character = 0
            try:
                with open(file_path) as f:
                    all_lines = f.readlines()
                if 0 < line <= len(all_lines):
                    idx = all_lines[line - 1].find(symbol_name)
                    if idx >= 0:
                        character = idx
            except Exception:
                pass
            locations.append({"file": file_path, "line": line, "character": character})

        return locations

    def _extract_code_block(self, file_path: str, start_line: int) -> str:
        """从起始行开始，按大括号配对提取完整代码块。"""
        try:
            with open(file_path) as f:
                lines = f.readlines()
        except Exception:
            return ""

        brace_depth = 0
        found_open = False
        end_line = start_line

        for i in range(start_line - 1, min(start_line + 300, len(lines))):
            for ch in lines[i]:
                if ch == '{':
                    brace_depth += 1
                    found_open = True
                elif ch == '}':
                    brace_depth -= 1
            if found_open and brace_depth == 0:
                end_line = i + 1
                break

        return "".join(lines[start_line - 1: end_line])

    def shutdown(self):
        if self._process:
            self._send_request("shutdown", {})
            self._send_notification("exit", {})
            self._process.terminate()
            self._process = None
