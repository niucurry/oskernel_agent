"""
参考 OS 指纹库：用于 T6 compare_with_reference_os 的代码级相似度分析。

公开 API：
  normalize_code(code)            代码归一化（消除注释 / 变量名 / 空白差异）
  compute_similarity(code_a, b)   三指标加权相似度（0.0-1.0）
  ReferenceOSDatabase             按需加载；缺失或损坏时自动重建指纹 JSON
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from difflib import SequenceMatcher
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE_CONFIG = PROJECT_ROOT / "config/reference_sources.yaml"
DEFAULT_SOURCE_CACHE = PROJECT_ROOT / "data/reference_sources"


class ReferenceDatabaseError(RuntimeError):
    """参考 OS 指纹库无法加载且自动重建未成功。"""


# 代码归一化

def normalize_code(code: str) -> str:
    """消除表面差异，只保留控制流 + 调用结构特征。

    消除：行注释 / 块注释 / 字符串字面量 / 数字（保留 0/1）/ 多余空白
    保留：关键字 / 运算符 / 函数名 / 控制流结构
    """
    # 行注释（C/Rust 都适用）
    code = re.sub(r"//[^\n]*", "", code)
    # 块注释
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    # 字符串字面量
    code = re.sub(r'"(?:[^"\\]|\\.)*"', '"STR"', code)
    code = re.sub(r"'(?:[^'\\]|\\.)*'", "'STR'", code)
    # 数字（保留 0 / 1，其余替换为 NUM）
    code = re.sub(r"\b(?!0\b|1\b)\d+\b", "NUM", code)
    # 归一化空白
    code = re.sub(r"\s+", " ", code).strip()
    return code


# 相似度计算

_MAX_CHARS_FOR_SEQ = 8_000   # 超过此长度截断，防止 SequenceMatcher 过慢

def compute_similarity(code_a: str, code_b: str) -> float:
    """三指标加权相似度：字符序列 × 0.4 + token Jaccard × 0.3 + 调用序列 × 0.3。"""
    norm_a = normalize_code(code_a)
    norm_b = normalize_code(code_b)

    if not norm_a and not norm_b:
        return 1.0
    if not norm_a or not norm_b:
        return 0.0

    # 指标 1：字符级编辑距离（长代码截断）
    a_trunc = norm_a[:_MAX_CHARS_FOR_SEQ]
    b_trunc = norm_b[:_MAX_CHARS_FOR_SEQ]
    seq_sim = SequenceMatcher(None, a_trunc, b_trunc).ratio()

    # 指标 2：token 集合 Jaccard
    tok_a = set(norm_a.split())
    tok_b = set(norm_b.split())
    union = tok_a | tok_b
    jaccard = len(tok_a & tok_b) / len(union) if union else 0.0

    # 指标 3：调用序列相似度（保留调用顺序）
    calls_a = re.findall(r"\b(\w+)\s*\(", norm_a)
    calls_b = re.findall(r"\b(\w+)\s*\(", norm_b)
    call_sim = SequenceMatcher(None, calls_a, calls_b).ratio()

    return seq_sim * 0.4 + jaccard * 0.3 + call_sim * 0.3


# ReferenceOSDatabase

class ReferenceOSDatabase:
    """
    参考 OS 指纹库：按需加载各参考 OS 的函数级代码摘要。

    每个参考 OS 对应一个 JSON 文件，格式：
    {
        "func_name": {
            "body_normalized": "...",   # normalize_code 后的结果
            "body_hash":       "md5...",
            "calls":           [...],   # 调用的函数名列表
            "file":            "...",   # 相对路径
            "line_count":      42
        },
        ...
    }

    正式流程调用 ``load_or_rebuild``：先严格校验已有 JSON；文件缺失、JSON 损坏或
    内容不完整时，从 ``config/reference_sources.yaml`` 指定的固定源码版本自动重建。
    构建结果先写同目录临时文件，通过校验后再原子替换，避免中断时留下半成品。
    """

    SUPPORTED: tuple[str, ...] = (
        "rcore-tutorial-v3",
        "rcore-tutorial-v2",
        "xv6-riscv",
        "ucore",
    )

    def __init__(
        self,
        db_dir: str | os.PathLike[str],
        *,
        source_config: str | os.PathLike[str] = DEFAULT_SOURCE_CONFIG,
        source_cache_dir: str | os.PathLike[str] = DEFAULT_SOURCE_CACHE,
        source_specs: dict[str, dict] | None = None,
    ):
        def _resolve(value: str | os.PathLike[str]) -> Path:
            path = Path(value).expanduser()
            return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()

        self.db_dir = str(_resolve(db_dir))
        self.source_config = _resolve(source_config)
        self.source_cache_dir = _resolve(source_cache_dir)
        self._source_specs_override = source_specs
        self._cache: dict[str, dict] = {}
        self._rebuild_lock = threading.Lock()

    def is_available(self, reference_name: str) -> bool:
        """指纹文件是否存在且通过结构校验；不会在探测阶段触发网络访问。"""
        try:
            return bool(self.load(reference_name))
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    def load(self, reference_name: str) -> dict:
        """严格加载指定参考 OS 的指纹数据，未找到时返回空字典。"""
        if reference_name in self._cache:
            return self._cache[reference_name]

        path = Path(self.db_dir) / f"{reference_name}.json"
        if not path.exists():
            return {}

        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self._validate_payload(reference_name, data)
        self._cache[reference_name] = data
        print(f"[ReferenceDB] 已加载 {reference_name}：{len(data)} 个函数")
        return data

    @staticmethod
    def _validate_payload(reference_name: str, data: object) -> None:
        if not isinstance(data, dict) or not data:
            raise ValueError(f"{reference_name} 指纹库为空或根节点不是对象")
        required = {"body_normalized", "body_hash", "calls", "file", "line_count"}
        for name, record in data.items():
            if not isinstance(name, str) or not name or not isinstance(record, dict):
                raise ValueError(f"{reference_name} 指纹库包含无效函数记录")
            missing = required - set(record)
            if missing:
                raise ValueError(
                    f"{reference_name} 函数 {name} 缺少字段：{', '.join(sorted(missing))}"
                )
            if not isinstance(record.get("body_normalized"), str) or not record["body_normalized"]:
                raise ValueError(f"{reference_name} 函数 {name} 缺少规范化代码")
            if not isinstance(record.get("body_hash"), str) or not record["body_hash"]:
                raise ValueError(f"{reference_name} 函数 {name} 的源码哈希无效")
            if not isinstance(record.get("calls"), list):
                raise ValueError(f"{reference_name} 函数 {name} 的 calls 不是数组")
            if not isinstance(record.get("file"), str) or not record["file"]:
                raise ValueError(f"{reference_name} 函数 {name} 的文件路径无效")
            if not isinstance(record.get("line_count"), int) or record["line_count"] <= 0:
                raise ValueError(f"{reference_name} 函数 {name} 的行数无效")

    def load_or_rebuild(self, reference_name: str) -> dict:
        """加载有效指纹库；缺失或损坏时自动从固定参考源码重建一次。"""
        if reference_name not in self.SUPPORTED:
            raise ReferenceDatabaseError(
                f"未知参考 OS：{reference_name}；支持：{', '.join(self.SUPPORTED)}"
            )
        try:
            data = self.load(reference_name)
            if data:
                return data
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[ReferenceDB] {reference_name} 加载失败，将自动重建：{exc}")

        # 同一进程中的并发评判只允许一个重建者；等待者拿锁后先复查，避免重复构建。
        with self._rebuild_lock:
            self._cache.pop(reference_name, None)
            try:
                data = self.load(reference_name)
                if data:
                    return data
            except (OSError, ValueError, json.JSONDecodeError):
                self._cache.pop(reference_name, None)

            try:
                self.rebuild(reference_name)
                data = self.load(reference_name)
            except Exception as exc:
                self._cache.pop(reference_name, None)
                raise ReferenceDatabaseError(
                    f"{reference_name} 指纹库不可用，自动重建失败：{exc}"
                ) from exc
            if not data:  # 防御性检查；正常会由 _validate_payload 更早拦截。
                raise ReferenceDatabaseError(f"{reference_name} 自动重建后仍为空")
            return data

    def _source_specs(self) -> dict[str, dict]:
        if self._source_specs_override is not None:
            return self._source_specs_override
        if not self.source_config.is_file():
            raise ReferenceDatabaseError(f"参考源码配置不存在：{self.source_config}")
        payload = yaml.safe_load(self.source_config.read_text(encoding="utf-8")) or {}
        sources = payload.get("sources")
        if not isinstance(sources, dict):
            raise ReferenceDatabaseError(
                f"参考源码配置缺少 sources 对象：{self.source_config}"
            )
        return sources

    @staticmethod
    def _has_source_code(path: Path) -> bool:
        return path.is_dir() and (
            any(path.rglob("*.rs")) or any(path.rglob("*.c"))
        )

    @staticmethod
    def _git(args: list[str], *, cwd: Path | None = None, timeout: int = 300) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "git 命令失败").strip()
            raise ReferenceDatabaseError(detail[-1000:])
        return proc.stdout.strip()

    def _cached_source_matches(self, path: Path, revision: str) -> bool:
        if not self._has_source_code(path):
            return False
        if not revision:
            return True
        try:
            head = self._git(["rev-parse", "HEAD"], cwd=path, timeout=30)
        except ReferenceDatabaseError:
            return False
        return head == revision or head.startswith(revision)

    def _clone_source(self, reference_name: str, spec: dict) -> Path:
        repo_url = str(spec.get("repo_url") or "").strip()
        revision = str(spec.get("revision") or "").strip()
        if not repo_url:
            raise ReferenceDatabaseError(f"{reference_name} 未配置 repo_url")

        rev_key = revision[:12] if revision else "default"
        cache_path = self.source_cache_dir / f"{reference_name}-{rev_key}"
        if self._cached_source_matches(cache_path, revision):
            return cache_path

        self.source_cache_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.source_cache_dir / (
            f".{reference_name}-{rev_key}.clone-{uuid.uuid4().hex}"
        )
        try:
            self._git(["init", str(temp_path)])
            self._git(["remote", "add", "origin", repo_url], cwd=temp_path)
            fetch_target = revision or "HEAD"
            self._git(["fetch", "--depth", "1", "origin", fetch_target], cwd=temp_path)
            self._git(["checkout", "--detach", "FETCH_HEAD"], cwd=temp_path)
            if not self._cached_source_matches(temp_path, revision):
                raise ReferenceDatabaseError(
                    f"{reference_name} 拉取版本与配置 revision 不一致"
                )

            if cache_path.exists():
                stale = cache_path.with_name(f".{cache_path.name}.stale-{uuid.uuid4().hex}")
                os.replace(cache_path, stale)
                os.replace(temp_path, cache_path)
                shutil.rmtree(stale, ignore_errors=True)
            else:
                os.replace(temp_path, cache_path)
            return cache_path
        finally:
            if temp_path.exists():
                shutil.rmtree(temp_path, ignore_errors=True)

    def _source_path(self, reference_name: str) -> Path:
        spec = self._source_specs().get(reference_name)
        if not isinstance(spec, dict):
            raise ReferenceDatabaseError(f"{reference_name} 未配置参考源码")
        local_path = str(spec.get("local_path") or "").strip()
        if local_path:
            source = Path(local_path).expanduser().resolve()
            if not self._has_source_code(source):
                raise ReferenceDatabaseError(f"{reference_name} 本地参考源码无效：{source}")
        else:
            source = self._clone_source(reference_name, spec)

        subdir = str(spec.get("source_subdir") or "").strip()
        source = (source / subdir).resolve() if subdir else source.resolve()
        if not self._has_source_code(source):
            raise ReferenceDatabaseError(
                f"{reference_name} 参考源码目录没有 Rust/C 源码：{source}"
            )
        return source

    def rebuild(self, reference_name: str) -> int:
        """从配置的固定源码版本重建一个指纹库，并在校验后原子替换旧文件。"""
        source = self._source_path(reference_name)
        db_dir = Path(self.db_dir)
        db_dir.mkdir(parents=True, exist_ok=True)
        target = db_dir / f"{reference_name}.json"
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{reference_name}.", suffix=".json.tmp", dir=db_dir,
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            count = self.build_from_repo(reference_name, str(source), str(temp_path))
            if count <= 0:
                raise ReferenceDatabaseError(f"{reference_name} 未提取到任何函数")
            payload = json.loads(temp_path.read_text(encoding="utf-8"))
            self._validate_payload(reference_name, payload)
            os.replace(temp_path, target)
            self._cache.pop(reference_name, None)
            print(f"[ReferenceDB] {reference_name} 自动重建完成：{count} 个函数")
            return count
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def build_from_repo(ref_name: str, repo_path: str, output_path: str) -> int:
        """
        离线构建指纹库（对每个参考 OS 运行一次即可）。

        参数：
          ref_name    参考 OS 名称（仅用于日志）
          repo_path   参考 OS 的本地路径
          output_path 输出 JSON 文件路径

        返回构建的函数数量。
        """
        from ..engines.path_c import TreeSitterEngine

        # 探测语言
        c_files  = len(list(Path(repo_path).rglob("*.c")))
        rs_files = len(list(Path(repo_path).rglob("*.rs")))
        lang = "rust" if rs_files > c_files else "c"
        print(f"[Build] {ref_name}：探测语言={lang}，"
              f"C 文件={c_files}，RS 文件={rs_files}")

        engine = TreeSitterEngine(repo_path, lang)

        fingerprints: dict[str, dict] = {}
        for key, entry in engine._func_index.items():
            body = entry.get("body", "")
            if not body:
                continue
            name = entry.get("name", key)
            fingerprints[name] = {
                "body_normalized": normalize_code(body),
                "body_hash":       hashlib.md5(
                    body.encode(errors="replace")
                ).hexdigest(),
                "calls":           entry.get("calls", []),
                "file":            entry.get("file", ""),
                "line_count":      body.count("\n") + 1,
            }

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(fingerprints, f, ensure_ascii=False, indent=2)

        print(f"[Build] {ref_name}：写入 {len(fingerprints)} 个函数，保存到 {output_path}")
        return len(fingerprints)
