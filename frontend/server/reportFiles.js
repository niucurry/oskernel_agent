import fs from "node:fs/promises";
import path from "node:path";
import { REPORTS_DIR, FRONTEND_ROOT } from "./config.js";
import { nowIso } from "./db.js";

// AI 生成代码检测已并入对比报告（第六章），不再作为独立报告类型。
export const REPORT_KINDS = ["comparison", "description"];

const REPORT_KIND_ORDER = new Map(REPORT_KINDS.map((kind, index) => [kind, index]));

export function reportDir(repoId) {
  return path.join(REPORTS_DIR, repoId);
}

export function relativeToFrontend(absPath) {
  return path.relative(FRONTEND_ROOT, absPath).replaceAll(path.sep, "/");
}

export function reportUrl(repoId, reportPath) {
  const encoded = reportPath
    .split(/[\\/]+/)
    .filter(Boolean)
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `/reports/${encodeURIComponent(repoId)}/${encoded}`;
}

function reportKindFromName(fileName) {
  const name = fileName.toLowerCase();
  if (name.includes("description") || name.includes("tree")) return "description";
  return "comparison";
}

export function normalizeReportKinds(kinds) {
  const values = Array.isArray(kinds) ? kinds : [];
  const normalized = values
    .map((kind) => String(kind || "").trim())
    .filter((kind) => REPORT_KINDS.includes(kind));
  return normalized.length ? [...new Set(normalized)] : [...REPORT_KINDS];
}

export function hasReportKinds(reports, kinds) {
  const available = new Set(reports.map((report) => report.kind));
  return kinds.every((kind) => available.has(kind));
}

function preferredReport(reports) {
  return [...reports].sort((a, b) => {
    const ak = REPORT_KIND_ORDER.get(a.kind) ?? 99;
    const bk = REPORT_KIND_ORDER.get(b.kind) ?? 99;
    if (ak !== bk) return ak - bk;
    return String(b.created_at || "").localeCompare(String(a.created_at || ""));
  })[0] || null;
}

function htmlText(html) {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ")
    .trim();
}

function htmlTitle(html, fallback) {
  const title = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1]
    || html.match(/<h1[^>]*>([\s\S]*?)<\/h1>/i)?.[1]
    || fallback;
  return htmlText(title).slice(0, 160);
}

export async function findExistingReport(repoId) {
  return preferredReport(await findExistingReports(repoId));
}

export async function findExistingReports(repoId) {
  const dir = reportDir(repoId);
  try {
    const entries = await fs.readdir(dir, { withFileTypes: true });
    const reports = entries
      .filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith(".html"))
      .map((entry) => ({
        fileName: entry.name,
        absPath: path.join(dir, entry.name),
        kind: reportKindFromName(entry.name)
      }));

    return reports;
  } catch {
    return [];
  }
}

export async function promoteReport(repoId, sourcePath, targetName = "comparison.html") {
  await fs.mkdir(reportDir(repoId), { recursive: true });
  const target = path.join(reportDir(repoId), targetName);
  if (path.resolve(sourcePath) !== path.resolve(target)) {
    await fs.copyFile(sourcePath, target);
  }
  return target;
}

export async function registerReport(database, repoId, absPath, source = "local", kind = null) {
  const html = await fs.readFile(absPath, "utf8");
  const fileName = path.basename(absPath);
  const relPath = relativeToFrontend(absPath);
  const relToRepo = path.relative(reportDir(repoId), absPath).replaceAll(path.sep, "/");
  const url = reportUrl(repoId, relToRepo);
  const title = htmlTitle(html, `${repoId} report`);
  const text = htmlText(html);
  const createdAt = nowIso();
  const reportKind = kind || reportKindFromName(fileName);

  database.db.run(
    `INSERT INTO reports (repo_id, kind, report_path, report_url, title, content_text, source, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(repo_id, report_path) DO UPDATE SET
       kind = excluded.kind,
       report_url = excluded.report_url,
       title = excluded.title,
       content_text = excluded.content_text,
       source = excluded.source,
       created_at = excluded.created_at`,
    [repoId, reportKind, relPath, url, title, text, source, createdAt]
  );
  const count = database.get("SELECT COUNT(*) AS n FROM reports WHERE repo_id = ?", [repoId])?.n || 0;
  const current = database.get(
    `SELECT report_url FROM reports
     WHERE repo_id = ?
     ORDER BY CASE kind WHEN 'comparison' THEN 0 WHEN 'description' THEN 1 ELSE 2 END, created_at DESC
     LIMIT 1`,
    [repoId]
  );
  database.db.run(
    `UPDATE repositories
     SET status = 'ready', report_count = ?, current_report_url = ?, last_error = NULL, updated_at = ?
     WHERE id = ?`,
    [count, current?.report_url || url, nowIso(), repoId]
  );
  await database.save();
  return { report_path: relPath, report_url: url, title, kind: reportKind };
}

export async function syncExistingReport(database, repoId) {
  const synced = await syncExistingReports(database, repoId);
  return preferredReport(synced);
}

export async function syncExistingReports(database, repoId) {
  const found = await findExistingReports(repoId);
  const synced = [];
  for (const report of found) {
    synced.push(await registerReport(database, repoId, report.absPath, "existing", report.kind));
  }
  return synced;
}
