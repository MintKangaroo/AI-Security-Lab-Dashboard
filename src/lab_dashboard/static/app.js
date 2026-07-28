const icons = {
  grid: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
  layers: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m12 2.8 9 4.7-9 4.7-9-4.7 9-4.7Z"/><path d="m3 12 9 4.7 9-4.7M3 16.5l9 4.7 9-4.7"/></svg>',
  activity: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 12h4l2.2-7 4.2 14 2.2-7H21"/></svg>',
  github: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 0 0-3.16 19.49c.5.09.68-.22.68-.48v-1.88c-2.78.6-3.37-1.18-3.37-1.18-.45-1.16-1.11-1.47-1.11-1.47-.91-.62.07-.61.07-.61 1 .07 1.53 1.03 1.53 1.03.9 1.53 2.35 1.09 2.92.83.09-.65.35-1.09.64-1.34-2.22-.25-4.55-1.11-4.55-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.27.1-2.64 0 0 .84-.27 2.75 1.02A9.56 9.56 0 0 1 12 6.82a9.5 9.5 0 0 1 2.5.34c1.91-1.3 2.75-1.02 2.75-1.02.55 1.37.2 2.39.1 2.64.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.68-4.57 4.93.36.31.68.92.68 1.86V21c0 .27.18.58.69.48A10 10 0 0 0 12 2Z"/></svg>',
  menu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M4 12h16M4 17h16"/></svg>',
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20 11a8 8 0 1 0-2.34 5.66"/><path d="M20 4v7h-7"/></svg>',
  radio: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="2"/><path d="M16.24 7.76a6 6 0 0 1 0 8.48M7.76 16.24a6 6 0 0 1 0-8.48M19.07 4.93a10 10 0 0 1 0 14.14M4.93 19.07a10 10 0 0 1 0-14.14"/></svg>',
  git: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="6" cy="5" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10M8 8c1 3 8 0 8-2"/></svg>',
  branch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="6" cy="5" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10M8 8c1 3 8 0 8-2"/></svg>',
  alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M10.3 3.7 2.5 17.1A2 2 0 0 0 4.2 20h15.6a2 2 0 0 0 1.7-2.9L13.7 3.7a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="m5 12 4 4L19 6"/></svg>',
  play: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m8 5 11 7-11 7V5Z"/></svg>',
  stop: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m6 6 12 12M18 6 6 18"/></svg>',
  terminal: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/></svg>',
  info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>',
};

const state = {
  overview: null,
  projects: [],
  jobs: [],
  selected: new Set(),
  filter: "all",
  query: "",
  pendingAction: null,
  jobPoll: null,
};

const actionLabels = {
  start: "시작",
  stop: "중지",
  test: "테스트",
};

const healthLabels = {
  online: "온라인",
  offline: "오프라인",
  occupied: "다른 서비스",
  degraded: "점검 필요",
  not_configured: "해당 없음",
};

function escapeHTML(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((element) => {
    const name = element.dataset.icon;
    if (icons[name]) element.innerHTML = icons[name];
  });
}

function relativeTime(value) {
  if (!value) return "기록 없음";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return "방금 전";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}분 전`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}시간 전`;
  return `${Math.floor(seconds / 86400)}일 전`;
}

function shortPath(path) {
  const parts = String(path || "").split("/");
  return parts.length > 3 ? `…/${parts.slice(-2).join("/")}` : path;
}

function accentClass(projectId) {
  return `accent-${String(projectId).toLowerCase().replaceAll(/[^a-z0-9_-]/g, "-")}`;
}

function setLoading(loading) {
  const button = document.getElementById("refreshButton");
  const status = document.querySelector(".sync-status");
  button.classList.toggle("is-spinning", loading);
  status.classList.toggle("is-loading", loading);
  if (loading) document.getElementById("syncLabel").textContent = "동기화 중";
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `요청 실패 (${response.status})`);
  return body;
}

async function loadOverview({ quiet = false } = {}) {
  if (!quiet) setLoading(true);
  try {
    const data = await fetchJSON("/api/overview");
    state.overview = data;
    state.projects = data.projects;
    state.jobs = data.jobs;
    renderAll();
    const updated = new Date(data.updated_at);
    document.getElementById("syncLabel").textContent =
      `${updated.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" })} 갱신`;
  } catch (error) {
    showToast(error.message, "error");
    document.getElementById("syncLabel").textContent = "연결 실패";
  } finally {
    setLoading(false);
  }
}

function renderAll() {
  const { summary } = state.overview;
  document.getElementById("labRoot").textContent = shortPath(state.overview.lab_root);
  document.getElementById("labRoot").title = state.overview.lab_root;
  document.getElementById("projectCount").textContent = summary.total;
  document.getElementById("heroProjectCount").textContent = summary.total;
  document.getElementById("totalMetric").textContent = String(summary.total).padStart(2, "0");
  document.getElementById("onlineMetric").textContent = String(summary.online).padStart(2, "0");
  document.getElementById("changesMetric").textContent = String(summary.changes).padStart(2, "0");
  document.getElementById("conflictsMetric").textContent = String(summary.port_conflicts).padStart(2, "0");
  document.getElementById("onlineNavCount").textContent = summary.online;
  document.getElementById("dirtyNavCount").textContent = summary.dirty;
  document.getElementById("onlineCaption").textContent =
    `${summary.total - summary.online} projects idle`;
  document.getElementById("changesCaption").textContent =
    `${summary.dirty} repositories dirty`;
  renderProjects();
  renderSignals();
  renderActivity();
  updateBulkBar();
}

function projectMatches(project) {
  const dirty = project.git.modified + project.git.untracked > 0;
  const filterMatch =
    state.filter === "all" ||
    (state.filter === "dirty" && dirty) ||
    (state.filter === "online" && project.health.state === "online") ||
    (state.filter === "offline" && project.health.state === "offline");
  if (!filterMatch) return false;
  if (!state.query) return true;
  const haystack = [
    project.name,
    project.description,
    project.category,
    project.stage,
    project.git.branch,
    ...(project.stack || []),
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(state.query.toLowerCase());
}

function projectRow(project) {
  const selected = state.selected.has(project.id);
  const dirty = project.git.modified + project.git.untracked > 0;
  const health = project.health.state;
  const latency = project.health.latency_ms ? `${project.health.latency_ms} ms` : "health check";
  const changes = dirty
    ? `
      ${project.git.modified ? `<span class="change-badge modified">M ${project.git.modified}</span>` : ""}
      ${project.git.untracked ? `<span class="change-badge untracked">? ${project.git.untracked}</span>` : ""}
    `
    : '<span class="change-badge clean">clean</span>';
  const dots = Array.from({ length: 4 }, (_, index) =>
    `<i class="${index < Math.min(4, (project.stack || []).length) ? "active" : ""}"></i>`,
  ).join("");

  return `
    <article
      class="project-row ${accentClass(project.id)} ${selected ? "is-selected" : ""}"
      data-project-id="${escapeHTML(project.id)}"
    >
      <button class="select-box" data-select-project="${escapeHTML(project.id)}" aria-label="${escapeHTML(project.name)} 선택">
        ${icons.check}
      </button>
      <div class="project-ident">
        <div class="project-avatar">${escapeHTML(project.short_name || project.name.slice(0, 2))}</div>
        <div>
          <strong>${escapeHTML(project.name)}</strong>
          <small>${escapeHTML(project.category || "Security research")}</small>
        </div>
      </div>
      <div class="stage-cell">
        <strong>${escapeHTML(project.stage || "In progress")}</strong>
        <div class="stack-dots">${dots}</div>
        <small>${escapeHTML((project.stack || []).slice(0, 2).join(" · "))}</small>
      </div>
      <div class="runtime-cell">
        <span class="runtime-state ${escapeHTML(health)}">
          <i></i>${escapeHTML(healthLabels[health] || health)}
        </span>
        <small class="runtime-latency">${escapeHTML(latency)}</small>
      </div>
      <div class="git-cell">
        <div class="branch-name">${icons.branch}<span>${escapeHTML(project.git.branch)}</span></div>
        <div class="change-badges">${changes}</div>
      </div>
      <button class="row-more" data-open-project="${escapeHTML(project.id)}" aria-label="${escapeHTML(project.name)} 상세">···</button>
    </article>
  `;
}

function renderProjects() {
  const list = document.getElementById("projectList");
  const visible = state.projects.filter(projectMatches);
  list.innerHTML = visible.map(projectRow).join("");
  document.getElementById("emptyState").classList.toggle("is-hidden", visible.length > 0);
  renderIcons(list);
}

function renderSignals() {
  const target = document.getElementById("signalsList");
  const signals = [];
  const conflicts = state.overview.port_conflicts;
  if (conflicts.length) {
    conflicts.slice(0, 2).forEach((conflict) => {
      const names = conflict.projects
        .map((id) => state.projects.find((project) => project.id === id)?.name || id)
        .join(", ");
      signals.push({
        type: "warning",
        icon: "alert",
        title: `포트 ${conflict.port} 공유`,
        body: `<code>${escapeHTML(names)}</code> 동시 실행 시 포트 설정을 변경하세요.`,
      });
    });
  }

  const dirty = state.projects.filter((project) => project.git.modified + project.git.untracked > 0);
  if (dirty.length) {
    signals.push({
      type: "info",
      icon: "git",
      title: `${dirty.length}개 저장소에 로컬 변경`,
      body: `총 ${state.overview.summary.changes}개 파일이 아직 커밋되지 않았습니다.`,
    });
  }
  if (state.overview.summary.online) {
    signals.push({
      type: "success",
      icon: "radio",
      title: `${state.overview.summary.online}개 서비스 응답 중`,
      body: "등록된 health endpoint가 정상 응답했습니다.",
    });
  }
  if (!signals.length) {
    signals.push({
      type: "success",
      icon: "check",
      title: "특이 신호가 없습니다",
      body: "현재 작업 공간이 안정적인 상태입니다.",
    });
  }
  target.innerHTML = signals
    .slice(0, 4)
    .map(
      (signal) => `
        <article class="signal-item">
          <div class="signal-icon ${signal.type}">${icons[signal.icon]}</div>
          <div class="signal-copy">
            <strong>${signal.title}</strong>
            <p>${signal.body}</p>
          </div>
        </article>
      `,
    )
    .join("");
}

function renderActivity() {
  const target = document.getElementById("activityList");
  document.getElementById("activityDot").classList.toggle("is-hidden", !state.jobs.length);
  if (!state.jobs.length) {
    target.innerHTML = '<div class="activity-empty">아직 실행한 작업이 없습니다.</div>';
    return;
  }
  target.innerHTML = state.jobs
    .slice(0, 6)
    .map(
      (job) => `
        <article class="activity-item" data-job-id="${escapeHTML(job.id)}">
          <span class="job-dot ${escapeHTML(job.status)}"></span>
          <div>
            <strong>${escapeHTML(job.project_name)} · ${escapeHTML(actionLabels[job.action] || job.action)}</strong>
            <small>${relativeTime(job.created_at)}</small>
          </div>
          <span class="job-status">${escapeHTML(job.status)}</span>
        </article>
      `,
    )
    .join("");

  const active = state.jobs.some((job) => ["queued", "running"].includes(job.status));
  if (active && !state.jobPoll) {
    state.jobPoll = window.setInterval(() => loadOverview({ quiet: true }), 2500);
  } else if (!active && state.jobPoll) {
    window.clearInterval(state.jobPoll);
    state.jobPoll = null;
  }
}

function updateBulkBar() {
  const count = state.selected.size;
  document.getElementById("selectionCount").textContent = count;
  document.getElementById("bulkBar").classList.toggle("is-visible", count > 0);
}

function toggleProject(projectId) {
  if (state.selected.has(projectId)) state.selected.delete(projectId);
  else state.selected.add(projectId);
  renderProjects();
  updateBulkBar();
}

function openDrawer(projectId) {
  const project = state.projects.find((item) => item.id === projectId);
  if (!project) return;
  const dirtyCount = project.git.modified + project.git.untracked;
  const actions = project.available_actions
    .map(
      (action) => `
        <button class="drawer-action" data-project-action="${escapeHTML(action)}" data-project-id="${escapeHTML(project.id)}">
          ${icons[action === "start" ? "play" : action === "stop" ? "stop" : "check"]}
          ${escapeHTML(actionLabels[action] || action)}
        </button>
      `,
    )
    .join("");
  const commit = project.git.last_commit;
  document.getElementById("drawerContent").innerHTML = `
    <div class="${accentClass(project.id)}">
      <div class="drawer-project-head">
        <div class="project-avatar">${escapeHTML(project.short_name || project.name.slice(0, 2))}</div>
        <div>
          <h2>${escapeHTML(project.name)}</h2>
          <p>${escapeHTML(project.category)} · ${escapeHTML(project.stage)}</p>
        </div>
      </div>
      <p class="drawer-description">${escapeHTML(project.description)}</p>
      <div class="drawer-actions">${actions}</div>

      <section class="drawer-section">
        <h3>Repository state</h3>
        <div class="detail-grid">
          <div class="detail-card">
            <span>Branch</span>
            <strong>${escapeHTML(project.git.branch)}</strong>
          </div>
          <div class="detail-card">
            <span>Working tree</span>
            <strong>${dirtyCount ? `${dirtyCount} changes` : "Clean"}</strong>
          </div>
          <div class="detail-card">
            <span>Runtime</span>
            <strong>${escapeHTML(healthLabels[project.health.state] || project.health.state)}</strong>
          </div>
          <div class="detail-card">
            <span>Ports</span>
            <strong>${escapeHTML(project.ports.length ? project.ports.join(", ") : "—")}</strong>
          </div>
        </div>
      </section>

      <section class="drawer-section">
        <h3>Technology</h3>
        <div class="tag-list">
          ${(project.stack || []).map((tag) => `<span class="tag">${escapeHTML(tag)}</span>`).join("")}
        </div>
      </section>

      <section class="drawer-section">
        <h3>Latest commit</h3>
        ${
          commit
            ? `<div class="commit-card">
                <strong>${escapeHTML(commit.subject)}</strong>
                <span>${escapeHTML(commit.hash)} · ${relativeTime(commit.date)}</span>
              </div>`
            : '<div class="commit-card"><strong>커밋 정보가 없습니다.</strong></div>'
        }
      </section>

      <a class="github-project-link" href="${escapeHTML(project.github)}" target="_blank" rel="noreferrer">
        <span>GitHub에서 저장소 열기</span><span>↗</span>
      </a>
    </div>
  `;
  document.getElementById("projectDrawer").classList.add("is-open");
  document.getElementById("projectDrawer").setAttribute("aria-hidden", "false");
  document.getElementById("drawerBackdrop").classList.add("is-open");
  document.body.classList.add("no-scroll");
}

function closeDrawer() {
  document.getElementById("projectDrawer").classList.remove("is-open");
  document.getElementById("projectDrawer").setAttribute("aria-hidden", "true");
  document.getElementById("drawerBackdrop").classList.remove("is-open");
  if (!document.getElementById("modalBackdrop").classList.contains("is-open")) {
    document.body.classList.remove("no-scroll");
  }
}

function requestAction({ projectIds, action }) {
  const available = projectIds.filter((id) => {
    const project = state.projects.find((item) => item.id === id);
    return project?.available_actions.includes(action);
  });
  if (!available.length) {
    showToast(`선택한 프로젝트에서 '${actionLabels[action]}' 작업을 사용할 수 없습니다.`, "error");
    return;
  }
  state.pendingAction = { projectIds: available, action, bulk: projectIds.length > 1 };
  const names = available
    .map((id) => state.projects.find((item) => item.id === id)?.name)
    .filter(Boolean);
  document.getElementById("modalTitle").textContent = `${actionLabels[action]} 작업을 실행할까요?`;
  document.getElementById("modalDescription").textContent =
    `${names.join(", ")}에서 설정에 등록된 명령을 실행합니다.${action === "stop" ? " 데이터 볼륨은 삭제하지 않습니다." : ""}`;
  document.getElementById("confirmAction").textContent = actionLabels[action];
  document.getElementById("modalBackdrop").classList.add("is-open");
  document.body.classList.add("no-scroll");
}

function closeActionModal() {
  state.pendingAction = null;
  document.getElementById("modalBackdrop").classList.remove("is-open");
  if (!document.getElementById("projectDrawer").classList.contains("is-open")) {
    document.body.classList.remove("no-scroll");
  }
}

async function executePendingAction() {
  if (!state.pendingAction) return;
  const pending = state.pendingAction;
  const confirm = document.getElementById("confirmAction");
  confirm.disabled = true;
  confirm.textContent = "등록 중…";
  try {
    const options = {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Lab-Dashboard": "1",
      },
    };
    let result;
    if (pending.projectIds.length === 1) {
      options.body = "{}";
      result = await fetchJSON(
        `/api/projects/${encodeURIComponent(pending.projectIds[0])}/actions/${encodeURIComponent(pending.action)}`,
        options,
      );
    } else {
      options.body = JSON.stringify({
        project_ids: pending.projectIds,
        action: pending.action,
      });
      result = await fetchJSON("/api/actions/bulk", options);
    }
    const count = result.jobs?.length || (result.job ? 1 : 0);
    showToast(`${count}개 작업을 실행 대기열에 등록했습니다.`);
    state.selected.clear();
    closeActionModal();
    closeDrawer();
    await loadOverview({ quiet: true });
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    confirm.disabled = false;
    confirm.textContent = pending ? actionLabels[pending.action] : "실행";
  }
}

async function openLog(jobId) {
  const job = state.jobs.find((item) => item.id === jobId);
  document.getElementById("logTitle").textContent =
    job ? `${job.project_name} · ${actionLabels[job.action] || job.action}` : "작업 로그";
  document.getElementById("logOutput").textContent = "로그를 불러오는 중…";
  document.getElementById("logBackdrop").classList.add("is-open");
  document.body.classList.add("no-scroll");
  try {
    const data = await fetchJSON(`/api/jobs/${encodeURIComponent(jobId)}/log`);
    document.getElementById("logOutput").textContent =
      data.log || "아직 출력된 로그가 없습니다.";
  } catch (error) {
    document.getElementById("logOutput").textContent = error.message;
  }
}

function closeLog() {
  document.getElementById("logBackdrop").classList.remove("is-open");
  document.body.classList.remove("no-scroll");
}

function showToast(message, type = "success") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  document.getElementById("toastRegion").append(toast);
  window.setTimeout(() => toast.remove(), 4200);
}

function setFilter(filter) {
  state.filter = filter;
  document.querySelectorAll(".filter-tab").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.filter === filter);
  });
  renderProjects();
}

function bindEvents() {
  document.getElementById("refreshButton").addEventListener("click", () => loadOverview());
  document.getElementById("searchFocus").addEventListener("click", () => {
    document.getElementById("projectSearch").focus();
    document.querySelector(".portfolio-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      document.getElementById("projectSearch").focus();
    }
    if (event.key === "Escape") {
      closeDrawer();
      closeActionModal();
      closeLog();
    }
  });
  document.getElementById("projectSearch").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    renderProjects();
  });
  document.querySelectorAll(".filter-tab").forEach((button) => {
    button.addEventListener("click", () => setFilter(button.dataset.filter));
  });
  document.querySelectorAll(".saved-view").forEach((button) => {
    button.addEventListener("click", () => {
      setFilter(button.dataset.filter);
      document.querySelector(".portfolio-panel").scrollIntoView({ behavior: "smooth" });
    });
  });
  document.getElementById("projectList").addEventListener("click", (event) => {
    const select = event.target.closest("[data-select-project]");
    if (select) {
      event.stopPropagation();
      toggleProject(select.dataset.selectProject);
      return;
    }
    const row = event.target.closest("[data-project-id]");
    if (row) openDrawer(row.dataset.projectId);
  });
  document.getElementById("drawerContent").addEventListener("click", (event) => {
    const action = event.target.closest("[data-project-action]");
    if (action) {
      requestAction({
        projectIds: [action.dataset.projectId],
        action: action.dataset.projectAction,
      });
    }
  });
  document.getElementById("closeDrawer").addEventListener("click", closeDrawer);
  document.getElementById("drawerBackdrop").addEventListener("click", closeDrawer);
  document.getElementById("clearSelection").addEventListener("click", () => {
    state.selected.clear();
    renderProjects();
    updateBulkBar();
  });
  document.querySelectorAll("[data-bulk-action]").forEach((button) => {
    button.addEventListener("click", () =>
      requestAction({
        projectIds: [...state.selected],
        action: button.dataset.bulkAction,
      }),
    );
  });
  document.getElementById("cancelAction").addEventListener("click", closeActionModal);
  document.getElementById("confirmAction").addEventListener("click", executePendingAction);
  document.getElementById("modalBackdrop").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeActionModal();
  });
  document.getElementById("activityList").addEventListener("click", (event) => {
    const item = event.target.closest("[data-job-id]");
    if (item) openLog(item.dataset.jobId);
  });
  document.getElementById("closeLog").addEventListener("click", closeLog);
  document.getElementById("logBackdrop").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeLog();
  });
  document.getElementById("viewAllActivity").addEventListener("click", () => {
    document.getElementById("activityList").scrollIntoView({ behavior: "smooth" });
  });
  document.getElementById("mobileMenu").addEventListener("click", () => {
    document.getElementById("sidebar").classList.toggle("is-open");
  });
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("is-active"));
      button.classList.add("is-active");
      document.getElementById("viewTitle").textContent =
        button.dataset.view[0].toUpperCase() + button.dataset.view.slice(1);
      const target =
        button.dataset.view === "overview"
          ? document.querySelector(".hero")
          : button.dataset.view === "projects"
            ? document.querySelector(".portfolio-panel")
            : document.querySelector(".activity-panel");
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      document.getElementById("sidebar").classList.remove("is-open");
    });
  });
}

renderIcons();
bindEvents();
loadOverview();
