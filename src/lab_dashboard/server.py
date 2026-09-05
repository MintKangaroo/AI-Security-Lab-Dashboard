from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]
STATIC_ROOT = PACKAGE_ROOT / "static"
DEFAULT_CONFIG = PACKAGE_ROOT / "config" / "projects.json"
RUNTIME_ENV_VAR = "AI_SECURITY_LAB_DASHBOARD_RUNTIME"
MUTATION_HEADER = "X-Lab-Dashboard"
MAX_BODY_SIZE = 32_768
STATUS_SCHEMA = "lab-status/1"
STATUS_STATES = frozenset({"ok", "warn", "error", "unknown"})
MAX_STATUS_BYTES = 64_000
MAX_STATUS_METRICS = 12
MAX_JOB_HISTORY = 50
OVERVIEW_CACHE_SECONDS = 1.0
ACTIVE_JOB_STATES = frozenset({"queued", "running"})
ACCENT_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def resolve_runtime_root(explicit: Path | None = None) -> Path:
    """Decide where job history and logs are written.

    In a source checkout that is the repo's own `.runtime/`. Installed as a
    console script the package lives in site-packages, so REPO_ROOT points at
    whatever directory happens to sit above it -- usually the virtualenv -- and
    state would be written there. The marker file is what tells the two apart;
    outside a checkout the state lands in the user's XDG state directory.
    """
    if explicit is not None:
        return explicit.expanduser()
    override = os.getenv(RUNTIME_ENV_VAR)
    if override:
        return Path(override).expanduser()
    if (REPO_ROOT / "pyproject.toml").is_file():
        return REPO_ROOT / ".runtime"
    state_home = os.getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state_home) / "ai-security-lab-dashboard"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_capture(command: list[str], cwd: Path, timeout: float = 4.0) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _safe_project_path(lab_root: Path, relative_path: str) -> Path:
    candidate = (lab_root / relative_path).resolve()
    resolved_root = lab_root.resolve()
    if candidate.parent != resolved_root:
        raise ValueError(f"Project path must be a direct child of the lab root: {relative_path}")
    return candidate


def _safe_status_path(project_path: Path, relative: str) -> Path:
    candidate = (project_path / relative).resolve()
    root = project_path.resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"Status file must live inside the project: {relative}")
    return candidate


def _status_note(headline: str, state: str = "error") -> dict[str, Any]:
    return {
        "state": state,
        "headline": headline,
        "generated_at": "",
        "last_run_at": "",
        "metrics": [],
    }


def _clip(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def read_status(project_path: Path, relative: str | None) -> dict[str, Any] | None:
    """Read a status document a project publishes about its own work.

    The dashboard knows nothing about what the numbers mean; it renders
    label/value pairs. The file is written by the project rather than by the
    dashboard, so every field is treated as untrusted input: an unrecognised
    state collapses to "unknown", the metric list is capped, and each string is
    truncated before it reaches the browser.
    """
    if not relative:
        return None
    try:
        path = _safe_status_path(project_path, relative)
    except ValueError as error:
        return _status_note(str(error))
    if not path.is_file():
        return _status_note("no status published", state="unknown")
    try:
        if path.stat().st_size > MAX_STATUS_BYTES:
            return _status_note("status file is too large to read")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _status_note("status file is unreadable")
    if not isinstance(document, dict) or document.get("schema") != STATUS_SCHEMA:
        return _status_note(f"unsupported status schema, expected {STATUS_SCHEMA}")

    metrics: list[dict[str, str]] = []
    for entry in document.get("metrics") or []:
        if len(metrics) == MAX_STATUS_METRICS:
            break
        if not isinstance(entry, dict):
            continue
        label = _clip(entry.get("label"), 40)
        value = _clip(entry.get("value"), 80)
        if label and value:
            metrics.append({"label": label, "value": value})
    state = document.get("state")
    return {
        "state": state if state in STATUS_STATES else "unknown",
        "headline": _clip(document.get("headline"), 120) or "status published",
        "generated_at": _clip(document.get("generated_at"), 40),
        "last_run_at": _clip(document.get("last_run_at"), 40),
        "metrics": metrics,
    }


def git_status(project_path: Path) -> dict[str, Any]:
    if not (project_path / ".git").exists():
        return {
            "available": False,
            "branch": "not-a-repository",
            "modified": 0,
            "untracked": 0,
            "ahead": 0,
            "behind": 0,
            "last_commit": None,
        }

    branch = _run_capture(["git", "branch", "--show-current"], project_path) or "detached"
    porcelain = _run_capture(["git", "status", "--porcelain=v1"], project_path)
    lines = [line for line in porcelain.splitlines() if line]
    untracked = sum(line.startswith("??") for line in lines)
    modified = len(lines) - untracked

    ahead = 0
    behind = 0
    upstream = _run_capture(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        project_path,
    )
    if upstream:
        counts = _run_capture(
            ["git", "rev-list", "--left-right", "--count", f"HEAD...{upstream}"],
            project_path,
        ).split()
        if len(counts) == 2:
            ahead, behind = (int(counts[0]), int(counts[1]))

    commit = _run_capture(
        [
            "git",
            "log",
            "-1",
            "--date=iso-strict",
            "--pretty=format:%h%x1f%ad%x1f%s",
        ],
        project_path,
    )
    last_commit = None
    if commit:
        parts = commit.split("\x1f", maxsplit=2)
        if len(parts) == 3:
            last_commit = {"hash": parts[0], "date": parts[1], "subject": parts[2]}

    return {
        "available": True,
        "branch": branch,
        "modified": modified,
        "untracked": untracked,
        "ahead": ahead,
        "behind": behind,
        "last_commit": last_commit,
    }


def check_health(url: str | None, expected: str | None) -> dict[str, Any]:
    if not url:
        return {"state": "not_configured", "latency_ms": None, "url": None}

    started = datetime.now(timezone.utc)
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "AI-Security-Lab-Dashboard/0.1"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=0.7) as response:
            body = response.read(16_384).decode("utf-8", errors="replace")
            elapsed = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            if expected and expected.lower() not in body.lower():
                return {"state": "occupied", "latency_ms": elapsed, "url": url}
            state = "online" if 200 <= response.status < 400 else "degraded"
            return {"state": state, "latency_ms": elapsed, "url": url}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return {"state": "offline", "latency_ms": None, "url": url}


@dataclass
class Job:
    id: str
    project_id: str
    project_name: str
    action: str
    command: list[str]
    kind: str = "task"
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    log_path: str | None = None

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("command", None)
        payload.pop("log_path", None)
        return payload


class JobConflictError(RuntimeError):
    """Raised when an action conflicts with an active project job."""


class JobManager:
    def __init__(self, runtime_root: Path | None = None) -> None:
        self.runtime_root = resolve_runtime_root(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.history_path = self.runtime_root / "jobs.json"
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="lab-job")
        self._jobs: dict[str, Job] = self._load_history()
        self._active_projects: dict[str, str] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._managed_projects: dict[str, str] = {}
        self._process_ready: dict[str, threading.Event] = {}
        self._stop_requested: set[str] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._remove_orphan_logs()

    def _load_history(self) -> dict[str, Job]:
        if not self.history_path.is_file():
            return {}
        try:
            records = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(records, list):
            return {}
        jobs: dict[str, Job] = {}
        for record in sorted(
            (record for record in records if isinstance(record, dict)),
            key=lambda record: record.get("created_at") or "",
            reverse=True,
        )[:MAX_JOB_HISTORY]:
            required = ("id", "project_id", "project_name", "action")
            if not all(isinstance(record.get(key), str) and record[key] for key in required):
                continue
            status = record.get("status")
            if not isinstance(status, str):
                continue
            if status in ACTIVE_JOB_STATES:
                status = "interrupted"
            job = Job(
                id=record["id"],
                project_id=record["project_id"],
                project_name=record["project_name"],
                action=record["action"],
                command=["dashboard", "historical-job"],
                kind=record.get("kind", "task") if isinstance(record.get("kind"), str) else "task",
                status=status,
                created_at=record.get("created_at", utc_now()),
                started_at=record.get("started_at"),
                finished_at=(
                    record.get("finished_at") or (utc_now() if status == "interrupted" else None)
                ),
                exit_code=(
                    record.get("exit_code")
                    if isinstance(record.get("exit_code"), int)
                    else None
                ),
                log_path=str(self.runtime_root / f"{record['id']}.log"),
            )
            jobs[job.id] = job
        return jobs

    def _prune_locked(self) -> None:
        """Keep the job history bounded, logs included.

        Only the newest MAX_JOB_HISTORY finished jobs are worth keeping -- the
        UI never shows more -- but an unfinished job is retained regardless of
        age, because dropping it would strand its running process without a
        record. Evicting a job deletes its log too; otherwise the history file
        stays small while .runtime/ grows without limit.
        """
        finished = [job for job in self._jobs.values() if job.status not in ACTIVE_JOB_STATES]
        if len(finished) <= MAX_JOB_HISTORY:
            return
        finished.sort(key=lambda job: job.created_at, reverse=True)
        for job in finished[MAX_JOB_HISTORY:]:
            self._jobs.pop(job.id, None)
            self._delete_log(job)

    def _delete_log(self, job: Job) -> None:
        path = Path(job.log_path) if job.log_path else self.runtime_root / f"{job.id}.log"
        with suppress(OSError):
            path.unlink()

    def _remove_orphan_logs(self) -> None:
        """Drop logs no retained job points at.

        Runs once at startup, after the history has been loaded and capped, so
        it also collects the logs of records the cap dropped, plus anything left
        behind by a history file that was truncated or lost.
        """
        known = {f"{job_id}.log" for job_id in self._jobs}
        with suppress(OSError):
            for path in self.runtime_root.glob("*.log"):
                if path.name not in known:
                    with suppress(OSError):
                        path.unlink()

    def _persist_locked(self) -> None:
        self._prune_locked()
        payload = [job.public() for job in self._jobs.values()]
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.runtime_root,
                prefix="jobs-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary_path, self.history_path)
        except OSError:
            if temporary_path:
                with suppress(OSError):
                    Path(temporary_path).unlink()

    def submit(
        self,
        project_id: str,
        project_name: str,
        action: str,
        command: list[str],
        cwd: Path,
        *,
        managed: bool = False,
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            project_id=project_id,
            project_name=project_name,
            action=action,
            command=list(command),
            kind="service" if managed else "task",
        )
        job.log_path = str(self.runtime_root / f"{job.id}.log")
        with self._lock:
            if self._closed:
                raise JobConflictError("작업 관리자가 종료 중입니다.")
            if project_id in self._active_projects:
                raise JobConflictError("이 프로젝트에서 이미 작업이 실행 중입니다.")
            self._jobs[job.id] = job
            self._active_projects[project_id] = job.id
            if managed:
                self._process_ready[project_id] = threading.Event()
            self._persist_locked()
        self._executor.submit(self._execute, job, cwd)
        return job

    def _execute(self, job: Job, cwd: Path) -> None:
        log_path = Path(job.log_path or self.runtime_root / f"{job.id}.log")
        with self._lock:
            job.status = "running"
            job.started_at = utc_now()

        process: subprocess.Popen[str] | None = None
        try:
            with log_path.open("w", encoding="utf-8") as log:
                log.write(f"$ {' '.join(job.command)}\n\n")
                log.flush()
                process = subprocess.Popen(
                    job.command,
                    cwd=cwd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                with self._lock:
                    self._processes[job.id] = process
                    if job.kind == "service":
                        self._managed_projects[job.project_id] = job.id
                        self._process_ready[job.project_id].set()
                    closing = self._closed
                    if closing:
                        self._stop_requested.add(job.id)
                if closing:
                    self._terminate_process(process)
                timeout = None if job.kind == "service" else 1800
                exit_code = process.wait(timeout=timeout)
            with self._lock:
                stopped = job.id in self._stop_requested
            status = "stopped" if stopped else ("succeeded" if exit_code == 0 else "failed")
        except subprocess.TimeoutExpired:
            if process is not None:
                self._terminate_process(process)
            exit_code = -1
            status = "timed_out"
            with log_path.open("a", encoding="utf-8") as log:
                log.write("\nDashboard timeout after 30 minutes.\n")
        except (OSError, ValueError) as exc:
            exit_code = -1
            status = "failed"
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\nCould not start command: {exc}\n")

        with self._lock:
            job.status = status
            job.exit_code = exit_code
            job.finished_at = utc_now()
            self._processes.pop(job.id, None)
            if self._active_projects.get(job.project_id) == job.id:
                self._active_projects.pop(job.project_id, None)
            if self._managed_projects.get(job.project_id) == job.id:
                self._managed_projects.pop(job.project_id, None)
            ready_event = self._process_ready.pop(job.project_id, None)
            if ready_event:
                ready_event.set()
            self._stop_requested.discard(job.id)
            self._persist_locked()

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str], grace_seconds: float = 5.0) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                process.terminate()
            except OSError:
                return
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                return
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)

    def stop_managed(self, project_id: str, project_name: str) -> Job:
        with self._lock:
            ready_event = self._process_ready.get(project_id)
            should_wait = project_id in self._active_projects and ready_event is not None
        if should_wait:
            ready_event.wait(timeout=2)

        with self._lock:
            if self._closed:
                raise JobConflictError("작업 관리자가 종료 중입니다.")
            start_job_id = self._managed_projects.get(project_id)
            process = self._processes.get(start_job_id or "")
            if not start_job_id or process is None:
                if project_id in self._active_projects:
                    message = "서비스가 아직 시작 중입니다. 잠시 후 다시 시도해 주세요."
                    raise JobConflictError(message)
                raise JobConflictError("이 대시보드가 시작한 실행 중 서비스가 없습니다.")
            if start_job_id in self._stop_requested:
                raise JobConflictError("서비스 중지 작업이 이미 진행 중입니다.")

            self._stop_requested.add(start_job_id)
            job = Job(
                id=uuid.uuid4().hex[:12],
                project_id=project_id,
                project_name=project_name,
                action="stop",
                command=["dashboard", "stop-managed-service"],
            )
            job.log_path = str(self.runtime_root / f"{job.id}.log")
            self._jobs[job.id] = job
            self._persist_locked()
        self._executor.submit(self._execute_managed_stop, job, process)
        return job

    def _execute_managed_stop(
        self,
        job: Job,
        process: subprocess.Popen[str],
    ) -> None:
        log_path = Path(job.log_path or self.runtime_root / f"{job.id}.log")
        with self._lock:
            job.status = "running"
            job.started_at = utc_now()
        try:
            with log_path.open("w", encoding="utf-8") as log:
                log.write(f"Stopping managed service for {job.project_name}.\n")
            self._terminate_process(process)
            exit_code = 0
            status = "succeeded"
        except OSError as exc:
            exit_code = -1
            status = "failed"
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"Could not stop managed service: {exc}\n")
        with self._lock:
            job.status = status
            job.exit_code = exit_code
            job.finished_at = utc_now()
            self._persist_locked()

    def list(self) -> list[dict[str, Any]]:
        """The newest jobs, with every unfinished one guaranteed a place.

        A long-running service is older than the tasks that follow it, so a
        plain newest-first cut can drop it -- and the browser stops polling for
        job updates the moment nothing running is listed.
        """
        with self._lock:
            jobs = list(self._jobs.values())
        active = [job for job in jobs if job.status in ACTIVE_JOB_STATES]
        finished = [job for job in jobs if job.status not in ACTIVE_JOB_STATES]
        finished.sort(key=lambda item: item.created_at, reverse=True)
        selected = active + finished[: max(0, MAX_JOB_HISTORY - len(active))]
        selected.sort(key=lambda item: item.created_at, reverse=True)
        return [job.public() for job in selected]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_for(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            job_id = self._active_projects.get(project_id)
            job = self._jobs.get(job_id or "")
            return job.public() if job else None

    def read_log(self, job_id: str, max_bytes: int = 64_000) -> str | None:
        job = self.get(job_id)
        if not job:
            return None
        path = Path(job.log_path or self.runtime_root / f"{job.id}.log")
        if not path.is_file():
            return ""
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            processes = list(self._processes.items())
            for job_id, _process in processes:
                self._stop_requested.add(job_id)
            for job in self._jobs.values():
                if job.status == "queued":
                    job.status = "canceled"
                    job.finished_at = utc_now()
            self._persist_locked()
        for _job_id, process in processes:
            self._terminate_process(process)
        self._executor.shutdown(wait=True, cancel_futures=True)


class Dashboard:
    def __init__(
        self,
        config_path: Path = DEFAULT_CONFIG,
        lab_root: Path | None = None,
        jobs: JobManager | None = None,
        runtime_root: Path | None = None,
    ) -> None:
        self.config_path = config_path.resolve()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        configured_root = os.getenv("AI_SECURITY_LAB_ROOT", config.get("lab_root", ".."))
        if lab_root is None:
            root_candidate = Path(configured_root)
            if not root_candidate.is_absolute():
                root_candidate = REPO_ROOT / root_candidate
            lab_root = root_candidate
        self.lab_root = lab_root.resolve()
        self.projects = config["projects"]
        self.project_map = {project["id"]: project for project in self.projects}
        self.jobs = jobs or JobManager(runtime_root)
        self._overview_lock = threading.Lock()
        self._overview_cache: tuple[float, dict[str, Any]] | None = None
        self._validate()

    def _validate(self) -> None:
        seen: set[str] = set()
        for project in self.projects:
            project_id = project["id"]
            # Ids end up in a CSS attribute selector, so keep them boring.
            if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.match(project_id):
                raise ValueError(f"Project id must be lowercase and url-safe: {project_id!r}")
            if project_id in seen:
                raise ValueError(f"Duplicate project id: {project_id}")
            seen.add(project_id)
            project_path = _safe_project_path(self.lab_root, project["path"])
            accent = project.get("accent")
            # The browser puts this straight into a style attribute.
            if accent is not None and not (
                isinstance(accent, str) and ACCENT_PATTERN.match(accent)
            ):
                raise ValueError(f"Accent must be a #rrggbb colour: {project_id}")
            status_file = project.get("status_file")
            if status_file is not None:
                if not isinstance(status_file, str) or not status_file:
                    raise ValueError(f"Status file must be a relative path: {project_id}")
                _safe_status_path(project_path, status_file)
            if project.get("managed_start") and "start" not in project.get("actions", {}):
                raise ValueError(f"Managed project must define a start action: {project_id}")
            for action, command in project.get("actions", {}).items():
                if action not in {"start", "stop", "test"}:
                    raise ValueError(f"Unsupported action for {project_id}: {action}")
                if (
                    not isinstance(command, list)
                    or not command
                    or not all(isinstance(part, str) and part for part in command)
                ):
                    message = f"Action must be a non-empty command array: {project_id}/{action}"
                    raise ValueError(message)

    def accent_css(self) -> str:
        """Per-project accents as a real stylesheet.

        These cannot be inline `style` attributes: the dashboard serves
        `style-src 'self'`, which drops them silently -- the attribute lands in
        the DOM and the declaration never applies. Hardcoding one CSS rule per
        project instead meant a newly registered project rendered with an
        undefined custom property until someone remembered the rule, so the
        config is the single source and this is generated from it.
        """
        rules = [
            f'[data-project-id="{project["id"]}"] {{ --project-accent: {project["accent"]}; }}'
            for project in self.projects
            if project.get("accent")
        ]
        return "\n".join(rules) + "\n"

    def project_snapshot(self, project: dict[str, Any]) -> dict[str, Any]:
        project_path = _safe_project_path(self.lab_root, project["path"])
        health = check_health(project.get("health_url"), project.get("health_contains"))
        snapshot = {
            key: value
            for key, value in project.items()
            if key not in {"actions", "health_contains", "status_file"}
        }
        snapshot["path"] = str(project_path)
        snapshot["exists"] = project_path.is_dir()
        snapshot["git"] = git_status(project_path)
        snapshot["health"] = health
        snapshot["status"] = read_status(project_path, project.get("status_file"))
        active_job = self.jobs.active_for(project["id"])
        available_actions = list(project.get("actions", {}).keys())
        if (
            project.get("managed_start")
            and active_job
            and active_job["kind"] == "service"
            and "stop" not in available_actions
        ):
            available_actions.append("stop")
        snapshot["available_actions"] = available_actions
        snapshot["active_job"] = active_job
        return snapshot

    def overview(self) -> dict[str, Any]:
        """The full portfolio snapshot, computed at most once per cache window.

        Building it forks a handful of git processes per project and probes
        every health endpoint, and the browser polls it every couple of seconds
        while a job runs -- each open tab separately. The lock also makes
        concurrent callers share one computation instead of each forking their
        own. Anything that changes state the browser acts on right away (a job
        being submitted) invalidates the cache, so what it can be stale about is
        only a job finishing or a repository changing underneath, by at most
        the window.
        """
        with self._overview_lock:
            cached = self._overview_cache
            if cached and time.monotonic() - cached[0] < OVERVIEW_CACHE_SECONDS:
                return cached[1]
            payload = self._build_overview()
            self._overview_cache = (time.monotonic(), payload)
            return payload

    def _invalidate_overview(self) -> None:
        with self._overview_lock:
            self._overview_cache = None

    def _build_overview(self) -> dict[str, Any]:
        snapshots: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(self.projects) or 1)) as executor:
            futures = {
                executor.submit(self.project_snapshot, project): project["id"]
                for project in self.projects
            }
            for future in as_completed(futures):
                snapshots.append(future.result())
        order = {project["id"]: index for index, project in enumerate(self.projects)}
        snapshots.sort(key=lambda item: order[item["id"]])

        port_owners: dict[int, list[str]] = {}
        for project in snapshots:
            for port in project.get("ports", []):
                port_owners.setdefault(int(port), []).append(project["id"])
        conflicts = [
            {"port": port, "projects": owners}
            for port, owners in sorted(port_owners.items())
            if len(owners) > 1
        ]

        dirty = sum(
            bool(project["git"]["modified"] or project["git"]["untracked"])
            for project in snapshots
        )
        online = sum(project["health"]["state"] == "online" for project in snapshots)
        total_changes = sum(
            project["git"]["modified"] + project["git"]["untracked"] for project in snapshots
        )
        return {
            "updated_at": utc_now(),
            "lab_root": str(self.lab_root),
            "summary": {
                "total": len(snapshots),
                "online": online,
                "dirty": dirty,
                "changes": total_changes,
                "port_conflicts": len(conflicts),
            },
            "port_conflicts": conflicts,
            "projects": snapshots,
            "jobs": self.jobs.list(),
        }

    def run_action(self, project_id: str, action: str) -> Job:
        project = self.project_map.get(project_id)
        if not project:
            raise KeyError("project")
        if action == "stop" and project.get("managed_start"):
            job = self.jobs.stop_managed(project_id, project["name"])
            self._invalidate_overview()
            return job
        command = project.get("actions", {}).get(action)
        if not command:
            raise KeyError("action")
        project_path = _safe_project_path(self.lab_root, project["path"])
        if not project_path.is_dir():
            raise FileNotFoundError(project_path)
        managed = bool(project.get("managed_start") and action == "start")
        job = self.jobs.submit(
            project_id,
            project["name"],
            action,
            command,
            project_path,
            managed=managed,
        )
        self._invalidate_overview()
        return job

    def close(self) -> None:
        self.jobs.shutdown()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "LabDashboard/0.1"
    dashboard: Dashboard

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")

    def _send_headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        cache_control = "no-store" if content_type.startswith("application/json") else "no-cache"
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'",
        )
        self.end_headers()

    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length < 0:
            raise ValueError("Invalid Content-Length")
        if length > MAX_BODY_SIZE:
            raise OverflowError("Request body is too large")
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _mutation_allowed(self) -> bool:
        if self.headers.get(MUTATION_HEADER) != "1":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        origin_host = urlsplit(origin).hostname
        return origin_host in {"127.0.0.1", "localhost", "::1"}

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        if path == "/api/overview":
            self._json(self.dashboard.overview())
            return
        if path == "/api/jobs":
            self._json({"jobs": self.dashboard.jobs.list()})
            return
        if path.startswith("/api/jobs/") and path.endswith("/log"):
            job_id = path.removeprefix("/api/jobs/").removesuffix("/log").strip("/")
            log = self.dashboard.jobs.read_log(job_id)
            if log is None:
                self._error(HTTPStatus.NOT_FOUND, "작업 로그를 찾을 수 없습니다.")
                return
            self._json({"job_id": job_id, "log": log})
            return
        if path == "/accents.css":
            body = self.dashboard.accent_css().encode("utf-8")
            self._send_headers(HTTPStatus.OK, "text/css; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "API 경로를 찾을 수 없습니다.")
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        if not self._mutation_allowed():
            self._error(HTTPStatus.FORBIDDEN, "로컬 대시보드 요청만 작업을 실행할 수 있습니다.")
            return
        path = unquote(urlsplit(self.path).path)
        try:
            payload = self._read_json()
        except OverflowError as exc:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
            return
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        if path == "/api/actions/bulk":
            self._handle_bulk(payload)
            return

        parts = [part for part in path.split("/") if part]
        if len(parts) == 5 and parts[:2] == ["api", "projects"] and parts[3] == "actions":
            project_id, action = parts[2], parts[4]
            try:
                job = self.dashboard.run_action(project_id, action)
            except KeyError:
                self._error(HTTPStatus.NOT_FOUND, "프로젝트 또는 작업을 찾을 수 없습니다.")
                return
            except JobConflictError as exc:
                self._error(HTTPStatus.CONFLICT, str(exc))
                return
            except FileNotFoundError:
                self._error(HTTPStatus.CONFLICT, "로컬 프로젝트 디렉터리를 찾을 수 없습니다.")
                return
            self._json({"job": job.public()}, HTTPStatus.ACCEPTED)
            return

        self._error(HTTPStatus.NOT_FOUND, "API 경로를 찾을 수 없습니다.")

    def _handle_bulk(self, payload: dict[str, Any]) -> None:
        project_ids = payload.get("project_ids")
        action = payload.get("action")
        if (
            not isinstance(project_ids, list)
            or not project_ids
            or len(project_ids) > len(self.dashboard.projects)
            or not all(isinstance(item, str) for item in project_ids)
        ):
            self._error(HTTPStatus.BAD_REQUEST, "project_ids 목록을 확인해 주세요.")
            return
        if action not in {"start", "stop", "test"}:
            self._error(HTTPStatus.BAD_REQUEST, "지원하지 않는 일괄 작업입니다.")
            return

        jobs: list[dict[str, Any]] = []
        skipped: list[str] = []
        conflicts: list[dict[str, str]] = []
        for project_id in dict.fromkeys(project_ids):
            try:
                jobs.append(self.dashboard.run_action(project_id, action).public())
            except JobConflictError as exc:
                conflicts.append({"project_id": project_id, "reason": str(exc)})
            except (KeyError, FileNotFoundError):
                skipped.append(project_id)
        if not jobs:
            message = "선택한 프로젝트에 실행 가능한 작업이 없거나 이미 작업 중입니다."
            self._error(HTTPStatus.CONFLICT, message)
            return
        self._json(
            {"jobs": jobs, "skipped": skipped, "conflicts": conflicts},
            HTTPStatus.ACCEPTED,
        )

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (STATIC_ROOT / relative).resolve()
        # There is no client-side routing to fall back to index.html for; a
        # path that is not a shipped file is simply missing.
        if STATIC_ROOT not in candidate.parents or not candidate.is_file():
            self._error(HTTPStatus.NOT_FOUND, "파일을 찾을 수 없습니다.")
            return
        suffix_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }
        body = candidate.read_bytes()
        content_type = suffix_types.get(candidate.suffix, "application/octet-stream")
        self._send_headers(HTTPStatus.OK, content_type, len(body))
        self.wfile.write(body)


def create_server(
    host: str = "127.0.0.1",
    port: int = 4173,
    dashboard: Dashboard | None = None,
) -> ThreadingHTTPServer:
    active_dashboard = dashboard or Dashboard()

    class BoundHandler(DashboardHandler):
        pass

    BoundHandler.dashboard = active_dashboard
    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Security Lab local dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=4173, help="Bind port (default: 4173)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Project config JSON")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help=f"Job history and logs directory (env: {RUNTIME_ENV_VAR})",
    )
    args = parser.parse_args()

    dashboard = Dashboard(config_path=args.config, runtime_root=args.runtime_root)
    server = create_server(args.host, args.port, dashboard)
    print(f"AI Security Lab Dashboard: http://{args.host}:{args.port}")
    print(f"Lab root: {dashboard.lab_root}")
    print(f"Runtime root: {dashboard.jobs.runtime_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
        dashboard.close()


if __name__ == "__main__":
    main()
