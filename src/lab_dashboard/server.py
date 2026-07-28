from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
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
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    log_path: str | None = None

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("command", None)
        return payload


class JobManager:
    def __init__(self, runtime_root: Path = RUNTIME_ROOT) -> None:
        self.runtime_root = runtime_root
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="lab-job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        project_id: str,
        project_name: str,
        action: str,
        command: list[str],
        cwd: Path,
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            project_id=project_id,
            project_name=project_name,
            action=action,
            command=list(command),
        )
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(self._execute, job, cwd)
        return job

    def _execute(self, job: Job, cwd: Path) -> None:
        log_path = self.runtime_root / f"{job.id}.log"
        with self._lock:
            job.status = "running"
            job.started_at = utc_now()
            job.log_path = str(log_path)

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
                exit_code = process.wait(timeout=1800)
            status = "succeeded" if exit_code == 0 else "failed"
        except subprocess.TimeoutExpired:
            process.terminate()
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

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            return [job.public() for job in jobs[:50]]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def read_log(self, job_id: str, max_bytes: int = 64_000) -> str | None:
        job = self.get(job_id)
        if not job or not job.log_path:
            return None
        path = Path(job.log_path)
        if not path.is_file():
            return ""
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")


class Dashboard:
    def __init__(self, config_path: Path = DEFAULT_CONFIG, lab_root: Path | None = None) -> None:
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
        self.jobs = JobManager()
        self._validate()

    def _validate(self) -> None:
        seen: set[str] = set()
        for project in self.projects:
            project_id = project["id"]
            if project_id in seen:
                raise ValueError(f"Duplicate project id: {project_id}")
            seen.add(project_id)
            _safe_project_path(self.lab_root, project["path"])
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
        snapshot["available_actions"] = list(project.get("actions", {}).keys())
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
        command = project.get("actions", {}).get(action)
        if not command:
            raise KeyError("action")
        project_path = _safe_project_path(self.lab_root, project["path"])
        if not project_path.is_dir():
            raise FileNotFoundError(project_path)
        return self.jobs.submit(project_id, project["name"], action, command, project_path)


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
        for project_id in dict.fromkeys(project_ids):
            try:
                jobs.append(self.dashboard.run_action(project_id, action).public())
            except (KeyError, FileNotFoundError):
                skipped.append(project_id)
        if not jobs:
            self._error(HTTPStatus.CONFLICT, "선택한 프로젝트에 실행 가능한 작업이 없습니다.")
            return
        self._json({"jobs": jobs, "skipped": skipped}, HTTPStatus.ACCEPTED)

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
    return ThreadingHTTPServer((host, port), BoundHandler)


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


if __name__ == "__main__":
    main()
