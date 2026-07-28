from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import threading
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
RUNTIME_ROOT = REPO_ROOT / ".runtime"
MUTATION_HEADER = "X-Lab-Dashboard"
MAX_BODY_SIZE = 32_768


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
    def __init__(self, runtime_root: Path = RUNTIME_ROOT) -> None:
        self.runtime_root = runtime_root
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="lab-job")
        self._jobs: dict[str, Job] = {}
        self._active_projects: dict[str, str] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._managed_projects: dict[str, str] = {}
        self._process_ready: dict[str, threading.Event] = {}
        self._stop_requested: set[str] = set()
        self._lock = threading.Lock()
        self._closed = False

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

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            return [job.public() for job in jobs[:50]]

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
        for _job_id, process in processes:
            self._terminate_process(process)
        self._executor.shutdown(wait=True, cancel_futures=True)


class Dashboard:
    def __init__(
        self,
        config_path: Path = DEFAULT_CONFIG,
        lab_root: Path | None = None,
        jobs: JobManager | None = None,
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
        self.jobs = jobs or JobManager()
        self._validate()

    def _validate(self) -> None:
        seen: set[str] = set()
        for project in self.projects:
            project_id = project["id"]
            if project_id in seen:
                raise ValueError(f"Duplicate project id: {project_id}")
            seen.add(project_id)
            _safe_project_path(self.lab_root, project["path"])
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

    def project_snapshot(self, project: dict[str, Any]) -> dict[str, Any]:
        project_path = _safe_project_path(self.lab_root, project["path"])
        health = check_health(project.get("health_url"), project.get("health_contains"))
        snapshot = {
            key: value
            for key, value in project.items()
            if key not in {"actions", "health_contains"}
        }
        snapshot["path"] = str(project_path)
        snapshot["exists"] = project_path.is_dir()
        snapshot["git"] = git_status(project_path)
        snapshot["health"] = health
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
            return self.jobs.stop_managed(project_id, project["name"])
        command = project.get("actions", {}).get(action)
        if not command:
            raise KeyError("action")
        project_path = _safe_project_path(self.lab_root, project["path"])
        if not project_path.is_dir():
            raise FileNotFoundError(project_path)
        managed = bool(project.get("managed_start") and action == "start")
        return self.jobs.submit(
            project_id,
            project["name"],
            action,
            command,
            project_path,
            managed=managed,
        )

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
        if candidate != STATIC_ROOT and STATIC_ROOT not in candidate.parents:
            self._error(HTTPStatus.NOT_FOUND, "파일을 찾을 수 없습니다.")
            return
        if not candidate.is_file():
            candidate = STATIC_ROOT / "index.html"
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
    args = parser.parse_args()

    dashboard = Dashboard(config_path=args.config)
    server = create_server(args.host, args.port, dashboard)
    print(f"AI Security Lab Dashboard: http://{args.host}:{args.port}")
    print(f"Lab root: {dashboard.lab_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
        dashboard.close()


if __name__ == "__main__":
    main()
