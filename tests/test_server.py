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
    ACCENT_PATTERN,
    DEFAULT_CONFIG,
    MAX_STATUS_METRICS,
    STATUS_SCHEMA,
    Dashboard,
    JobConflictError,
    JobManager,
    _safe_project_path,
    create_server,
    git_status,
    read_status,
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


def _write_status(project: Path, document: object) -> None:
    path = project / ".runtime" / "status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _status_document(**overrides: object) -> dict[str, object]:
    document = {
        "schema": STATUS_SCHEMA,
        "generated_at": "2026-01-01T00:00:00Z",
        "state": "ok",
        "headline": "8/8 techniques covered",
        "last_run_at": "2026-01-01T00:00:00Z",
        "metrics": [{"label": "runs", "value": "3"}],
    }
    document.update(overrides)
    return document


def test_read_status_returns_none_without_a_configured_file(tmp_path: Path) -> None:
    assert read_status(tmp_path, None) is None


def test_read_status_reads_a_published_document(tmp_path: Path) -> None:
    _write_status(tmp_path, _status_document())
    status = read_status(tmp_path, ".runtime/status.json")
    assert status["state"] == "ok"
    assert status["metrics"] == [{"label": "runs", "value": "3"}]


def test_read_status_reports_a_project_that_never_ran(tmp_path: Path) -> None:
    status = read_status(tmp_path, ".runtime/status.json")
    assert status["state"] == "unknown"
    assert status["metrics"] == []


@pytest.mark.parametrize("relative", ["../outside.json", "/etc/passwd", "."])
def test_read_status_refuses_a_path_outside_the_project(tmp_path: Path, relative: str) -> None:
    status = read_status(tmp_path, relative)
    assert status["state"] == "error"
    assert "inside the project" in status["headline"]


def test_read_status_refuses_a_symlink_that_escapes_the_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".runtime").mkdir(parents=True)
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(_status_document()), encoding="utf-8")
    (project / ".runtime" / "status.json").symlink_to(secret)
    assert read_status(project, ".runtime/status.json")["state"] == "error"


def test_read_status_rejects_an_unknown_schema(tmp_path: Path) -> None:
    _write_status(tmp_path, _status_document(schema="something-else/9"))
    status = read_status(tmp_path, ".runtime/status.json")
    assert status["state"] == "error"
    assert STATUS_SCHEMA in status["headline"]


@pytest.mark.parametrize("payload", ["not json at all", '["a list"]'])
def test_read_status_survives_a_corrupt_file(tmp_path: Path, payload: str) -> None:
    path = tmp_path / ".runtime" / "status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    assert read_status(tmp_path, ".runtime/status.json")["state"] == "error"


def test_read_status_treats_the_document_as_untrusted(tmp_path: Path) -> None:
    """A project writes this file itself, so nothing in it is taken on faith."""
    _write_status(
        tmp_path,
        _status_document(
            state="catastrophic",
            headline="h" * 500,
            metrics=[{"label": "l" * 200, "value": "v" * 200}]
            + [{"label": f"m{index}", "value": "1"} for index in range(50)]
            + ["not a metric", {"label": "no value"}],
        ),
    )
    status = read_status(tmp_path, ".runtime/status.json")
    assert status["state"] == "unknown"
    assert len(status["headline"]) == 120
    assert len(status["metrics"]) == MAX_STATUS_METRICS
    assert len(status["metrics"][0]["label"]) == 40
    assert len(status["metrics"][0]["value"]) == 80


def test_read_status_ignores_an_oversized_file(tmp_path: Path) -> None:
    _write_status(tmp_path, _status_document(headline="x" * 100_000))
    status = read_status(tmp_path, ".runtime/status.json")
    assert status["state"] == "error"
    assert "too large" in status["headline"]


def _status_dashboard(tmp_path: Path, status_file: str | None) -> Dashboard:
    lab_root = tmp_path / "lab"
    (lab_root / "sample").mkdir(parents=True)
    project: dict[str, object] = {
        "id": "sample",
        "name": "Sample",
        "path": "sample",
        "ports": [],
        "actions": {"test": ["python3", "-c", "print('ok')"]},
    }
    if status_file is not None:
        project["status_file"] = status_file
    config_path = tmp_path / "projects.json"
    config_path.write_text(json.dumps({"projects": [project]}), encoding="utf-8")
    return Dashboard(
        config_path=config_path,
        lab_root=lab_root,
        jobs=JobManager(runtime_root=tmp_path / "runtime"),
    )


def test_overview_surfaces_a_published_status(tmp_path: Path) -> None:
    dashboard = _status_dashboard(tmp_path, ".runtime/status.json")
    _write_status(tmp_path / "lab" / "sample", _status_document())
    try:
        project = dashboard.overview()["projects"][0]
        assert project["status"]["headline"] == "8/8 techniques covered"
        assert "status_file" not in project
    finally:
        dashboard.close()


def test_overview_leaves_status_null_for_projects_that_publish_none(tmp_path: Path) -> None:
    dashboard = _status_dashboard(tmp_path, None)
    try:
        assert dashboard.overview()["projects"][0]["status"] is None
    finally:
        dashboard.close()


@pytest.mark.parametrize("status_file", ["../escape.json", "", 7])
def test_dashboard_rejects_an_unsafe_status_file_at_startup(
    tmp_path: Path, status_file: object
) -> None:
    with pytest.raises(ValueError):
        _status_dashboard(tmp_path, status_file)  # type: ignore[arg-type]


def _accent_dashboard(tmp_path: Path, accent: object) -> Dashboard:
    lab_root = tmp_path / "lab"
    (lab_root / "sample").mkdir(parents=True)
    project: dict[str, object] = {
        "id": "sample",
        "name": "Sample",
        "path": "sample",
        "ports": [],
        "accent": accent,
        "actions": {"test": ["python3", "-c", "print('ok')"]},
    }
    config_path = tmp_path / "projects.json"
    config_path.write_text(json.dumps({"projects": [project]}), encoding="utf-8")
    return Dashboard(
        config_path=config_path,
        lab_root=lab_root,
        jobs=JobManager(runtime_root=tmp_path / "runtime"),
    )


def test_every_configured_project_declares_an_accent() -> None:
    """The accent used to live in CSS, so a newly registered project rendered
    with an undefined custom property until someone remembered to add a rule."""
    config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    for project in config["projects"]:
        assert ACCENT_PATTERN.match(project["accent"]), project["id"]


@pytest.mark.parametrize(
    "accent", ["red", "#fff", "#12345g", "#ffb86b; background: url(x)", 0xFFB86B]
)
def test_dashboard_rejects_an_accent_it_cannot_put_in_a_style_attribute(
    tmp_path: Path, accent: object
) -> None:
    with pytest.raises(ValueError):
        _accent_dashboard(tmp_path, accent)


def test_overview_passes_the_accent_through(tmp_path: Path) -> None:
    dashboard = _accent_dashboard(tmp_path, "#ffb86b")
    try:
        assert dashboard.overview()["projects"][0]["accent"] == "#ffb86b"
    finally:
        dashboard.close()


def test_accent_css_covers_every_configured_project(tmp_path: Path) -> None:
    dashboard = _accent_dashboard(tmp_path, "#ffb86b")
    try:
        assert dashboard.accent_css() == (
            '[data-project-id="sample"] { --project-accent: #ffb86b; }\n'
        )
    finally:
        dashboard.close()


def test_shipped_config_gives_every_project_an_accent_rule() -> None:
    dashboard = Dashboard()
    try:
        css = dashboard.accent_css()
        for project in dashboard.projects:
            assert f'[data-project-id="{project["id"]}"]' in css
    finally:
        dashboard.close()


def test_accents_are_served_as_a_stylesheet_because_inline_styles_are_blocked(
    tmp_path: Path,
) -> None:
    """style-src 'self' drops a style attribute silently: it stays in the DOM
    and never applies. Accents therefore have to arrive as a real stylesheet."""
    dashboard = _accent_dashboard(tmp_path, "#ffb86b")
    server = create_server("127.0.0.1", 0, dashboard)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base_url}/accents.css") as response:
            body = response.read().decode("utf-8")
            assert response.headers["Content-Type"] == "text/css; charset=utf-8"
            policy = response.headers["Content-Security-Policy"]
        assert "--project-accent: #ffb86b" in body
        assert "style-src 'self'" in policy
        assert "unsafe-inline" not in policy
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        dashboard.close()


@pytest.mark.parametrize("project_id", ["Sample", 'a"] {}', "with space", 7])
def test_dashboard_rejects_a_project_id_it_cannot_put_in_a_selector(
    tmp_path: Path, project_id: object
) -> None:
    lab_root = tmp_path / "lab"
    (lab_root / "sample").mkdir(parents=True)
    config_path = tmp_path / "projects.json"
    config_path.write_text(
        json.dumps({"projects": [{"id": project_id, "name": "S", "path": "sample", "ports": []}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        Dashboard(
            config_path=config_path,
            lab_root=lab_root,
            jobs=JobManager(runtime_root=tmp_path / "runtime"),
        )
