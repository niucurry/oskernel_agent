import os
import re
import json
import queue
import threading
import subprocess

from analysis_engine import AnalysisEngine
from code_parser import find_function_calls


class RustAnalyzerEngine(AnalysisEngine):
    """路径 A：通过 LSP 协议与 rust-analyzer 通信"""

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

    def initialize(self) -> bool:
        """
        启动 rust-analyzer 并完成握手
        如果仓库没有 Cargo.toml，直接返回 False（降级到下一条路径）
        """
        if not os.path.exists(os.path.join(self.repo_path, "Cargo.toml")):
            print("[路径A] 未找到 Cargo.toml，跳过 rust-analyzer")
            return False

        try:
            self._process = subprocess.Popen(
                ["rust-analyzer"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self._reader_thread = threading.Thread(
                target=self._read_responses, daemon=True
            )
            self._reader_thread.start()

            init_result = self._send_request("initialize", {
                "processId": os.getpid(),
                "rootUri": f"file://{self.repo_path}",
                "capabilities": {
                    "textDocument": {
                        "definition": {"dynamicRegistration": False},
                        "references": {"dynamicRegistration": False},
                    },
                    "window": {"workDoneProgress": True},
                },
            })

            if init_result is None:
                return False

            self._send_notification("initialized", {})
            self._wait_for_indexing(timeout=120)

            print("[路径A] rust-analyzer 初始化成功")
            return True

        except FileNotFoundError:
            print("[路径A] rust-analyzer 未安装")
            return False
        except Exception as e:
            print(f"[路径A] 初始化失败：{e}")
            return False

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

                # 区分响应（有 id 且无 method）和通知（有 method）
                if "id" in msg and "method" not in msg:
                    with self._pending_lock:
                        resp_queue = self._pending.get(msg["id"])
                    if resp_queue:
                        resp_queue.put(msg)
                elif msg.get("method") == "$/progress":
                    value = msg.get("params", {}).get("value", {})
                    if value.get("kind") == "end" and "Indexing" in value.get("message", ""):
                        self._indexing_done.set()

            except Exception:
                return

    def _wait_for_indexing(self, timeout: int):
        print(f"[路径A] 等待索引建立（最多 {timeout}s）...")
        finished = self._indexing_done.wait(timeout=timeout)
        if finished:
            print("[路径A] 索引建立完成")
        else:
            print("[路径A] 索引等待超时，继续尝试")

    #统一接口实现

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

    def get_engine_info(self) -> dict:
        return {
            "engine": "rust-analyzer",
            "path": "A",
            "precision": "high",
            "capabilities": [
                "精确跨 crate 跳转定义",
                "完整类型推断（含泛型）",
                "精确引用查找",
            ],
            "limitations": [],
        }

    #辅助方法

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

    def _open_document(self, uri: str, file_path: str):
        """LSP 要求在查询前先通知打开文档。"""
        try:
            with open(file_path) as f:
                text = f.read()
        except Exception:
            return

        self._send_notification("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": "rust", "version": 1, "text": text}
        })

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

    def _find_enclosing_function(self, file_path: str, line: int) -> str | None:
        """从指定行向上搜索最近的函数声明，返回函数名。"""
        try:
            with open(file_path) as f:
                all_lines = f.readlines()
        except Exception:
            return None

        for i in range(line - 1, -1, -1):
            match = re.match(r'\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)', all_lines[i])
            if match:
                return match.group(1)
        return None

    def _extract_callees_from_body(self, body: str) -> list[str]:
        """使用 tree-sitter 从函数体中提取被调用的函数名。"""
        return find_function_calls(body.encode("utf-8"), "rust")

    def _parse_struct_fields(self, body: str) -> list[dict]:
        """从结构体源码中解析字段列表。"""
        start = body.find('{')
        if start == -1:
            return []

        brace_depth, end = 0, start
        for i in range(start, len(body)):
            if body[i] == '{':
                brace_depth += 1
            elif body[i] == '}':
                brace_depth -= 1
                if brace_depth == 0:
                    end = i
                    break

        _SKIP = {"fn", "pub", "let", "use", "impl", "struct", "enum",
                 "type", "const", "static", "where"}
        pattern = re.compile(
            r'^\s*(?:pub(?:\s*\([^)]*\))?\s+)?(\w+)\s*:\s*([^,\n}]+)',
            re.MULTILINE,
        )
        fields = []
        for match in pattern.finditer(body[start + 1: end]):
            name = match.group(1)
            if name in _SKIP:
                continue
            fields.append({"name": name, "type": match.group(2).strip().rstrip(',').strip()})

        return fields

    def shutdown(self):
        if self._process:
            self._send_request("shutdown", {})
            self._send_notification("exit", {})
            self._process.terminate()
            self._process = None
