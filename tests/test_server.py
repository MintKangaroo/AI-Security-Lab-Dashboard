from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from lab_dashboard.server import Dashboard, _safe_project_path, create_server, git_status


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


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

    overview = Dashboard(config_path=config_path).overview()

    assert overview["summary"]["total"] == 1
    assert overview["projects"][0]["id"] == "sample"
    assert overview["projects"][0]["available_actions"] == ["test"]
    assert overview["projects"][0]["health"]["state"] == "not_configured"


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
    dashboard = Dashboard(config_path=config_path, lab_root=lab_root)
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
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
