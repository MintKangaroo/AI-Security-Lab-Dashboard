from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from lab_dashboard.server import (
    ACCENT_PATTERN,
    DEFAULT_CONFIG,
    MAX_JOB_HISTORY,
    MAX_STATUS_METRICS,
    RUNTIME_ENV_VAR,
    STATUS_SCHEMA,
    Dashboard,
    Job,
    JobConflictError,
    JobManager,
    _safe_project_path,
    check_health,
    create_server,
    git_status,
    read_status,
    resolve_runtime_root,
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


def _bulk_dashboard(
    tmp_path: Path, *, project_ids: tuple[str, ...] = ("alpha", "beta")
) -> Dashboard:
    lab_root = tmp_path / "lab"
    projects = []
    for project_id in project_ids:
        (lab_root / project_id).mkdir(parents=True)
        projects.append(
            {
                "id": project_id,
                "name": project_id.title(),
                "path": project_id,
                "ports": [],
                "actions": {"test": [sys.executable, "-c", "print('ok')"]},
            }
        )
    config_path = tmp_path / "projects.json"
    config_path.write_text(json.dumps({"projects": projects}), encoding="utf-8")
    return Dashboard(
        config_path=config_path,
        lab_root=lab_root,
        jobs=JobManager(runtime_root=tmp_path / "runtime"),
    )


@contextmanager
def _running_dashboard(dashboard: Dashboard) -> Iterator[str]:
    server = create_server("127.0.0.1", 0, dashboard)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        dashboard.close()


def _post(
    url: str,
    payload: dict[str, object] | None,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload if payload is not None else {}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Lab-Dashboard": "1",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_bulk_action_queues_a_job_for_every_selected_project(tmp_path: Path) -> None:
    dashboard = _bulk_dashboard(tmp_path)
    with _running_dashboard(dashboard) as base_url:
        status, body = _post(
            f"{base_url}/api/actions/bulk",
            {"project_ids": ["alpha", "beta"], "action": "test"},
        )
        assert status == 202
        assert {job["project_id"] for job in body["jobs"]} == {"alpha", "beta"}
        assert body["skipped"] == []
        assert body["conflicts"] == []
        assert all("log_path" not in job and "command" not in job for job in body["jobs"])
        for job in body["jobs"]:
            _wait_for(lambda job_id=job["id"]: dashboard.jobs.get(job_id).status == "succeeded")


def test_bulk_action_runs_a_repeated_project_once(tmp_path: Path) -> None:
    """The same id twice is a selection artefact, not a request for two runs --
    and the second submit would collide with the first on the project lock."""
    dashboard = _bulk_dashboard(tmp_path)
    with _running_dashboard(dashboard) as base_url:
        status, body = _post(
            f"{base_url}/api/actions/bulk",
            {"project_ids": ["alpha", "alpha"], "action": "test"},
        )
        assert status == 202
        assert len(body["jobs"]) == 1
        assert body["conflicts"] == []
        _wait_for(lambda: dashboard.jobs.get(body["jobs"][0]["id"]).status == "succeeded")


def test_bulk_action_separates_skipped_projects_from_the_jobs_it_started(tmp_path: Path) -> None:
    dashboard = _bulk_dashboard(tmp_path)
    with _running_dashboard(dashboard) as base_url:
        status, body = _post(
            f"{base_url}/api/actions/bulk",
            {"project_ids": ["alpha", "ghost"], "action": "test"},
        )
        assert status == 202
        assert [job["project_id"] for job in body["jobs"]] == ["alpha"]
        assert body["skipped"] == ["ghost"]
        _wait_for(lambda: dashboard.jobs.get(body["jobs"][0]["id"]).status == "succeeded")


def test_bulk_action_skips_a_project_whose_directory_is_missing(tmp_path: Path) -> None:
    dashboard = _bulk_dashboard(tmp_path)
    (tmp_path / "lab" / "beta").rmdir()
    with _running_dashboard(dashboard) as base_url:
        status, body = _post(
            f"{base_url}/api/actions/bulk",
            {"project_ids": ["alpha", "beta"], "action": "test"},
        )
        assert status == 202
        assert [job["project_id"] for job in body["jobs"]] == ["alpha"]
        assert body["skipped"] == ["beta"]
        _wait_for(lambda: dashboard.jobs.get(body["jobs"][0]["id"]).status == "succeeded")


def test_bulk_action_reports_a_busy_project_as_a_conflict(tmp_path: Path) -> None:
    dashboard = _bulk_dashboard(tmp_path)
    blocker = dashboard.jobs.submit(
        "beta",
        "Beta",
        "test",
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path / "lab" / "beta",
    )
    with _running_dashboard(dashboard) as base_url:
        status, body = _post(
            f"{base_url}/api/actions/bulk",
            {"project_ids": ["alpha", "beta"], "action": "test"},
        )
        assert status == 202
        assert [job["project_id"] for job in body["jobs"]] == ["alpha"]
        assert [conflict["project_id"] for conflict in body["conflicts"]] == ["beta"]
        assert body["conflicts"][0]["reason"]
        assert dashboard.jobs.get(blocker.id).status == "running"


def test_bulk_action_fails_when_no_project_can_run(tmp_path: Path) -> None:
    dashboard = _bulk_dashboard(tmp_path)
    with _running_dashboard(dashboard) as base_url:
        status, body = _post(
            f"{base_url}/api/actions/bulk",
            {"project_ids": ["ghost", "phantom"], "action": "test"},
        )
        assert status == 409
        assert body["error"]
        assert dashboard.jobs.list() == []


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "test"},
        {"project_ids": [], "action": "test"},
        {"project_ids": "alpha", "action": "test"},
        {"project_ids": ["alpha", 7], "action": "test"},
        {"project_ids": ["alpha", "beta", "gamma"], "action": "test"},
        {"project_ids": ["alpha"], "action": "deploy"},
        {"project_ids": ["alpha"]},
    ],
)
def test_bulk_action_rejects_a_malformed_payload(tmp_path: Path, payload: dict) -> None:
    dashboard = _bulk_dashboard(tmp_path)
    with _running_dashboard(dashboard) as base_url:
        status, body = _post(f"{base_url}/api/actions/bulk", payload)
        assert status == 400
        assert body["error"]
        assert dashboard.jobs.list() == []


@pytest.mark.parametrize("origin", ["http://127.0.0.1:4173", "http://localhost:4173"])
def test_mutation_is_accepted_from_the_local_dashboard_origin(tmp_path: Path, origin: str) -> None:
    dashboard = _bulk_dashboard(tmp_path, project_ids=("alpha",))
    with _running_dashboard(dashboard) as base_url:
        status, body = _post(
            f"{base_url}/api/projects/alpha/actions/test",
            {},
            headers={"Origin": origin},
        )
        assert status == 202
        _wait_for(lambda: dashboard.jobs.get(body["job"]["id"]).status == "succeeded")


@pytest.mark.parametrize(
    "origin", ["http://evil.example", "https://127.0.0.1.evil.example", "null"]
)
def test_mutation_is_rejected_from_a_foreign_origin(tmp_path: Path, origin: str) -> None:
    """The custom header alone cannot be forged cross-origin, but a browser that
    does send one has to carry an Origin the dashboard recognises."""
    dashboard = _bulk_dashboard(tmp_path, project_ids=("alpha",))
    with _running_dashboard(dashboard) as base_url:
        status, body = _post(
            f"{base_url}/api/projects/alpha/actions/test",
            {},
            headers={"Origin": origin},
        )
        assert status == 403
        assert body["error"]
        assert dashboard.jobs.list() == []


def test_mutation_is_rejected_without_the_dashboard_header(tmp_path: Path) -> None:
    dashboard = _bulk_dashboard(tmp_path, project_ids=("alpha",))
    with _running_dashboard(dashboard) as base_url:
        request = urllib.request.Request(
            f"{base_url}/api/actions/bulk",
            data=b'{"project_ids": ["alpha"], "action": "test"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 403
        assert dashboard.jobs.list() == []


@contextmanager
def _health_endpoint(body: str, status: int = 200) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/health"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_check_health_reports_a_project_without_an_endpoint() -> None:
    assert check_health(None, None) == {"state": "not_configured", "latency_ms": None, "url": None}


def test_check_health_reports_a_responding_endpoint() -> None:
    with _health_endpoint('{"service": "alpha", "status": "live"}') as url:
        result = check_health(url, "alpha")
    assert result["state"] == "online"
    assert result["url"] == url
    assert isinstance(result["latency_ms"], int)


def test_check_health_flags_a_port_held_by_another_service() -> None:
    """A neighbouring project on the same port answers 200; the marker string is
    what separates 'my service is up' from 'something else owns this port'."""
    with _health_endpoint('{"service": "someone-else"}') as url:
        result = check_health(url, "alpha")
    assert result["state"] == "occupied"


def test_check_health_reports_a_refusing_endpoint_as_offline() -> None:
    with _health_endpoint("{}") as url:
        pass  # The server is closed on exit, so the port now refuses connections.
    assert check_health(url, None) == {"state": "offline", "latency_ms": None, "url": url}


def _finished_job(index: int) -> Job:
    return Job(
        id=f"job{index:04d}",
        project_id="alpha",
        project_name="Alpha",
        action="test",
        command=["dashboard", "historical-job"],
        status="succeeded",
        created_at=f"2026-01-01T00:00:{index:02d}Z",
        started_at=f"2026-01-01T00:00:{index:02d}Z",
        finished_at=f"2026-01-01T00:00:{index:02d}Z",
        exit_code=0,
    )


def _seed_history(manager: JobManager, jobs: list[Job]) -> None:
    for job in jobs:
        job.log_path = str(manager.runtime_root / f"{job.id}.log")
        Path(job.log_path).write_text(f"log for {job.id}\n", encoding="utf-8")
        manager._jobs[job.id] = job
    with manager._lock:
        manager._persist_locked()


def test_job_history_is_capped_and_evicted_logs_are_deleted(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    manager = JobManager(runtime_root=runtime_root)
    overflow = 10
    try:
        _seed_history(manager, [_finished_job(i) for i in range(MAX_JOB_HISTORY + overflow)])
        assert len(manager.list()) == MAX_JOB_HISTORY
        records = json.loads((runtime_root / "jobs.json").read_text(encoding="utf-8"))
        assert len(records) == MAX_JOB_HISTORY

        surviving = {record["id"] for record in records}
        assert f"job{MAX_JOB_HISTORY + overflow - 1:04d}" in surviving
        assert "job0000" not in surviving
        assert {path.stem for path in runtime_root.glob("*.log")} == surviving
    finally:
        manager.shutdown()


def test_pruning_keeps_an_unfinished_job_even_when_it_is_the_oldest(tmp_path: Path) -> None:
    """Dropping a running job would strand its process with no record of it."""
    runtime_root = tmp_path / "runtime"
    manager = JobManager(runtime_root=runtime_root)
    running = _finished_job(0)
    running.status = "running"
    running.finished_at = None
    try:
        _seed_history(manager, [running, *(_finished_job(i) for i in range(1, 80))])
        assert manager.get(running.id) is not None
        assert (runtime_root / f"{running.id}.log").is_file()
        listed = manager.list()
        assert len(listed) == MAX_JOB_HISTORY
        # It is the oldest job of the lot, so a plain newest-first cut would
        # drop it and the browser would stop polling for job updates.
        assert running.id in {job["id"] for job in listed}
    finally:
        manager.shutdown()


def test_a_restart_drops_history_beyond_the_cap(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    first = JobManager(runtime_root=runtime_root)
    _seed_history(first, [_finished_job(i) for i in range(MAX_JOB_HISTORY)])
    first.shutdown()
    # A history file written by an older build, before the cap existed.
    (runtime_root / "jobs.json").write_text(
        json.dumps([asdict(_finished_job(i)) for i in range(MAX_JOB_HISTORY + 25)]),
        encoding="utf-8",
    )

    second = JobManager(runtime_root=runtime_root)
    try:
        assert len(second.list()) == MAX_JOB_HISTORY
        assert second.get("job0000") is None
        assert second.get(f"job{MAX_JOB_HISTORY + 24:04d}") is not None
    finally:
        second.shutdown()


def test_a_restart_removes_a_log_no_retained_job_points_at(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    first = JobManager(runtime_root=runtime_root)
    _seed_history(first, [_finished_job(0)])
    first.shutdown()
    orphan = runtime_root / "deadbeef1234.log"
    orphan.write_text("left over\n", encoding="utf-8")

    second = JobManager(runtime_root=runtime_root)
    try:
        assert not orphan.exists()
        assert (runtime_root / "job0000.log").is_file()
    finally:
        second.shutdown()


def test_runtime_root_prefers_an_explicit_path(tmp_path: Path) -> None:
    assert resolve_runtime_root(tmp_path / "chosen") == tmp_path / "chosen"


def test_runtime_root_honours_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUNTIME_ENV_VAR, str(tmp_path / "from-env"))
    assert resolve_runtime_root() == tmp_path / "from-env"


def test_runtime_root_uses_the_checkout_it_is_running_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(RUNTIME_ENV_VAR, raising=False)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr("lab_dashboard.server.REPO_ROOT", tmp_path)
    assert resolve_runtime_root() == tmp_path / ".runtime"


def test_runtime_root_avoids_the_install_directory_outside_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed as a console script, REPO_ROOT is whatever sits above
    site-packages -- usually the virtualenv, which is no place for state."""
    monkeypatch.delenv(RUNTIME_ENV_VAR, raising=False)
    site_packages_parent = tmp_path / "venv" / "lib"
    site_packages_parent.mkdir(parents=True)
    monkeypatch.setattr("lab_dashboard.server.REPO_ROOT", site_packages_parent)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    resolved = resolve_runtime_root()
    assert resolved == tmp_path / "state" / "ai-security-lab-dashboard"
    assert site_packages_parent not in resolved.parents


def test_dashboard_hands_its_runtime_root_to_the_job_manager(tmp_path: Path) -> None:
    lab_root = tmp_path / "lab"
    (lab_root / "sample").mkdir(parents=True)
    config_path = tmp_path / "projects.json"
    config_path.write_text(
        json.dumps({"projects": [{"id": "sample", "name": "S", "path": "sample", "ports": []}]}),
        encoding="utf-8",
    )
    dashboard = Dashboard(
        config_path=config_path, lab_root=lab_root, runtime_root=tmp_path / "runtime"
    )
    try:
        assert dashboard.jobs.runtime_root == tmp_path / "runtime"
        assert (tmp_path / "runtime").is_dir()
    finally:
        dashboard.close()

