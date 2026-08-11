<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from "vue";
import {
  Database,
  ExternalLink,
  FileText,
  Loader2,
  Play,
  RefreshCw,
  Search,
  Trash2,
  Upload
} from "lucide-vue-next";
import {
  clearQueuedJobs,
  deleteJob,
  fetchJobs,
  fetchRepositories,
  fetchRepository,
  fetchSummary,
  generateMissing,
  generateRepository,
  importXlsx
} from "./lib/api.js";

const filters = reactive({ q: "", year: "", status: "" });
const summary = ref({ total: 0, ready: 0, pending: 0, generating: 0, failed: 0, years: [] });
const repositories = ref([]);
const jobs = ref([]);
const selected = ref(null);
const loading = ref(false);
const importMessage = ref("");
const actionMessage = ref("");
const errorMessage = ref("");
const fileInput = ref(null);
const deletingJobId = ref("");
const clearingQueue = ref(false);
const detailPane = ref(null);

const statusLabels = {
  pending: "待生成",
  ready: "已生成",
  generating: "生成中",
  failed: "失败",
  queued: "排队中",
  running: "运行中",
  succeeded: "成功",
  skipped: "已跳过",
  cancelled: "已取消"
};

const reportOptions = [
  { kind: "summary", label: "一页摘要" },
  { kind: "description", label: "作品描述" },
  { kind: "development", label: "开发过程" },
  { kind: "comparison", label: "对比分析" }
];

const reportKindLabels = Object.fromEntries(reportOptions.map((option) => [option.kind, option.label]));

const years = computed(() => summary.value.years?.map((item) => item.year).filter(Boolean) || []);
const selectedReportTypes = ref(reportOptions.map((option) => option.kind));
const activeReportKind = ref("summary");
const selectedReports = computed(() => selected.value?.reports || []);
const selectedReportChoices = computed(() => {
  const seen = new Set();
  const order = new Map(reportOptions.map((option, index) => [option.kind, index]));
  return [...selectedReports.value]
    .sort((a, b) => (order.get(a.kind) ?? 99) - (order.get(b.kind) ?? 99))
    .filter((report) => {
      if (seen.has(report.kind)) return false;
      seen.add(report.kind);
      return true;
    });
});
const activeReport = computed(() => (
  selectedReportChoices.value.find((report) => report.kind === activeReportKind.value)
  || selectedReportChoices.value[0]
  || null
));
const selectedReportUrl = computed(() => activeReport.value?.report_url || selected.value?.current_report_url || "");
const clearableQueueCount = computed(() => jobs.value.filter((job) => ["queued", "cancelled"].includes(job.status)).length);
const canGenerate = computed(() => selectedReportTypes.value.length > 0);
const selectedReportTypeText = computed(() => selectedReportTypes.value.map((kind) => reportKindLabels[kind]).join("、"));

function statusClass(status) {
  return {
    ready: "is-ready",
    succeeded: "is-ready",
    skipped: "is-ready",
    generating: "is-running",
    running: "is-running",
    queued: "is-pending",
    failed: "is-failed",
    cancelled: "is-cancelled",
    pending: "is-pending"
  }[status] || "is-pending";
}

function statusText(status) {
  return statusLabels[status] || status;
}

function reportKindText(kind) {
  return reportKindLabels[kind] || kind;
}

function setActiveReportKind(kind) {
  activeReportKind.value = kind;
}

function syncActiveReportKind(reports = []) {
  const available = reports.map((report) => report.kind);
  activeReportKind.value = available.includes("summary") ? "summary" : (available[0] || "summary");
}

function jobLogTail(log) {
  const value = String(log || "").trim();
  if (!value) return "";
  return value.length > 2400 ? value.slice(-2400) : value;
}

async function loadAll() {
  loading.value = true;
  errorMessage.value = "";
  try {
    const [summaryData, repoData, jobData] = await Promise.all([
      fetchSummary(),
      fetchRepositories(filters),
      fetchJobs()
    ]);
    summary.value = summaryData;
    repositories.value = repoData.rows;
    jobs.value = jobData.rows;
    if (selected.value) {
      const latest = repositories.value.find((repo) => repo.id === selected.value.id);
      if (latest) {
        selected.value = { ...selected.value, ...latest };
        if (latest.status === "ready" && latest.report_count !== (selected.value.reports || []).length) {
          fetchRepository(selected.value.id).then((repo) => {
            if (selected.value && selected.value.id === repo.id) {
              selected.value = { ...selected.value, ...repo };
              syncActiveReportKind(repo.reports || []);
            }
          }).catch(() => {});
        }
      }
    }
  } catch (error) {
    errorMessage.value = error.message;
  } finally {
    loading.value = false;
  }
}

async function selectRepo(repo) {
  errorMessage.value = "";
  try {
    selected.value = await fetchRepository(repo.id);
    syncActiveReportKind(selected.value.reports || []);
    await nextTick();
    if (window.matchMedia("(max-width: 768px)").matches) {
      detailPane.value?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } catch (error) {
    errorMessage.value = error.message;
  }
}

async function onImport(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  importMessage.value = "";
  errorMessage.value = "";
  try {
    const result = await importXlsx(file);
    importMessage.value = `导入 ${result.total} 条，新增 ${result.inserted} 条，更新 ${result.updated} 条，已识别本地报告 ${result.ready} 条`;
    await loadAll();
  } catch (error) {
    errorMessage.value = error.message;
  } finally {
    event.target.value = "";
  }
}

async function onGenerateMissing() {
  actionMessage.value = "";
  errorMessage.value = "";
  if (!canGenerate.value) {
    errorMessage.value = "请至少选择一种报告";
    return;
  }
  try {
    const result = await generateMissing(selectedReportTypes.value);
    actionMessage.value = `已加入队列 ${result.queued} 个任务：${selectedReportTypeText.value}`;
    await loadAll();
  } catch (error) {
    errorMessage.value = error.message;
  }
}

async function onGenerateOne(repo) {
  actionMessage.value = "";
  errorMessage.value = "";
  if (!canGenerate.value) {
    errorMessage.value = "请至少选择一种报告";
    return;
  }
  try {
    const job = await generateRepository(repo.id, selectedReportTypes.value);
    actionMessage.value = job.status === "skipped" ? "本地报告已存在" : `已加入队列：${job.id}`;
    await loadAll();
  } catch (error) {
    errorMessage.value = error.message;
  }
}

async function onDeleteJob(job) {
  actionMessage.value = "";
  errorMessage.value = "";
  deletingJobId.value = job.id;
  try {
    const result = await deleteJob(job.id);
    actionMessage.value = result.cancelled ? `已取消并删除任务：${job.id}` : `已删除任务：${job.id}`;
    await loadAll();
  } catch (error) {
    errorMessage.value = error.message;
  } finally {
    deletingJobId.value = "";
  }
}

async function onClearQueuedJobs() {
  actionMessage.value = "";
  errorMessage.value = "";
  clearingQueue.value = true;
  try {
    const result = await clearQueuedJobs();
    actionMessage.value = `已清空等待队列 ${result.deleted} 个任务`;
    await loadAll();
  } catch (error) {
    errorMessage.value = error.message;
  } finally {
    clearingQueue.value = false;
  }
}

let searchTimer = null;
function debouncedLoad() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadAll, 220);
}

let refreshTimer = null;
onMounted(() => {
  loadAll();
  refreshTimer = setInterval(loadAll, 5000);
});
onUnmounted(() => clearInterval(refreshTimer));
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <div>
        <p class="eyebrow">OSKernel Reports</p>
        <h1>OS 内核代码分析系统</h1>
      </div>
      <div class="topbar-actions">
        <div class="report-type-picker" aria-label="选择生成报告类型">
          <label v-for="option in reportOptions" :key="option.kind" class="report-check">
            <input v-model="selectedReportTypes" type="checkbox" :value="option.kind" />
            <span>{{ option.label }}</span>
          </label>
        </div>
        <input ref="fileInput" class="hidden-input" type="file" accept=".xlsx,.xls" @change="onImport" />
        <button class="icon-button secondary" type="button" @click="fileInput?.click()">
          <Upload :size="17" />
          导入 xlsx
        </button>
        <button class="icon-button primary" type="button" :disabled="!canGenerate" @click="onGenerateMissing">
          <Play :size="17" />
          生成缺失报告
        </button>
        <button class="square-button" type="button" title="刷新" @click="loadAll">
          <RefreshCw :size="18" :class="{ spin: loading }" />
        </button>
      </div>
    </header>

    <section class="metrics-band">
      <div class="metric">
        <Database :size="18" />
        <span>总作品</span>
        <strong>{{ summary.total }}</strong>
      </div>
      <div class="metric">
        <FileText :size="18" />
        <span>已生成</span>
        <strong>{{ summary.ready }}</strong>
      </div>
      <div class="metric">
        <Loader2 :size="18" />
        <span>生成中</span>
        <strong>{{ summary.generating }}</strong>
      </div>
      <div class="metric">
        <span class="dot failed"></span>
        <span>失败</span>
        <strong>{{ summary.failed }}</strong>
      </div>
    </section>

    <section class="toolbar">
      <label class="search-box">
        <Search :size="18" />
        <input v-model="filters.q" type="search" placeholder="检索年份、学校、队伍、仓库地址或报告正文" @input="debouncedLoad" />
      </label>
      <select v-model="filters.year" @change="loadAll">
        <option value="">全部年份</option>
        <option v-for="year in years" :key="year" :value="year">{{ year }}</option>
      </select>
      <select v-model="filters.status" @change="loadAll">
        <option value="">全部状态</option>
        <option value="ready">已生成</option>
        <option value="pending">待生成</option>
        <option value="generating">生成中</option>
        <option value="failed">失败</option>
      </select>
    </section>

    <div v-if="importMessage || actionMessage || errorMessage" class="message-row">
      <span v-if="importMessage" class="message success">{{ importMessage }}</span>
      <span v-if="actionMessage" class="message neutral">{{ actionMessage }}</span>
      <span v-if="errorMessage" class="message error">{{ errorMessage }}</span>
    </div>

    <main class="content-grid">
      <section class="list-pane">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>年份</th>
                <th>学校</th>
                <th>队伍</th>
                <th>状态</th>
                <th>报告</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="repo in repositories"
                :key="repo.id"
                :class="{ selected: selected?.id === repo.id }"
                @click="selectRepo(repo)"
              >
                <td class="mono">{{ repo.id }}</td>
                <td>{{ repo.year }}</td>
                <td>{{ repo.school }}</td>
                <td class="team-cell">
                  <strong>{{ repo.team_name }}</strong>
                  <span>{{ repo.repo_url }}</span>
                </td>
                <td>
                  <span class="status-pill" :class="statusClass(repo.status)">
                    {{ statusText(repo.status) }}
                  </span>
                </td>
                <td>
                  <a v-if="repo.current_report_url" class="report-link" :href="repo.current_report_url" target="_blank" @click.stop>
                    <ExternalLink :size="15" />
                    打开
                  </a>
                  <span v-else class="muted">无</span>
                </td>
                <td>
                  <button class="tiny-button" type="button" :disabled="!canGenerate" @click.stop="onGenerateOne(repo)">
                    <Play :size="14" />
                    生成
                  </button>
                </td>
              </tr>
              <tr v-if="!repositories.length">
                <td colspan="7" class="empty-cell">暂无记录</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <aside ref="detailPane" class="detail-pane">
        <template v-if="selected">
          <div class="detail-head">
            <div>
              <p class="eyebrow">{{ selected.year }} · {{ selected.school }}</p>
              <h2>{{ selected.team_name }}</h2>
            </div>
            <span class="status-pill" :class="statusClass(selected.status)">
              {{ statusText(selected.status) }}
            </span>
          </div>
          <div class="detail-meta">
            <span>{{ selected.event }}</span>
            <span>{{ selected.sub_event }}</span>
            <span class="mono">{{ selected.id }}</span>
          </div>
          <a class="repo-url" :href="selected.repo_url" target="_blank">{{ selected.repo_url }}</a>
          <div v-if="selected.last_error" class="message error">{{ selected.last_error }}</div>
          <div v-if="selectedReportChoices.length" class="report-tabs">
            <button
              v-for="report in selectedReportChoices"
              :key="report.id"
              class="report-tab"
              :class="{ active: activeReport?.id === report.id }"
              type="button"
              @click="setActiveReportKind(report.kind)"
            >
              {{ reportKindText(report.kind) }}
            </button>
            <a v-if="selectedReportUrl" class="report-open" :href="selectedReportUrl" target="_blank">
              <ExternalLink :size="14" />
              新窗口
            </a>
          </div>
          <iframe v-if="selectedReportUrl" class="report-frame" :src="selectedReportUrl"></iframe>
          <div v-else class="empty-report">
            <FileText :size="28" />
            <span>报告尚未生成</span>
          </div>
        </template>
        <div v-else class="empty-report">
          <FileText :size="28" />
          <span>选择一个作品</span>
        </div>
      </aside>
    </main>

    <section class="jobs-band">
      <div class="section-title">
        <h2>最近任务</h2>
        <button
          class="tiny-button danger"
          type="button"
          :disabled="!clearableQueueCount || clearingQueue"
          @click="onClearQueuedJobs"
        >
          <Trash2 :size="14" />
          清空等待队列
        </button>
      </div>
      <div class="job-list">
        <div v-for="job in jobs.slice(0, 10)" :key="job.id" class="job-card">
          <div class="job-row">
            <span class="status-dot" :class="statusClass(job.status)"></span>
            <span class="mono">{{ job.id }}</span>
            <span class="job-repo">{{ job.year }} {{ job.school }} · {{ job.team_name }}</span>
            <span class="status-pill" :class="statusClass(job.status)">{{ statusText(job.status) }}</span>
            <button
              class="square-button danger compact"
              type="button"
              title="删除任务"
              :disabled="deletingJobId === job.id"
              @click="onDeleteJob(job)"
            >
              <Trash2 :size="15" />
            </button>
          </div>
          <div v-if="job.error" class="job-error">{{ job.error }}</div>
          <details v-if="job.log" class="job-log">
            <summary>查看日志尾部</summary>
            <pre>{{ jobLogTail(job.log) }}</pre>
          </details>
        </div>
        <div v-if="!jobs.length" class="muted">暂无任务</div>
      </div>
    </section>
  </div>
</template>
