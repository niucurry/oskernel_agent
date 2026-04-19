import os
import json
from git import Repo

# 想要测试的单个公开仓库的 HTTPS 链接
TARGET_REPO_URL = 'https://gitlab.eduxiji.net/educg-group-36002-2710490/T202510008995695-2259.git' 

def extract_commit_logs(repo_path):
    """提取该仓库的最近 Commit 记录"""
    repo = Repo(repo_path)
    # 获取最近 100 次提交，如果总数不够 100 也不会报错
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

def test_single_repo():
    output_dir = "./data/historical_repos"
    meta_dir = "./data/metadata"
    
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    if not os.path.exists(meta_dir):
        os.makedirs(meta_dir)

    # 从 URL 中提取项目名称作为文件夹名（去掉末尾的 .git）
    repo_name = TARGET_REPO_URL.split('/')[-1]
    if repo_name.endswith('.git'):
        repo_name = repo_name[:-4]

    project_path = os.path.join(output_dir, repo_name)
    print(f"正在处理测试项目: {repo_name}...")

    #  克隆代码
    if not os.path.exists(project_path):
        print(f"  -> 开始从 {TARGET_REPO_URL} 克隆...")
        try:
            Repo.clone_from(TARGET_REPO_URL, project_path)
        except Exception as e:
            print(f" 克隆失败，请检查链接是否正确且仓库是否公开: {e}")
            return
    else:
        print(f"  -> 目录已存在，跳过克隆")

    #  提取 Commit 日志
    print(f"  -> 提取 Commit 日志...")
    try:
        logs = extract_commit_logs(project_path)
    except Exception as e:
        print(f"  -> 提取日志失败: {e}")
        logs = []

    #  构建与 Agent Demo 完全兼容的元数据格式
    metadata = {
        "id": "test_id_001",
        "name": repo_name,
        "description": "单点测试项目", # 因为没用 API，这里写个占位符
        "path": project_path,
        "url": TARGET_REPO_URL,
        "recent_commits": logs
    }

    # 为了能让后面的大模型脚本直接读取，这里将它打包成列表形式写入 JSON
    meta_file = os.path.join(meta_dir, "all_repos_info.json")
    with open(meta_file, 'w', encoding='utf-8') as f:
        json.dump([metadata], f, ensure_ascii=False, indent=2)
    
    print(f"\n 单个项目数据处理完毕！日志已提取并保存至: {meta_file}")

if __name__ == "__main__":
    test_single_repo()