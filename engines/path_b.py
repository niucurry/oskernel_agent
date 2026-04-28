import os
import re
import json
import shutil
import subprocess
import threading
from pathlib import Path

from engines.lsp_base import LspEngine
from parser.code_parser import find_function_calls


# ── compile_commands.json 生成策略 ──

def try_generate_compile_commands(repo_path: str) -> str | None:
    """四种策略逐一尝试，任何一种成功就返回路径。"""
    output = os.path.join(repo_path, "compile_commands.json")

    strategies = [
        ("bear + make -n",    _strategy_bear_dry_run),
        ("compiledb",         _strategy_compiledb),
        ("cmake",             _strategy_cmake),
        ("手工生成最小化版本", _strategy_manual_minimal),
    ]

    for name, func in strategies:
        print(f"  [路径B] 尝试策略：{name}")
        try:
            if func(repo_path, output):
                with open(output) as f:
                    entries = json.load(f)
                if len(entries) > 0:
                    print(f"  [路径B] ✅ 成功，{len(entries)} 个编译条目")
                    return output
        except Exception as e:
            print(f"  [路径B] ❌ 策略 {name} 失败：{e}")
            continue

    return None


def _strategy_bear_dry_run(repo_path: str, output: str) -> bool:
    """
    策略1：bear + make -n
    -n 让 make 只打印要执行的命令，不实际编译
    bear 从打印的命令中提取编译参数
    """
    subprocess.run(
        ["bear", "--output", output, "--", "make", "-n"],
        cwd=repo_path, capture_output=True, timeout=60,
    )
    return os.path.exists(output) and os.path.getsize(output) > 10


def _strategy_compiledb(repo_path: str, output: str) -> bool:
    """
    策略2：compiledb 工具
    专门设计来从 Makefile 生成 compile_commands.json
    对交叉编译支持比 bear 更好
    """
    subprocess.run(
        ["compiledb", "-n", "make"],
        cwd=repo_path, capture_output=True, timeout=60,
    )
    return os.path.exists(output) and os.path.getsize(output) > 10


def _strategy_cmake(repo_path: str, output: str) -> bool:
    """
    策略3：cmake 项目直接生成
    少数 OS 项目使用 cmake 构建
    """
    if not os.path.exists(os.path.join(repo_path, "CMakeLists.txt")):
        return False

    build_dir = os.path.join(repo_path, "_build_temp")
    os.makedirs(build_dir, exist_ok=True)

    subprocess.run(
        ["cmake", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", repo_path],
        cwd=build_dir, capture_output=True, timeout=60,
    )

    generated = os.path.join(build_dir, "compile_commands.json")
    if os.path.exists(generated):
        shutil.copy(generated, output)
        return True
    return False


def _strategy_manual_minimal(repo_path: str, output: str) -> bool:
    """
    策略4（兜底）：手工扫描所有 .c 文件和 .h 目录
    生成最小化的 compile_commands.json

    精度低于真正的编译数据库，但至少能让 clangd
    找到头文件，实现跳转定义
    """
    c_files = list(Path(repo_path).rglob("*.c"))
    if not c_files:
        return False

    include_dirs = sorted(set(str(h.parent) for h in Path(repo_path).rglob("*.h")))
    include_flags = " ".join(f"-I{d}" for d in include_dirs)
    extra_flags = _extract_flags_from_makefile(repo_path)

    commands = [
        {
            "directory": repo_path,
            "command": f"gcc {include_flags} {extra_flags} -c {c_file}",
            "file": str(c_file),
        }
        for c_file in c_files
    ]

    with open(output, "w") as f:
        json.dump(commands, f, indent=2)
    return True


def _extract_flags_from_makefile(repo_path: str) -> str:
    """从 Makefile 中提取关键编译参数（-D 宏和 -I 路径）。"""
    makefile = os.path.join(repo_path, "Makefile")
    if not os.path.exists(makefile):
        return ""

    flags = []
    try:
        content = Path(makefile).read_text(errors="replace")
        for m in re.finditer(r'-D\w+(?:=\S+)?', content):
            flags.append(m.group())
        for m in re.finditer(r'-I\s*(\S+)', content):
            flags.append(f"-I{m.group(1)}")
    except Exception:
        pass

    return " ".join(flags)


# ── ClangdEngine ──

class ClangdEngine(LspEngine):
    """路径 B：通过 LSP 协议与 clangd 通信（C 仓库）"""

    def __init__(self, repo_path: str, compile_commands_path: str, level2_index=None):
        super().__init__(repo_path, level2_index)
        self.compile_commands_path = compile_commands_path

    def initialize(self) -> bool:
        try:
            self._process = subprocess.Popen(
                [
                    "clangd",
                    f"--compile-commands-dir={os.path.dirname(self.compile_commands_path)}",
                    "--header-insertion=never",
                    "--clang-tidy=false",
                ],
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
            self._wait_for_indexing(timeout=60)
            print("[路径B] clangd 初始化成功")
            return True

        except FileNotFoundError:
            print("[路径B] clangd 未安装")
            return False
        except Exception as e:
            print(f"[路径B] 初始化失败：{e}")
            return False

    def _check_indexing_complete(self, msg: dict) -> bool:
        """clangd 用 token='backgroundIndexProgress' + kind='end' 表示后台索引完成。"""
        params = msg.get("params", {})
        return (
            params.get("token") == "backgroundIndexProgress"
            and params.get("value", {}).get("kind") == "end"
        )

    def get_engine_info(self) -> dict:
        return {
            "engine": "clangd",
            "path": "B",
            "precision": "high",
            "compile_commands": self.compile_commands_path,
            "capabilities": [
                "精确跨文件跳转定义（含头文件）",
                "完整类型推断（含宏展开）",
                "精确引用查找",
            ],
            "limitations": [
                "依赖 compile_commands.json 质量",
                "如果是手工生成的，宏展开可能不完整",
            ],
        }

    def _open_document(self, uri: str, file_path: str):
        try:
            with open(file_path) as f:
                text = f.read()
        except Exception:
            return
        self._send_notification("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": "c", "version": 1, "text": text}
        })

    def _find_enclosing_function(self, file_path: str, line: int) -> str | None:
        """向上搜索最近的 C 函数定义行（低缩进 + 函数签名形式）。"""
        try:
            with open(file_path) as f:
                all_lines = f.readlines()
        except Exception:
            return None

        _CONTROL = {"if", "for", "while", "switch", "return", "sizeof", "typeof"}
        # 匹配 C 函数定义：可选修饰符 + 返回类型 + 函数名 + (
        pattern = re.compile(
            r'^(?:(?:static|extern|inline|volatile|const)\s+)*'
            r'(?:(?:unsigned|signed|long|short)\s+)*'
            r'\w[\w\s\*]*\b(\w+)\s*\('
        )

        for i in range(line - 1, -1, -1):
            match = pattern.match(all_lines[i])
            if match and match.group(1) not in _CONTROL:
                return match.group(1)
        return None

    def _extract_callees_from_body(self, body: str) -> list[str]:
        return find_function_calls(body.encode("utf-8"), "c")

    def _parse_struct_fields(self, body: str) -> list[dict]:
        """
        解析 C 语言结构体字段，兼容两种形式：
          struct Name { ... };
          typedef struct { ... } Name;
        """
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

        struct_body = body[start + 1: end]
        _SKIP = {"if", "for", "while", "return", "else", "typedef",
                 "struct", "union", "enum", "extern", "static"}

        # 匹配：[修饰符] type [*]field_name[可选数组];
        pattern = re.compile(
            r'^\s+'
            r'((?:(?:struct|enum|union|const|volatile|unsigned|signed|long|short)\s+)*\w+)'
            r'[\s\*]+'
            r'(\*?\w+(?:\[.*?\])?)'
            r'\s*;',
            re.MULTILINE,
        )
        fields = []
        for match in pattern.finditer(struct_body):
            type_part = match.group(1).strip()
            name_part = match.group(2).strip().lstrip('*')
            if name_part in _SKIP:
                continue
            fields.append({"name": name_part, "type": type_part})

        return fields
