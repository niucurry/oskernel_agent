"""One-time retirement: verify an external archive before removing old experiments."""
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import shutil
import tarfile


def fingerprint(path):
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"kind": "file", "sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def main():
    root = Path(__file__).resolve().parents[2]
    here = Path(__file__).resolve().parent
    plan = json.loads((here / "cleanup-plan.json").read_text())
    assert str(root) == plan["root"]
    result_path = here / "cleanup-result.json"
    if result_path.exists():
        raise SystemExit("Cleanup already recorded; refusing to repeat")
    keep = set(plan["preserve_data"])
    entries = {}
    for relative in plan["retire_trees"] + plan["retire_files"]:
        base = root / relative
        assert base.resolve().is_relative_to(root)
        paths = base.rglob("*") if base.is_dir() else [base]
        for path in paths:
            name = str(path.relative_to(root))
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_file() or path.is_symlink():
                entries[name] = fingerprint(path)
    freeze = json.loads((root / "research/cfc_heldout/system-freeze.json").read_text())["files"]
    assert all(entries[name]["sha256"] == expected for name, expected in freeze.items())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = root.parent / "agent-archives" / ("retired-behavior-" + stamp)
    archive_dir.mkdir(parents=True, mode=0o700)
    archive = archive_dir / "experiments.tar.gz"
    manifest = {"created_utc": stamp, "original_root": str(root), "entries": entries,
                "frozen_files": freeze, "plan": plan}
    blob = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    (archive_dir / "manifest.json").write_bytes(blob)
    print(f"Archiving {len(entries)} entries", flush=True)
    with tarfile.open(archive, "w:gz", compresslevel=3) as out:
        for name in sorted(entries):
            # Store regular files independently even if the source uses hardlinks.
            out.inodes.clear()
            out.add(root / name, arcname=name, recursive=False)
        info = tarfile.TarInfo("cleanup-manifest.json")
        info.size = len(blob)
        out.addfile(info, io.BytesIO(blob))
    print("Verifying every archived file before deletion", flush=True)
    verified = set()
    with tarfile.open(archive, "r|gz") as source:
        for member in source:
            if member.name == "cleanup-manifest.json":
                assert source.extractfile(member).read() == blob
                continue
            expected = entries[member.name]
            if expected["kind"] == "symlink":
                assert member.issym() and member.linkname == expected["target"]
            else:
                assert member.isfile()
                digest = sha256()
                stream = source.extractfile(member)
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                assert digest.hexdigest() == expected["sha256"], member.name
            verified.add(member.name)
    assert verified == set(entries)
    deleted_bytes = 0
    deleted_count = 0
    for name, expected in entries.items():
        if name in keep:
            continue
        path = root / name
        assert fingerprint(path) == expected, f"Changed during archive: {name}"
        deleted_bytes += expected.get("bytes", 0)
        path.unlink()
        deleted_count += 1
    for path in list(root.rglob("__pycache__")) + [root / ".pytest_cache", root / ".ruff_cache"]:
        if path.is_dir() and not path.is_symlink() and ".git" not in path.relative_to(root).parts:
            shutil.rmtree(path)
    for name in plan["retire_trees"]:
        base = root / name
        for path in sorted([*base.rglob("*"), base], key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not path.is_symlink():
                try:
                    path.rmdir()
                except OSError:
                    pass
    assert all(fingerprint(root / name)["sha256"] == expected for name, expected in plan["preserve_data"].items())
    result = {"archive": str(archive), "archive_sha256": fingerprint(archive)["sha256"],
              "archive_bytes": archive.stat().st_size, "archived_entries": len(entries),
              "deleted_entries": deleted_count, "deleted_file_bytes": deleted_bytes,
              "preserved_data_files": len(keep), "frozen_files_verified_in_archive": len(freeze)}
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
