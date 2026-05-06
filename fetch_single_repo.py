import os
import json
import argparse
from pathlib import Path
from git import Repo


def extract_commit_logs(repo_path):
    """提取该仓库的最近 100 条 commit 记录"""
    repo = Repo(repo_path)
    commits = list(repo.iter_commits(max_count=100))
    logs = []
    for c in commits:
        logs.append({
            "hash": c.hexsha,
            "author": c.author.name,
            "date": c.authored_datetime.isoformat(),
            "message": c.message.strip()
        })
    return logs


def fetch_repo(url: str, output_dir: str = "./data/historical_repos",
               meta_dir: str = "./data/metadata") -> str:
    """克隆远程仓库并写入元数据，返回本地仓库路径。"""
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(meta_dir, exist_ok=True)

    repo_name = url.rstrip("/").split("/")[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]

    project_path = os.path.join(output_dir, repo_name)
    print(f"正在处理项目: {repo_name}...")

    if not os.path.exists(project_path):
        print(f"  -> 开始从 {url} 克隆...")
        try:
            Repo.clone_from(url, project_path)
        except Exception as e:
            raise RuntimeError(f"克隆失败，请检查链接是否正确且仓库是否公开: {e}")
    else:
        print("  -> 目录已存在，跳过克隆")
        repo = Repo(project_path)
        # 兼容旧版 no_checkout 克隆：工作区为空时补做 checkout
        tracked = [f for f in os.listdir(project_path) if f != ".git"]
        if not tracked and repo.heads:
            print("  -> 工作区为空，执行 checkout...")
            repo.heads[0].checkout()

    print("  -> 提取 commit 日志...")
    try:
        logs = extract_commit_logs(project_path)
    except Exception as e:
        print(f"  -> 提取日志失败: {e}")
        logs = []

    metadata = {
        "id": repo_name,
        "name": repo_name,
        "description": "",
        "path": project_path,
        "url": url,
        "recent_commits": logs,
    }

    meta_file = os.path.join(meta_dir, "all_repos_info.json")
    existing = []
    if os.path.exists(meta_file):
        try:
            with open(meta_file, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    existing = [r for r in existing if r.get("name") != repo_name]
    existing.append(metadata)

    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    print(f"  -> 完成，路径: {project_path}")
    return project_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="克隆远程仓库并生成元数据")
    parser.add_argument("url", nargs="?",
                        default="https://gitlab.eduxiji.net/educg-group-36002-2710490/T202510008995695-2259.git",
                        help="仓库 HTTPS 地址")
    parser.add_argument("--output-dir", default="./data/historical_repos",
                        help="本地仓库存放目录（默认 ./data/historical_repos）")
    parser.add_argument("--meta-dir", default="./data/metadata",
                        help="元数据存放目录（默认 ./data/metadata）")
    args = parser.parse_args()

    fetch_repo(args.url, args.output_dir, args.meta_dir)
