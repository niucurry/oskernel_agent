import os
import argparse
from collections import Counter
from git import Repo

from oskernel_agent.repository_identity import repository_display_name, repository_storage_key


def extract_commit_logs(repo_path: str) -> list[dict]:
    """提取仓库全量 commit 记录。"""
    repo = Repo(repo_path)
    logs = []
    for c in repo.iter_commits():
        logs.append({
            "hash":    c.hexsha,
            "author":  c.author.name,
            "date":    c.authored_datetime.isoformat(),
            "message": c.message.strip(),
        })
    return logs


def summarize_commits(repo_path: str) -> str:
    """返回供 LLM 阅读的 commit 历史摘要文本。"""
    try:
        logs = extract_commit_logs(repo_path)
    except Exception as e:
        return f"（无法提取 commit 历史：{e}）"

    if not logs:
        return "（该仓库暂无 commit 记录）"

    total = len(logs)
    authors = Counter(c["author"] for c in logs)
    top_authors = authors.most_common(5)
    earliest = logs[-1]["date"][:10]
    latest   = logs[0]["date"][:10]
    recent_msgs = "\n".join(f"  - {c['message'].splitlines()[0]}" for c in logs[:20])

    author_lines = "\n".join(f"  {name}（{n} commits）" for name, n in top_authors)
    return (
        f"【Git 历史摘要】\n"
        f"总 commit 数：{total}\n"
        f"时间跨度：{earliest} → {latest}\n"
        f"主要贡献者：\n{author_lines}\n"
        f"最近 20 条提交：\n{recent_msgs}"
    )


def fetch_repo(url: str, output_dir: str = "./data/historical_repos") -> str:
    """克隆远程仓库，返回本地仓库路径。"""
    os.makedirs(output_dir, exist_ok=True)

    repo_name = repository_display_name(url)
    project_path = os.path.join(output_dir, repository_storage_key(url))
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

    print(f"  -> 完成，路径: {project_path}")
    return project_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="克隆远程仓库")
    parser.add_argument("url", help="仓库 HTTPS 地址")
    parser.add_argument("--output-dir", default="./data/historical_repos",
                        help="本地仓库存放目录（默认 ./data/historical_repos）")
    args = parser.parse_args()

    path = fetch_repo(args.url, args.output_dir)
    print(summarize_commits(path))
