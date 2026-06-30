import crypto from "node:crypto";
import xlsx from "xlsx";
import { syncExistingReport } from "./reportFiles.js";

const COLUMN_MAP = {
  year: "年份",
  event: "赛事",
  sub_event: "子赛事",
  school: "学校",
  team_name: "队伍名称",
  repo_url: "仓库地址"
};

function clean(value) {
  return String(value ?? "").trim();
}

export function normalizeRepoUrl(raw) {
  const value = clean(raw).replace(/\/+$/, "").replace(/\.git$/i, "");
  try {
    const url = new URL(value);
    url.hash = "";
    url.search = "";
    url.protocol = url.protocol.toLowerCase();
    url.hostname = url.hostname.toLowerCase();
    url.pathname = url.pathname.replace(/\/+$/, "").replace(/\.git$/i, "");
    return url.toString().replace(/\/+$/, "").toLowerCase();
  } catch {
    return value.toLowerCase();
  }
}

function hashId(normalizedUrl, length = 8) {
  const hash = crypto.createHash("sha1").update(normalizedUrl).digest("hex").slice(0, length);
  return `repo_${hash}`;
}

function stableRepoId(database, normalizedUrl) {
  const existing = database.get("SELECT id FROM repositories WHERE normalized_url = ?", [normalizedUrl]);
  if (existing) return existing.id;
  for (const length of [8, 12, 16, 20]) {
    const id = hashId(normalizedUrl, length);
    const collision = database.get("SELECT normalized_url FROM repositories WHERE id = ?", [id]);
    if (!collision || collision.normalized_url === normalizedUrl) return id;
  }
  return hashId(normalizedUrl, 40);
}

export function parseWorkbook(buffer) {
  const workbook = xlsx.read(buffer, { type: "buffer" });
  const sheetName = workbook.SheetNames[0];
  if (!sheetName) throw new Error("xlsx 文件没有可读取的工作表");
  const rows = xlsx.utils.sheet_to_json(workbook.Sheets[sheetName], { defval: "" });
  const required = Object.values(COLUMN_MAP);
  const columns = Object.keys(rows[0] || {});
  const missing = required.filter((name) => !columns.includes(name));
  if (missing.length) throw new Error(`xlsx 缺少必要列：${missing.join(", ")}`);
  return rows
    .map((row) => ({
      year: clean(row[COLUMN_MAP.year]),
      event: clean(row[COLUMN_MAP.event]),
      sub_event: clean(row[COLUMN_MAP.sub_event]),
      school: clean(row[COLUMN_MAP.school]),
      team_name: clean(row[COLUMN_MAP.team_name]),
      repo_url: clean(row[COLUMN_MAP.repo_url])
    }))
    .filter((row) => row.repo_url);
}

export async function importRepositories(database, buffer) {
  const parsed = parseWorkbook(buffer);
  let inserted = 0;
  let updated = 0;
  let ready = 0;
  const imported = [];

  await database.transaction(async () => {
    for (const row of parsed) {
      const normalizedUrl = normalizeRepoUrl(row.repo_url);
      const before = database.get("SELECT id FROM repositories WHERE normalized_url = ?", [normalizedUrl]);
      const repo = await database.upsertRepository({
        ...row,
        id: stableRepoId(database, normalizedUrl),
        normalized_url: normalizedUrl
      });
      if (before) updated += 1;
      else inserted += 1;
      imported.push(repo);
    }
  });

  for (const repo of imported) {
    const report = await syncExistingReport(database, repo.id);
    if (report) ready += 1;
  }

  return {
    total: parsed.length,
    inserted,
    updated,
    ready,
    repositories: imported
  };
}
