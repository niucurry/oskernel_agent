import os
import re
import subprocess
import threading

from lsp_engine import LspEngine
from code_parser import find_function_calls


class RustAnalyzerEngine(LspEngine):
    """路径 A：通过 LSP 协议与 rust-analyzer 通信"""

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

    def _open_document(self, uri: str, file_path: str):
        try:
            with open(file_path) as f:
                text = f.read()
        except Exception:
            return
        self._send_notification("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": "rust", "version": 1, "text": text}
        })

    def _find_enclosing_function(self, file_path: str, line: int) -> str | None:
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
        return find_function_calls(body.encode("utf-8"), "rust")

    def _parse_struct_fields(self, body: str) -> list[dict]:
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
