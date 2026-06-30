import fs from "node:fs/promises";
import path from "node:path";
import initSqlJs from "sql.js";
import { DATA_DIR, DB_PATH, FRONTEND_ROOT } from "./config.js";

function nowIso() {
  return new Date().toISOString();
}

export class AppDatabase {
  constructor() {
    this.SQL = null;
    this.db = null;
  }

  async init() {
    await fs.mkdir(DATA_DIR, { recursive: true });
    this.SQL = await initSqlJs({
      locateFile: (file) => path.join(FRONTEND_ROOT, "node_modules", "sql.js", "dist", file)
    });
    try {
      const bytes = await fs.readFile(DB_PATH);
      this.db = new this.SQL.Database(bytes);
    } catch {
      this.db = new this.SQL.Database();
    }
    this.migrate();
    await this.save();
  }

  migrate() {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS repositories (
        id TEXT PRIMARY KEY,
        year TEXT NOT NULL DEFAULT '',
        event TEXT NOT NULL DEFAULT '',
        sub_event TEXT NOT NULL DEFAULT '',
        school TEXT NOT NULL DEFAULT '',
        team_name TEXT NOT NULL DEFAULT '',
        repo_url TEXT NOT NULL UNIQUE,
        normalized_url TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'pending',
        report_count INTEGER NOT NULL DEFAULT 0,
        current_report_url TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_id TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'comparison',
        report_path TEXT NOT NULL,
        report_url TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        content_text TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(repo_id) REFERENCES repositories(id)
      );

      CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_repo_path
        ON reports(repo_id, report_path);

      CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        repo_id TEXT NOT NULL,
        type TEXT NOT NULL,
        status TEXT NOT NULL,
        command TEXT NOT NULL DEFAULT '',
        log TEXT NOT NULL DEFAULT '',
        error TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        FOREIGN KEY(repo_id) REFERENCES repositories(id)
      );
    `);
  }

  async save() {
    const data = this.db.export();
    await fs.writeFile(DB_PATH, Buffer.from(data));
  }

  query(sql, params = []) {
    const stmt = this.db.prepare(sql);
    const rows = [];
    try {
      stmt.bind(params);
      while (stmt.step()) rows.push(stmt.getAsObject());
    } finally {
      stmt.free();
    }
    return rows;
  }

  get(sql, params = []) {
    return this.query(sql, params)[0] || null;
  }

  async run(sql, params = []) {
    this.db.run(sql, params);
    await this.save();
  }

  async transaction(fn) {
    this.db.exec("BEGIN");
    try {
      const result = await fn();
      this.db.exec("COMMIT");
      await this.save();
      return result;
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  async upsertRepository(row) {
    const existing = this.get(
      "SELECT * FROM repositories WHERE normalized_url = ?",
      [row.normalized_url]
    );
    const createdAt = existing?.created_at || nowIso();
    const status = existing?.status || "pending";
    this.db.run(
      `INSERT INTO repositories (
        id, year, event, sub_event, school, team_name, repo_url, normalized_url,
        status, report_count, current_report_url, last_error, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(normalized_url) DO UPDATE SET
        year = excluded.year,
        event = excluded.event,
        sub_event = excluded.sub_event,
        school = excluded.school,
        team_name = excluded.team_name,
        repo_url = excluded.repo_url,
        updated_at = excluded.updated_at`,
      [
        existing?.id || row.id,
        row.year,
        row.event,
        row.sub_event,
        row.school,
        row.team_name,
        row.repo_url,
        row.normalized_url,
        status,
        existing?.report_count || 0,
        existing?.current_report_url || null,
        existing?.last_error || null,
        createdAt,
        nowIso()
      ]
    );
    return this.get("SELECT * FROM repositories WHERE normalized_url = ?", [row.normalized_url]);
  }
}

export const db = new AppDatabase();
export { nowIso };
