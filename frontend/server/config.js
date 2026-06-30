import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const SERVER_DIR = path.dirname(fileURLToPath(import.meta.url));
export const FRONTEND_ROOT = path.resolve(SERVER_DIR, "..");
export const PROJECT_ROOT = path.resolve(FRONTEND_ROOT, "..");
export const DATA_DIR = path.join(FRONTEND_ROOT, "data");
export const REPORTS_DIR = path.join(FRONTEND_ROOT, "reports");
export const DB_PATH = path.join(DATA_DIR, "app.sqlite");
export const PORT = Number(process.env.FRONTEND_API_PORT || 3130);

export function findPython() {
  if (process.env.PYTHON_BIN) return process.env.PYTHON_BIN;
  const candidates = [
    path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe"),
    path.join(PROJECT_ROOT, ".venv", "bin", "python"),
    "python"
  ];
  return candidates.find((candidate) => candidate === "python" || fs.existsSync(candidate));
}
