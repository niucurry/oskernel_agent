try:
    import tomllib                  # Python ≥ 3.11 内置
except ImportError:
    try:
        import tomli as tomllib     # pip install tomli
    except ImportError:
        raise ImportError("需要 Python ≥ 3.11，或运行：pip install tomli")

from pathlib import Path

_path = Path(__file__).resolve().parents[2] / "config.toml"
with open(_path, "rb") as _f:
    _cfg = tomllib.load(_f)

api    = _cfg["api"]
data   = _cfg["data"]
target = _cfg.get("target", {})
engine = _cfg["engine"]
