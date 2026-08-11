import fs from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { findPython, PROJECT_ROOT } from "./config.js";
import { nowIso } from "./db.js";
import {
  cleanupReportDirectory,
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

function pipelineEnv() {
  return {
    ...process.env,
    PYTHONUTF8: "1",
    AGENT_SUBSYS_CONCURRENCY: process.env.AGENT_SUBSYS_CONCURRENCY || "4",
    AGENT_LLM_CONCURRENCY: process.env.AGENT_LLM_CONCURRENCY || "4",
  };
}

function extractFailureReason(log) {
  const lines = String(log || "").split(/\r?\n/);
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (!line) continue;
    if (/^ModuleNotFoundError:\s*(.+)/.test(line)) {
      return `缺少 Python 依赖：${RegExp.$1}`;
    }
    if (/^(ImportError|FileNotFoundError|PermissionError):\s*(.+)/.test(line)) {
      return line;
    }
    if (/^(fatal|error|remote):\s+/i.test(line)) return line;
    if (/^[A-Za-z_]\w*(Error|Exception):/.test(line)) return line;
  }
  return "进程异常退出，请查看日志详情";
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

export class PipelineQueue {
  constructor(database) {
    this.db = database;
    this.queue = [];
    this.running = false;
    this.activeChildren = new Map();
    this.jobReportKinds = new Map();
    this.cancelledJobs = new Set();
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
    try {
      while (this.queue.length) {
        const id = this.queue.shift();
        const current = this.db.get("SELECT * FROM jobs WHERE id = ?", [id]);
        if (!current || current.status !== "queued") continue;
        await this.runJob(id);
      }
    } finally {
      this.running = false;
    }
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
      this.cancelledJobs.add(id);
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

    const failedJobs = this.db.query(
      "SELECT id, repo_id, error FROM jobs WHERE status = 'failed' AND (error IS NULL OR error = '')"
    );
    for (const job of failedJobs) {
      const genericError = "流水线进程异常退出，请检查日志并重新生成。";
      this.db.db.run("UPDATE jobs SET error = ? WHERE id = ?", [genericError, job.id]);
      this.db.db.run(
        `UPDATE repositories
         SET last_error = ?, updated_at = ?
         WHERE id = ? AND status = 'failed' AND (last_error IS NULL OR last_error = '')`,
        [genericError, now, job.repo_id]
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
    try {
      await this.runJobImplementation(id, job);
    } finally {
      this.cancelledJobs.delete(id);
      await cleanupReportDirectory(job.repo_id);
    }
  }

  async runJobImplementation(id, job) {
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
      status: "running", command: "", started_at: nowIso(),
      log: `请求生成报告：${requestedKinds.join(", ")}\n`
    });

    const args = [
      "-X", "utf8", "-m", "oskernel_agent.report_jobs",
      "--repo", repo.repo_url,
      "--repo-id", repo.id,
      "--output-dir", reportDir(repo.id),
      "--kinds", requestedKinds.join(","),
    ];
    if (process.env.FINALS_MIN_COMMITS) {
      args.push("--min-commits", process.env.FINALS_MIN_COMMITS);
    }

    const { log } = await this.runProcess(id, "report_jobs", python, args, "", []);

    if (this.cancelledJobs.has(id)) return;

    if (!this.db.get("SELECT id FROM jobs WHERE id = ?", [id])) return;

    const jsonMatch = log.match(/\{(?:"repo_id"|"kinds"|"started_at"|"finished_at")[\s\S]*\}/);
    if (!jsonMatch) {
      const reason = extractFailureReason(log);
      const error = `report_jobs 未输出有效 JSON（${reason}）`;
      await this.setRepoStatus(repo.id, "failed", error);
      await this.updateJob(id, { status: "failed", error, log, finished_at: nowIso() });
      return;
    }

    let result;
    try {
      result = JSON.parse(jsonMatch[0]);
    } catch {
      const error = "无法解析 report_jobs 输出，JSON 格式异常";
      await this.setRepoStatus(repo.id, "failed", error);
      await this.updateJob(id, { status: "failed", error, log, finished_at: nowIso() });
      return;
    }

    const kindsOutput = result.kinds || {};
    for (const [kind, kr] of Object.entries(kindsOutput)) {
      if (kr.status === "ok" && kr.html_path) {
        await registerReport(this.db, repo.id,
          path.join(reportDir(repo.id), kr.html_path), "pipeline", kind);
      }
    }

    const existing = await syncExistingReports(this.db, repo.id);
    const allOk = requestedKinds.every(k => kindsOutput[k]?.status === "ok");

    if (!allOk) {
      const failures = requestedKinds.filter(k => kindsOutput[k]?.status !== "ok");
      const errors = failures.map(k => `${k}: ${kindsOutput[k]?.error || "未知错误"}`).join("; ");
      await this.setRepoStatus(repo.id, "failed", errors);
      await this.updateJob(id, { status: "failed", error: errors, log, finished_at: nowIso() });
      return;
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
