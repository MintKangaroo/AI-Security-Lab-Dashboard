from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

from lab_dashboard.server import (
    Dashboard,
    JobConflictError,
    JobManager,
    _safe_project_path,
    create_server,
    git_status,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out while waiting for asynchronous job")


def test_safe_project_path_accepts_direct_child(tmp_path: Path) -> None:
    assert _safe_project_path(tmp_path, "project") == (tmp_path / "project").resolve()


@pytest.mark.parametrize("relative", ["../outside", "nested/project", "/tmp/outside"])
def test_safe_project_path_rejects_escape(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError):
        _safe_project_path(tmp_path, relative)


def test_git_status_reports_branch_and_worktree_changes(tmp_path: Path) -> None:
    project = tmp_path / "sample"
    project.mkdir()
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.email", "dashboard@example.test")
    _git(project, "config", "user.name", "Dashboard Test")
    tracked = project / "tracked.txt"
    tracked.write_text("initial\n", encoding="utf-8")
    _git(project, "add", "tracked.txt")
    _git(project, "commit", "-m", "initial")

    tracked.write_text("changed\n", encoding="utf-8")
    (project / "new.txt").write_text("new\n", encoding="utf-8")
    status = git_status(project)

    assert status["available"] is True
    assert status["branch"] == "main"
    assert status["modified"] == 1
    assert status["untracked"] == 1
    assert status["last_commit"]["subject"] == "initial"


def test_dashboard_overview_aggregates_projects(tmp_path: Path) -> None:
    lab_root = tmp_path / "lab"
    project = lab_root / "sample"
    project.mkdir(parents=True)
    config = {
        "lab_root": str(lab_root),
        "projects": [
            {
                "id": "sample",
                "name": "Sample",
                "path": "sample",
                "description": "Test project",
                "ports": [],
                "health_url": None,
                "actions": {"test": ["python3", "-c", "print('ok')"]},
            }
        ],
    }
    config_path = tmp_path / "projects.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    dashboard = Dashboard(
        config_path=config_path,
        jobs=JobManager(runtime_root=tmp_path / "runtime"),
    )
    try:
        overview = dashboard.overview()

        assert overview["summary"]["total"] == 1
        assert overview["projects"][0]["id"] == "sample"
        assert overview["projects"][0]["available_actions"] == ["test"]
        assert overview["projects"][0]["health"]["state"] == "not_configured"
    finally:
        dashboard.close()


def test_http_server_exposes_overview_and_protects_mutations(tmp_path: Path) -> None:
    lab_root = tmp_path / "lab"
    (lab_root / "sample").mkdir(parents=True)
    config_path = tmp_path / "projects.json"
    config_path.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "id": "sample",
                        "name": "Sample",
                        "path": "sample",
                        "ports": [],
                        "actions": {"test": ["python3", "-c", "print('ok')"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    dashboard = Dashboard(
        config_path=config_path,
        lab_root=lab_root,
        jobs=JobManager(runtime_root=tmp_path / "runtime"),
    )
    server = create_server("127.0.0.1", 0, dashboard)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        with urllib.request.urlopen(f"{base_url}/api/overview") as response:
            payload = json.load(response)
        assert response.status == 200
        assert payload["summary"]["total"] == 1

        request = urllib.request.Request(
            f"{base_url}/api/projects/sample/actions/test",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 403

        authorized_request = urllib.request.Request(
            f"{base_url}/api/projects/sample/actions/test",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Lab-Dashboard": "1",
            },
            method="POST",
        )
        with urllib.request.urlopen(authorized_request) as response:
            accepted = json.load(response)
        assert response.status == 202
        job_id = accepted["job"]["id"]
        _wait_for(lambda: dashboard.jobs.get(job_id).status == "succeeded")

        with urllib.request.urlopen(f"{base_url}/api/jobs/{job_id}/log") as response:
            log_payload = json.load(response)
        assert "ok" in log_payload["log"]
        assert "log_path" not in accepted["job"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        dashboard.close()


def test_job_manager_rejects_overlapping_project_jobs(tmp_path: Path) -> None:
    manager = JobManager(runtime_root=tmp_path / "runtime")
    try:
        job = manager.submit(
            "sample",
            "Sample",
            "test",
            [
                sys.executable,
                "-c",
                "import time; print('ready', flush=True); time.sleep(30)",
            ],
            tmp_path,
        )
        _wait_for(lambda: "ready" in (manager.read_log(job.id) or ""))

        with pytest.raises(JobConflictError, match="이미 작업"):
            manager.submit(
                "sample",
                "Sample",
                "test",
                [sys.executable, "-c", "print('duplicate')"],
                tmp_path,
            )
    finally:
        manager.shutdown()

    assert manager.get(job.id).status == "stopped"


def test_job_manager_starts_and_stops_managed_service(tmp_path: Path) -> None:
    manager = JobManager(runtime_root=tmp_path / "runtime")
    try:
        start_job = manager.submit(
            "service",
            "Managed Service",
            "start",
            [
                sys.executable,
                "-c",
                "import time; print('ready', flush=True); time.sleep(30)",
            ],
            tmp_path,
            managed=True,
        )
        _wait_for(lambda: "ready" in (manager.read_log(start_job.id) or ""))

        stop_job = manager.stop_managed("service", "Managed Service")
        _wait_for(lambda: manager.get(stop_job.id).status == "succeeded")
        _wait_for(lambda: manager.get(start_job.id).status == "stopped")

        assert manager.active_for("service") is None
        assert "Stopping managed service" in (manager.read_log(stop_job.id) or "")
    finally:
        manager.shutdown()


def test_job_history_survives_manager_restart(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    manager = JobManager(runtime_root=runtime_root)
    job = manager.submit(
        "sample",
        "Sample",
        "test",
        [sys.executable, "-c", "print('persisted')"],
        tmp_path,
    )
    _wait_for(lambda: manager.get(job.id).status == "succeeded")
    manager.shutdown()

    restored = JobManager(runtime_root=runtime_root)
    try:
        loaded = restored.get(job.id)
        assert loaded is not None
        assert loaded.status == "succeeded"
        assert loaded.project_id == "sample"
    finally:
        restored.shutdown()


def test_running_history_is_marked_interrupted_on_restart(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    manager = JobManager(runtime_root=runtime_root)
    job = manager.submit(
        "sample",
        "Sample",
        "test",
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
    )
    _wait_for(lambda: manager.get(job.id).status == "running")
    manager.shutdown()
    record = job.public()
    record["status"] = "running"
    record["finished_at"] = None
    (runtime_root / "jobs.json").write_text(json.dumps([record]), encoding="utf-8")

    restored = JobManager(runtime_root=runtime_root)
    try:
        loaded = restored.get(job.id)
        assert loaded is not None
        assert loaded.status == "interrupted"
        assert loaded.finished_at is not None
    finally:
        restored.shutdown()
