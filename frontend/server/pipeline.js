import fs from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { findPython, PROJECT_ROOT } from "./config.js";
import { nowIso } from "./db.js";
import {
  findExistingReports,
  hasReportKinds,
  normalizeReportKinds,
  promoteReport,
  registerReport,
  reportDir,
  syncExistingReports
} from "./reportFiles.js";

function jobId() {
  return `job_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`;
}

function trimLog(log) {
  return log.length > 60000 ? log.slice(-60000) : log;
}

function oneLine(text, maxLength = 700) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}…` : value;
}

function logLines(log) {
  return String(log || "")
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function findLastLine(lines, tester) {
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (tester(lines[i])) return lines[i];
  }
  return "";
}

export function extractFailureReason(log, exitCode = null) {
  const lines = logLines(log);
  if (!lines.length) return exitCode === null ? "流水线未输出错误日志" : `流水线退出码 ${exitCode}`;

  const moduleError = findLastLine(lines, (line) => /^ModuleNotFoundError:/.test(line));
  if (moduleError) return oneLine(`缺少 Python 依赖：${moduleError.replace(/^ModuleNotFoundError:\s*/, "")}`);

  const importError = findLastLine(lines, (line) => /^(ImportError|FileNotFoundError|PermissionError|ValueError|RuntimeError):/.test(line));
  if (importError) return oneLine(importError);

  const calledProcessError = findLastLine(lines, (line) => /CalledProcessError:/.test(line));
  if (calledProcessError) {
    const gitError = findLastLine(lines, (line) => /^(fatal|error|remote):\s+/i.test(line));
    return oneLine(gitError ? `${calledProcessError}；${gitError}` : calledProcessError);
  }

  const directError = findLastLine(lines, (line) => /^(fatal|error|exception):\s+/i.test(line));
  if (directError) return oneLine(directError);

  const tracebackIndex = lines.lastIndexOf("Traceback (most recent call last):");
  if (tracebackIndex >= 0) {
    const tracebackTail = lines.slice(tracebackIndex + 1).findLast((line) => /^[A-Za-z_][\w.]*Error:/.test(line));
    if (tracebackTail) return oneLine(tracebackTail);
  }

  return oneLine(lines.at(-1) || `流水线退出码 ${exitCode ?? "未知"}`);
}

function failureMessage(log, exitCode) {
  const reason = extractFailureReason(log, exitCode);
  return `流水线失败（退出码 ${exitCode}）：${reason}`;
}

function parseExitCode(error) {
  const match = String(error || "").match(/退出码\s+(-?\d+)/);
  return match ? Number(match[1]) : null;
}

function pipelineEnv() {
  return {
    ...process.env,
    PYTHONUTF8: "1",
    AGENT_SUBSYS_CONCURRENCY: process.env.AGENT_SUBSYS_CONCURRENCY || "4",
    AGENT_LLM_CONCURRENCY: process.env.AGENT_LLM_CONCURRENCY || "4"
  };
}

async function killProcessTree(child) {
  if (!child?.pid) return;
  if (process.platform === "win32") {
    await new Promise((resolve) => {
      const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true });
      killer.on("close", resolve);
      killer.on("error", () => {
        child.kill("SIGTERM");
        resolve();
      });
    });
    return;
  }
  child.kill("SIGTERM");
}

async function findFilesBySuffix(dir, suffix, skipDirs = new Set(["_repos", "node_modules", ".git"])) {
  const results = [];
  async function walk(current) {
    const entries = await fs.readdir(current, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) {
        if (!skipDirs.has(entry.name)) await walk(full);
      } else if (entry.isFile() && entry.name.toLowerCase().endsWith(suffix)) {
        results.push(full);
      }
    }
  }
  await walk(dir);
  return results;
}

async function findGeneratedComparisonHtml(repoId) {
  // 查重流水线（src.pipeline 的 _finalize_comparison_output）会把最终报告归档到
  //   <output-dir>/<仓库名>/<仓库名>_comparison.html
  // 的子目录，而不是直接放在 output-dir 顶层，所以这里递归查找 *_comparison.html，
  // 取最新的一个（promoteReport 随后会拷贝为顶层 comparison.html）。
  const candidates = await findFilesBySuffix(reportDir(repoId), "_comparison.html");
  if (!candidates.length) {
    // 兜底：兼容已 promote 到顶层的旧报告
    const reports = await findExistingReports(repoId);
    return reports.find((report) => report.kind === "comparison")?.absPath || null;
  }
  const withStat = await Promise.all(
    candidates.map(async (absPath) => ({ absPath, mtime: (await fs.stat(absPath)).mtimeMs }))
  );
  withStat.sort((a, b) => b.mtime - a.mtime);
  return withStat[0].absPath;
}

async function findGeneratedComparisonDigest(repoId) {
  const candidates = await findFilesBySuffix(reportDir(repoId), "_comparison.digest.json");
  if (!candidates.length) return null;
  const withStat = await Promise.all(
    candidates.map(async (absPath) => ({ absPath, mtime: (await fs.stat(absPath)).mtimeMs }))
  );
  withStat.sort((a, b) => b.mtime - a.mtime);
  return withStat[0].absPath;
}

async function pathExists(target) {
  try {
    await fs.access(target);
    return true;
  } catch {
    return false;
  }
}

function safeRepoDirectoryName(repoUrl, fallback) {
  const raw = (() => {
    try {
      const url = new URL(repoUrl);
      return url.pathname.split("/").filter(Boolean).at(-1) || fallback;
    } catch {
      return String(repoUrl || fallback).split(/[\\/]/).filter(Boolean).at(-1) || fallback;
    }
  })();
  const withoutGit = raw.replace(/\.git$/i, "");
  return withoutGit.replace(/[<>:"/\\|?*\x00-\x1F]/g, "_") || fallback;
}

async function findClonedRepoPath(repoId) {
  const reposDir = path.join(reportDir(repoId), "_repos");
  const entries = await fs.readdir(reposDir, { withFileTypes: true }).catch(() => []);
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const candidate = path.join(reposDir, entry.name);
    if (await pathExists(path.join(candidate, ".git"))) return candidate;
  }
  return null;
}

export class PipelineQueue {
  constructor(database) {
    this.db = database;
    this.queue = [];
    this.running = false;
    this.activeChildren = new Map();
    this.jobReportKinds = new Map();
  }

  async enqueue(repoId, reportKinds = null) {
    const repo = this.db.get("SELECT * FROM repositories WHERE id = ?", [repoId]);
    if (!repo) throw new Error(`作品不存在：${repoId}`);

    const requestedKinds = normalizeReportKinds(reportKinds);
    const existing = await syncExistingReports(this.db, repoId);
    const id = jobId();
    if (hasReportKinds(existing, requestedKinds)) {
      await this.createJob(id, repoId, "skipped", `所选报告已存在，直接使用本地报告：${requestedKinds.join(", ")}`);
      return this.db.get("SELECT * FROM jobs WHERE id = ?", [id]);
    }

    await this.createJob(id, repoId, "queued", "");
    this.jobReportKinds.set(id, requestedKinds);
    this.queue.push(id);
    this.drain();
    return this.db.get("SELECT * FROM jobs WHERE id = ?", [id]);
  }

  async createJob(id, repoId, status, log) {
    this.db.db.run(
      `INSERT INTO jobs (id, repo_id, type, status, log, created_at)
       VALUES (?, ?, 'pipeline', ?, ?, ?)`,
      [id, repoId, status, log, nowIso()]
    );
    await this.db.save();
  }

  async drain() {
    if (this.running) return;
    this.running = true;
    while (this.queue.length) {
      const id = this.queue.shift();
      const current = this.db.get("SELECT * FROM jobs WHERE id = ?", [id]);
      if (!current || current.status !== "queued") continue;
      await this.runJob(id);
    }
    this.running = false;
  }

  async updateJob(id, patch) {
    const current = this.db.get("SELECT * FROM jobs WHERE id = ?", [id]);
    if (!current) return;
    const next = { ...current, ...patch };
    this.db.db.run(
      `UPDATE jobs
       SET status = ?, command = ?, log = ?, error = ?, started_at = ?, finished_at = ?
       WHERE id = ?`,
      [
        next.status,
        next.command || "",
        next.log || "",
        next.error || null,
        next.started_at || null,
        next.finished_at || null,
        id
      ]
    );
    await this.db.save();
  }

  async setRepoStatus(repoId, status, error = null) {
    this.db.db.run(
      "UPDATE repositories SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
      [status, error, nowIso(), repoId]
    );
    await this.db.save();
  }

  async resetRepoAfterJobRemoval(repoId) {
    const repo = this.db.get("SELECT * FROM repositories WHERE id = ?", [repoId]);
    if (!repo || repo.status === "ready") return;
    const active = this.db.get(
      "SELECT id FROM jobs WHERE repo_id = ? AND status IN ('queued', 'running') LIMIT 1",
      [repoId]
    );
    if (!active) await this.setRepoStatus(repoId, "pending", null);
  }

  async deleteJob(id) {
    const job = this.db.get("SELECT * FROM jobs WHERE id = ?", [id]);
    if (!job) return { deleted: 0, cancelled: false };

    const wasRunning = job.status === "running";
    this.queue = this.queue.filter((queuedId) => queuedId !== id);
    this.jobReportKinds.delete(id);

    if (wasRunning) {
      await this.setRepoStatus(job.repo_id, "pending", "任务已取消，可重新生成。");
      await killProcessTree(this.activeChildren.get(id));
    }

    this.db.db.run("DELETE FROM jobs WHERE id = ?", [id]);
    await this.db.save();
    if (!wasRunning) await this.resetRepoAfterJobRemoval(job.repo_id);
    return { deleted: 1, cancelled: wasRunning };
  }

  async clearJobs(statuses = ["queued"]) {
    const safeStatuses = statuses
      .map((status) => String(status || "").trim())
      .filter(Boolean);
    if (!safeStatuses.length) return { deleted: 0, cancelled: 0 };

    const placeholders = safeStatuses.map(() => "?").join(", ");
    const jobs = this.db.query(`SELECT id FROM jobs WHERE status IN (${placeholders})`, safeStatuses);
    let deleted = 0;
    let cancelled = 0;
    for (const job of jobs) {
      const result = await this.deleteJob(job.id);
      deleted += result.deleted;
      if (result.cancelled) cancelled += 1;
    }
    return { deleted, cancelled };
  }

  async recoverInterruptedJobs() {
    const now = nowIso();
    const staleQueued = this.db.query("SELECT * FROM jobs WHERE status = 'queued'");
    const staleRunning = this.db.query("SELECT * FROM jobs WHERE status = 'running'");
    const cancelledMessage = "服务重启后原等待队列已失效，请重新加入生成队列。";
    const interruptedMessage = "服务重启或进程已停止，任务未完成；请重新生成。";

    for (const job of staleQueued) {
      this.db.db.run(
        "UPDATE jobs SET status = 'cancelled', error = ?, finished_at = ? WHERE id = ?",
        [cancelledMessage, now, job.id]
      );
    }

    for (const job of staleRunning) {
      this.db.db.run(
        "UPDATE jobs SET status = 'failed', error = ?, finished_at = COALESCE(finished_at, ?) WHERE id = ?",
        [interruptedMessage, now, job.id]
      );
      this.db.db.run(
        "UPDATE repositories SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ? AND status = 'generating'",
        [interruptedMessage, now, job.repo_id]
      );
    }

    const generatingRepos = this.db.query("SELECT id FROM repositories WHERE status = 'generating'");
    for (const repo of generatingRepos) {
      const runningJob = this.db.get(
        "SELECT id FROM jobs WHERE repo_id = ? AND status = 'running' LIMIT 1",
        [repo.id]
      );
      if (!runningJob) {
        this.db.db.run(
          "UPDATE repositories SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
          [interruptedMessage, now, repo.id]
        );
      }
    }

    const failedJobs = this.db.query("SELECT id, repo_id, log, error FROM jobs WHERE status = 'failed' AND log != ''");
    for (const job of failedJobs) {
      const currentError = String(job.error || "");
      if (currentError && !/^流水线退出码\s+-?\d+$/.test(currentError)) continue;
      const exitCode = parseExitCode(currentError) ?? 1;
      const improved = failureMessage(job.log, exitCode);
      this.db.db.run("UPDATE jobs SET error = ? WHERE id = ?", [improved, job.id]);
      this.db.db.run(
        `UPDATE repositories
         SET last_error = ?, updated_at = ?
         WHERE id = ? AND status = 'failed' AND (last_error IS NULL OR last_error = ? OR last_error LIKE '流水线退出码 %')`,
        [improved, now, job.repo_id, currentError]
      );
    }

    await this.db.save();
    return {
      cancelledQueued: staleQueued.length,
      failedRunning: staleRunning.length
    };
  }

  async runProcess(id, label, executable, args, log, commandLines) {
    const command = [executable, ...args].join(" ");
    const nextCommandLines = [...commandLines, command];
    let nextLog = trimLog(`${log}${log ? "\n" : ""}[${label}] ${command}\n`);
    await this.updateJob(id, { command: nextCommandLines.join("\n"), log: nextLog });

    const child = spawn(executable, args, {
      cwd: PROJECT_ROOT,
      env: pipelineEnv(),
      windowsHide: true
    });

    this.activeChildren.set(id, child);
    child.stdout.on("data", async (data) => {
      nextLog = trimLog(nextLog + data.toString());
      await this.updateJob(id, { log: nextLog });
    });
    child.stderr.on("data", async (data) => {
      nextLog = trimLog(nextLog + data.toString());
      await this.updateJob(id, { log: nextLog });
    });

    const exitCode = await new Promise((resolve) => {
      child.on("close", resolve);
      child.on("error", (error) => {
        nextLog = trimLog(`${nextLog}\n${error.stack || error.message || String(error)}`);
        resolve(1);
      });
    });
    this.activeChildren.delete(id);

    return { exitCode, log: nextLog, commandLines: nextCommandLines };
  }

  async runJob(id) {
    const job = this.db.get("SELECT * FROM jobs WHERE id = ?", [id]);
    if (!job) return;
    const repo = this.db.get("SELECT * FROM repositories WHERE id = ?", [job.repo_id]);
    if (!repo) {
      await this.updateJob(id, { status: "failed", error: "作品记录不存在", finished_at: nowIso() });
      return;
    }

    const requestedKinds = this.jobReportKinds.get(id) || normalizeReportKinds();
    this.jobReportKinds.delete(id);
    await fs.mkdir(reportDir(repo.id), { recursive: true });
    const python = findPython();
    await this.setRepoStatus(repo.id, "generating");
    await this.updateJob(id, {
      status: "running",
      command: "",
      started_at: nowIso(),
      log: `请求生成报告：${requestedKinds.join(", ")}\n`
    });

    let existing = await syncExistingReports(this.db, repo.id);
    const summaryRequested = requestedKinds.includes("summary");
    const requiredKinds = summaryRequested
      ? ["comparison", "description", "development", "summary"]
      : requestedKinds;
    const missingSet = new Set(
      requiredKinds.filter((kind) => !hasReportKinds(existing, [kind]))
    );
    const digestPaths = {
      comparison: path.join(reportDir(repo.id), "comparison.digest.json"),
      description: path.join(reportDir(repo.id), "description.digest.json"),
      development: path.join(reportDir(repo.id), "development.digest.json")
    };
    if (summaryRequested) {
      for (const kind of ["comparison", "description", "development"]) {
        if (!(await pathExists(digestPaths[kind]))) missingSet.add(kind);
      }
      if (["comparison", "description", "development"].some((kind) => missingSet.has(kind))) {
        missingSet.add("summary");
      }
    }
    const missingKinds = ["comparison", "description", "development", "summary"]
      .filter((kind) => missingSet.has(kind));
    if (!missingKinds.length) {
      await this.updateJob(id, { status: "skipped", log: "所选报告已存在，直接使用本地报告。", finished_at: nowIso() });
      return;
    }

    let log = `请求生成报告：${requestedKinds.join(", ")}\n`;
    let commandLines = [];

    if (missingKinds.includes("comparison")) {
      const args = [
        "-X",
        "utf8",
        "-m",
        "src.pipeline",
        "--repo",
        repo.repo_url,
        "--output-dir",
        reportDir(repo.id),
        // 与后端「正确全流程」对齐：启用基线扣除，否则 confirmed 会因未扣上游基线而虚高
        // （实测某作品 confirmed 749→33 全靠此项）。缺基线数据时后端会自动降级为无操作。
        // 不加 --skip-ai-detect：AI 生成代码检测由流水线内部的 ai_detect 步产出，
        // 并入对比报告第六章（与后端一致，只测非借鉴函数）。无 GPU/模型时后端会优雅跳过。
        "--baselines"
      ];
      const result = await this.runProcess(id, "comparison", python, args, log, commandLines);
      log = result.log;
      commandLines = result.commandLines;

      if (!this.db.get("SELECT id FROM jobs WHERE id = ?", [id])) return;

      if (result.exitCode !== 0) {
        const error = failureMessage(log, result.exitCode);
        await this.setRepoStatus(repo.id, "failed", error);
        await this.updateJob(id, { status: "failed", error, log, finished_at: nowIso() });
        return;
      }

      const generated = await findGeneratedComparisonHtml(repo.id);
      if (!generated) {
        const error = "查重流水线完成但未发现 HTML 报告";
        await this.setRepoStatus(repo.id, "failed", error);
        await this.updateJob(id, { status: "failed", error, log, finished_at: nowIso() });
        return;
      }

      const canonical = await promoteReport(repo.id, generated, "comparison.html");
      const generatedDigest = await findGeneratedComparisonDigest(repo.id);
      if (!generatedDigest) {
        const error = "对比流水线完成但未发现摘要数据";
        await this.setRepoStatus(repo.id, "failed", error);
        await this.updateJob(id, { status: "failed", error, log, finished_at: nowIso() });
        return;
      }
      await promoteReport(repo.id, generatedDigest, "comparison.digest.json");
      await registerReport(this.db, repo.id, canonical, "pipeline", "comparison");
      existing = await syncExistingReports(this.db, repo.id);
    }

    const needsLocalRepo = missingKinds.includes("description") || missingKinds.includes("development");
    let clonedRepoPath = needsLocalRepo ? await findClonedRepoPath(repo.id) : null;
    if (needsLocalRepo && !clonedRepoPath) {
      const reposDir = path.join(reportDir(repo.id), "_repos");
      await fs.mkdir(reposDir, { recursive: true });
      const repoName = safeRepoDirectoryName(repo.repo_url, repo.id);
      let cloneTarget = path.join(reposDir, repoName);
      if (await pathExists(cloneTarget)) {
        cloneTarget = path.join(reposDir, `${repoName}_${Date.now()}`);
      }
      const cloneArgs = [
        "clone",
        "-c",
        "core.protectNTFS=false",
        "--depth",
        "200",
        repo.repo_url,
        cloneTarget
      ];
      const result = await this.runProcess(id, "clone", "git", cloneArgs, log, commandLines);
      log = result.log;
      commandLines = result.commandLines;

      if (!this.db.get("SELECT id FROM jobs WHERE id = ?", [id])) return;

      if (result.exitCode !== 0) {
        const error = failureMessage(log, result.exitCode);
        await this.setRepoStatus(repo.id, "failed", error);
        await this.updateJob(id, { status: "failed", error, log, finished_at: nowIso() });
        return;
      }
      clonedRepoPath = cloneTarget;
    }

    if (missingKinds.includes("description")) {
      const descriptionPath = path.join(reportDir(repo.id), "description.html");
      const descriptionArgs = [
        "-X",
        "utf8",
        "agent.py",
        "--repo-path",
        clonedRepoPath,
        "--output",
        descriptionPath
      ];
      const result = await this.runProcess(id, "description", python, descriptionArgs, log, commandLines);
      log = result.log;
      commandLines = result.commandLines;

      if (!this.db.get("SELECT id FROM jobs WHERE id = ?", [id])) return;

      if (result.exitCode !== 0 || !(await pathExists(digestPaths.description))) {
        const error = result.exitCode !== 0
          ? failureMessage(log, result.exitCode)
          : "作品描述报告完成但未发现摘要数据";
        await this.setRepoStatus(repo.id, "failed", error);
        await this.updateJob(id, { status: "failed", error, log, finished_at: nowIso() });
        return;
      }

      await registerReport(this.db, repo.id, descriptionPath, "pipeline", "description");
      existing = await syncExistingReports(this.db, repo.id);
    }

    if (missingKinds.includes("development")) {
      const developmentPath = path.join(reportDir(repo.id), "development.html");
      const developmentArgs = [
        "-X",
        "utf8",
        "-m",
        "finals",
        "development",
        "--repo",
        clonedRepoPath,
        "--repo-id",
        repo.id,
        "--output",
        developmentPath
      ];
      const minimumCommits = String(process.env.FINALS_MIN_COMMITS || "").trim();
      if (minimumCommits) {
        developmentArgs.push("--min-commits", minimumCommits);
      }
      const result = await this.runProcess(id, "development", python, developmentArgs, log, commandLines);
      log = result.log;
      commandLines = result.commandLines;

      if (!this.db.get("SELECT id FROM jobs WHERE id = ?", [id])) return;

      if (result.exitCode !== 0 || !(await pathExists(digestPaths.development))) {
        const error = result.exitCode !== 0
          ? failureMessage(log, result.exitCode)
          : "开发过程报告完成但未发现摘要数据";
        await this.setRepoStatus(repo.id, "failed", error);
        await this.updateJob(id, { status: "failed", error, log, finished_at: nowIso() });
        return;
      }

      await registerReport(this.db, repo.id, developmentPath, "pipeline", "development");
      existing = await syncExistingReports(this.db, repo.id);
    }

    if (missingKinds.includes("summary")) {
      const unavailable = [];
      for (const [kind, digestPath] of Object.entries(digestPaths)) {
        if (!(await pathExists(digestPath))) unavailable.push(kind);
      }
      if (unavailable.length) {
        const error = `无法生成摘要，缺少上游数据：${unavailable.join(", ")}`;
        await this.setRepoStatus(repo.id, "failed", error);
        await this.updateJob(id, { status: "failed", error, log, finished_at: nowIso() });
        return;
      }

      const summaryPath = path.join(reportDir(repo.id), "summary.pdf");
      const summaryArgs = [
        "-X",
        "utf8",
        "-m",
        "finals",
        "summary",
        "--description-digest",
        digestPaths.description,
        "--development-digest",
        digestPaths.development,
        "--comparison-digest",
        digestPaths.comparison,
        "--repo-id",
        repo.id,
        "--output",
        summaryPath
      ];
      const result = await this.runProcess(id, "summary", python, summaryArgs, log, commandLines);
      log = result.log;
      commandLines = result.commandLines;

      if (!this.db.get("SELECT id FROM jobs WHERE id = ?", [id])) return;

      if (result.exitCode !== 0 || !(await pathExists(summaryPath))) {
        const error = result.exitCode !== 0
          ? failureMessage(log, result.exitCode)
          : "摘要生成完成但未发现 PDF";
        await this.setRepoStatus(repo.id, "failed", error);
        await this.updateJob(id, { status: "failed", error, log, finished_at: nowIso() });
        return;
      }

      await registerReport(this.db, repo.id, summaryPath, "pipeline", "summary");
      existing = await syncExistingReports(this.db, repo.id);
    }

    if (!hasReportKinds(existing, requestedKinds)) {
      const error = "任务完成但所选报告没有全部登记成功";
      await this.setRepoStatus(repo.id, "failed", error);
      await this.updateJob(id, { status: "failed", error, log, finished_at: nowIso() });
      return;
    }

    await this.updateJob(id, { status: "succeeded", log, finished_at: nowIso() });
  }
}
