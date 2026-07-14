try:
    import tomllib                  # Python ≥ 3.11 内置
except ImportError:
    try:
        import tomli as tomllib     # pip install tomli
    except ImportError:
        raise ImportError("需要 Python ≥ 3.11，或运行：pip install tomli")

from pathlib import Path

_project_root = Path(__file__).resolve().parents[2]
_path = _project_root / "config.toml"
if _path.is_file():
    with _path.open("rb") as _f:
        _cfg = tomllib.load(_f)
else:
    # 允许 --help 等只读入口在尚未创建本地配置时启动；真正分析仍使用原有默认目录。
    _cfg = {
        "api": {"key": "", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
        "data": {"repos_dir": "./data/historical_repos",
                 "metadata_dir": "./data/metadata", "cache_dir": "./data/cache"},
        "target": {"repo_id": ""},
        "engine": {"rust_analyzer_timeout": 120, "clangd_timeout": 60,
                   "max_call_depth": 3, "max_steps": 30,
                   "skip_dirs": ["vendor", "third_party", "target"]},
    }

api    = _cfg["api"]
data   = _cfg["data"]
target = _cfg.get("target", {})
engine = _cfg["engine"]
