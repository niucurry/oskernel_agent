async function jsonResponse(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

export async function fetchSummary() {
  return jsonResponse(await fetch("/api/summary"));
}

export async function fetchRepositories(params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") query.set(key, value);
  });
  return jsonResponse(await fetch(`/api/repositories?${query.toString()}`));
}

export async function fetchRepository(id) {
  return jsonResponse(await fetch(`/api/repositories/${encodeURIComponent(id)}`));
}

export async function fetchJobs() {
  return jsonResponse(await fetch("/api/jobs"));
}

export async function deleteJob(id) {
  return jsonResponse(await fetch(`/api/jobs/${encodeURIComponent(id)}`, {
    method: "DELETE"
  }));
}

export async function clearQueuedJobs() {
  return jsonResponse(await fetch("/api/jobs/clear", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ statuses: ["queued", "cancelled"] })
  }));
}

export async function importXlsx(file) {
  const body = new FormData();
  body.append("file", file);
  return jsonResponse(await fetch("/api/import/xlsx", { method: "POST", body }));
}

export async function generateMissing(reportTypes = []) {
  return jsonResponse(await fetch("/api/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ missingOnly: true, reportTypes })
  }));
}

export async function generateRepository(id, reportTypes = []) {
  return jsonResponse(await fetch(`/api/repositories/${encodeURIComponent(id)}/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reportTypes })
  }));
}
