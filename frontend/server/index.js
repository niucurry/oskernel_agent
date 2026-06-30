import path from "node:path";
import fs from "node:fs";
import express from "express";
import cors from "cors";
import multer from "multer";
import { db } from "./db.js";
import { PORT, REPORTS_DIR, FRONTEND_ROOT } from "./config.js";
import { importRepositories } from "./importer.js";
import { PipelineQueue } from "./pipeline.js";
import { normalizeReportKinds, syncExistingReports } from "./reportFiles.js";

const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 20 * 1024 * 1024 } });
const app = express();

function asyncRoute(handler) {
  return (req, res, next) => Promise.resolve(handler(req, res, next)).catch(next);
}

function parseLimit(value, fallback = 200) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(1, Math.min(1000, Math.floor(n)));
}

function repositoryWhere(query) {
  const clauses = [];
  const params = [];
  if (query.year) {
    clauses.push("r.year = ?");
    params.push(String(query.year));
  }
  if (query.status) {
    clauses.push("r.status = ?");
    params.push(String(query.status));
  }
  if (query.q) {
    const like = `%${String(query.q).toLowerCase()}%`;
    clauses.push(`(
      lower(r.id) LIKE ? OR lower(r.year) LIKE ? OR lower(r.school) LIKE ?
      OR lower(r.team_name) LIKE ? OR lower(r.repo_url) LIKE ?
      OR EXISTS (
        SELECT 1 FROM reports rp
        WHERE rp.repo_id = r.id AND lower(rp.content_text) LIKE ?
      )
    )`);
    params.push(like, like, like, like, like, like);
  }
  return {
    sql: clauses.length ? `WHERE ${clauses.join(" AND ")}` : "",
    params
  };
}

await db.init();
const queue = new PipelineQueue(db);
await queue.recoverInterruptedJobs();

app.use(cors());
app.use(express.json({ limit: "2mb" }));
app.use("/reports", express.static(REPORTS_DIR));

app.get("/api/health", (req, res) => {
  res.json({ ok: true });
});

app.get("/api/summary", (req, res) => {
  const total = db.get("SELECT COUNT(*) AS n FROM repositories")?.n || 0;
  const ready = db.get("SELECT COUNT(*) AS n FROM repositories WHERE status = 'ready'")?.n || 0;
  const generating = db.get("SELECT COUNT(*) AS n FROM repositories WHERE status = 'generating'")?.n || 0;
  const failed = db.get("SELECT COUNT(*) AS n FROM repositories WHERE status = 'failed'")?.n || 0;
  const pending = total - ready - generating - failed;
  const years = db.query("SELECT year, COUNT(*) AS count FROM repositories GROUP BY year ORDER BY year");
  res.json({ total, ready, generating, failed, pending, years });
});

app.get("/api/repositories", (req, res) => {
  const limit = parseLimit(req.query.limit);
  const offset = Math.max(0, Number(req.query.offset || 0) || 0);
  const where = repositoryWhere(req.query);
  const rows = db.query(
    `SELECT r.*,
      (SELECT MAX(created_at) FROM reports rp WHERE rp.repo_id = r.id) AS latest_report_at
     FROM repositories r
     ${where.sql}
     ORDER BY r.year DESC, r.school, r.team_name
     LIMIT ? OFFSET ?`,
    [...where.params, limit, offset]
  );
  const total = db.get(
    `SELECT COUNT(*) AS n FROM repositories r ${where.sql}`,
    where.params
  )?.n || 0;
  res.json({ rows, total, limit, offset });
});

app.get("/api/repositories/:id", asyncRoute(async (req, res) => {
  await syncExistingReports(db, req.params.id);
  const repo = db.get("SELECT * FROM repositories WHERE id = ?", [req.params.id]);
  if (!repo) return res.status(404).json({ error: "作品不存在" });
  const reports = db.query(
    `SELECT * FROM reports
     WHERE repo_id = ?
     ORDER BY CASE kind WHEN 'comparison' THEN 0 WHEN 'description' THEN 1 WHEN 'ai_detect' THEN 2 ELSE 3 END,
              created_at DESC`,
    [req.params.id]
  );
  res.json({ ...repo, reports });
}));

app.get("/api/repositories/:id/reports", (req, res) => {
  const reports = db.query(
    `SELECT * FROM reports
     WHERE repo_id = ?
     ORDER BY CASE kind WHEN 'comparison' THEN 0 WHEN 'description' THEN 1 WHEN 'ai_detect' THEN 2 ELSE 3 END,
              created_at DESC`,
    [req.params.id]
  );
  res.json({ rows: reports });
});

app.post("/api/import/xlsx", upload.single("file"), asyncRoute(async (req, res) => {
  if (!req.file) return res.status(400).json({ error: "缺少 xlsx 文件" });
  const result = await importRepositories(db, req.file.buffer);
  res.json(result);
}));

app.post("/api/generate", asyncRoute(async (req, res) => {
  const body = req.body || {};
  const reportKinds = normalizeReportKinds(body.reportTypes);
  let ids = Array.isArray(body.ids) ? body.ids : [];
  if (!ids.length) {
    let sql = "SELECT id FROM repositories r";
    const params = [];
    if (body.missingOnly !== false) {
      const missing = reportKinds
        .map(() => "NOT EXISTS (SELECT 1 FROM reports rp WHERE rp.repo_id = r.id AND rp.kind = ?)")
        .join(" OR ");
      sql += ` WHERE ${missing}`;
      params.push(...reportKinds);
    }
    ids = db.query(`${sql} ORDER BY year DESC, school, team_name`, params).map((row) => row.id);
  }
  const jobs = [];
  for (const id of ids) jobs.push(await queue.enqueue(id, reportKinds));
  res.json({ queued: jobs.length, jobs });
}));

app.post("/api/repositories/:id/generate", asyncRoute(async (req, res) => {
  const job = await queue.enqueue(req.params.id, normalizeReportKinds(req.body?.reportTypes));
  res.json(job);
}));

app.get("/api/jobs", (req, res) => {
  const rows = db.query(
    `SELECT j.*, r.team_name, r.school, r.year
     FROM jobs j
     LEFT JOIN repositories r ON r.id = j.repo_id
     ORDER BY j.created_at DESC
     LIMIT 100`
  );
  res.json({ rows });
});

app.get("/api/jobs/:id", (req, res) => {
  const job = db.get("SELECT * FROM jobs WHERE id = ?", [req.params.id]);
  if (!job) return res.status(404).json({ error: "任务不存在" });
  res.json(job);
});

app.delete("/api/jobs/:id", asyncRoute(async (req, res) => {
  const result = await queue.deleteJob(req.params.id);
  if (!result.deleted) return res.status(404).json({ error: "任务不存在" });
  res.json(result);
}));

app.post("/api/jobs/clear", asyncRoute(async (req, res) => {
  const statuses = Array.isArray(req.body?.statuses) ? req.body.statuses : ["queued"];
  const result = await queue.clearJobs(statuses);
  res.json(result);
}));

const distDir = path.join(FRONTEND_ROOT, "dist");
if (fs.existsSync(distDir)) {
  app.use(express.static(distDir));
  app.get("*", (req, res) => res.sendFile(path.join(distDir, "index.html")));
}

app.use((error, req, res, next) => {
  console.error(error);
  res.status(500).json({ error: error.message || "服务异常" });
});

app.listen(PORT, "127.0.0.1", () => {
  console.log(`Report API listening on http://127.0.0.1:${PORT}`);
});
