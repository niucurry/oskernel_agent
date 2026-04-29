import os
import re
import subprocess
import threading

from engines.lsp_base import LspEngine
from parser.code_parser import find_function_calls

# rust-analyzer 用来标记索引结束的 token 关键词
_RA_INDEXING_TOKENS = {"Indexing", "Roots Scanned", "Loading"}


class RustAnalyzerEngine(LspEngine):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # token_id → title 的映射，用于把数字 token 关联到它的 begin 标题
        self._progress_titles: dict = {}

    def _check_indexing_complete(self, msg: dict) -> bool:
        """
        rust-analyzer 进度消息结构：
          begin:  {"token": "rustAnalyzer/Indexing", "value": {"kind": "begin", "title": "Indexing"}}
          report: {"token": ..., "value": {"kind": "report", "message": "100/200"}}
          end:    {"token": ..., "value": {"kind": "end"}}          ← message 字段不存在！

        父类实现检查 value.message 里有无 "Indexing"，但 end 消息没有 message，永远不触发。
        正确做法：检查 token 字符串或 begin 时记录的 title。
        """
        params = msg.get("params", {})
        token  = params.get("token")
        value  = params.get("value", {})
        kind   = value.get("kind", "")

        # begin 阶段：记录 token → title 映射
        if kind == "begin":
            title = value.get("title", "")
            if token is not None:
                self._progress_titles[token] = title

        if kind != "end":
            return False

        # 用 token 字符串直接判断
        if isinstance(token, str):
            return any(kw in token for kw in _RA_INDEXING_TOKENS)

        # 数字 token：查找 begin 时记录的 title
        title = self._progress_titles.get(token, "")
        return any(kw in title for kw in _RA_INDEXING_TOKENS)

    """路径 A：通过 LSP 协议与 rust-analyzer 通信"""

    def _find_cargo_root(self) -> str | None:
        """
        递归找含 Cargo.toml 的目录，优先选内核相关目录（os/kernel/kern），
        其次选层级最浅的，跳过 vendor/target/.git。
        """
        _SKIP    = {"vendor", "target", ".git", "node_modules"}
        _PREFER  = {"os", "kernel", "kern", "core"}
        base_depth = self.repo_path.rstrip(os.sep).count(os.sep)

        candidates: list[tuple[int, int, str]] = []  # (depth, not_preferred, path)
        for root, dirs, files in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if d not in _SKIP]
            if "Cargo.toml" in files:
                depth = root.count(os.sep) - base_depth
                preferred = os.path.basename(root).lower() in _PREFER
                candidates.append((depth, 0 if preferred else 1, root))

        if not candidates:
            return None
        candidates.sort()          # 先按深度升序，同深度内核目录排前面
        return candidates[0][2]

    def initialize(self) -> bool:
        """
        启动 rust-analyzer 并完成握手
        如果仓库没有 Cargo.toml，直接返回 False（降级到下一条路径）
        """
        cargo_root = self._find_cargo_root()
        if cargo_root is None:
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
                "rootUri": f"file://{cargo_root}",
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
            self._wait_for_indexing(timeout=self._timeout)
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
