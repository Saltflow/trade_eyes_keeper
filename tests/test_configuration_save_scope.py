"""Exercise configuration-only commits in disposable real Git repositories."""

import subprocess

import pytest

from src.core.config_store import save_configuration


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Offline acceptance")
    git(tmp_path, "config", "user.email", "acceptance@example.invalid")
    git(tmp_path, "config", "core.autocrlf", "false")
    path = tmp_path / "config/config.yaml"
    path.parent.mkdir()
    path.write_text("stocks: ['600036']\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("before\n", encoding="utf-8")
    git(tmp_path, "add", "config/config.yaml", "other.txt")
    git(tmp_path, "commit", "-qm", "fixture baseline", "--no-verify")
    return tmp_path, path


def stage_other(root):
    (root / "other.txt").write_text("someone else's staged change\n", encoding="utf-8")
    git(root, "add", "other.txt")
    return git(root, "rev-parse", ":other.txt")


def test_save_commits_only_configuration_and_preserves_other_index(repository):
    root, path = repository
    other_blob = stage_other(root)
    path.write_text("stocks: ['600036', '00883']\n", encoding="utf-8")
    response = save_configuration(path)
    assert response.startswith("✅")
    assert git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == "config/config.yaml"
    assert git(root, "show", "HEAD:config/config.yaml") == "stocks: ['600036', '00883']"
    assert git(root, "show", "HEAD:other.txt") == "before"
    assert git(root, "diff", "--cached", "--name-only") == "other.txt"
    assert git(root, "rev-parse", ":other.txt") == other_blob


def test_unchanged_configuration_never_commits_other_staged_changes(repository):
    root, path = repository
    original_head = git(root, "rev-parse", "HEAD")
    other_blob = stage_other(root)
    response = save_configuration(path)
    assert git(root, "rev-parse", "HEAD") == original_head
    assert git(root, "diff", "--cached", "--name-only") == "other.txt"
    assert git(root, "rev-parse", ":other.txt") == other_blob
    assert "已保存" not in response


def test_git_failure_does_not_claim_saved(repository):
    root, path = repository
    original_head = git(root, "rev-parse", "HEAD")
    path.write_text("stocks: ['00883']\n", encoding="utf-8")
    # A Git-owned lock causes a genuine failure without affecting the main repo.
    lock = root / ".git/index.lock"
    lock.write_text("fixture lock", encoding="utf-8")
    try:
        response = save_configuration(path)
        assert response.startswith("❌")
        assert "已保存" not in response
        assert git(root, "rev-parse", "HEAD") == original_head
        assert path.read_text(encoding="utf-8") == "stocks: ['00883']\n"
    finally:
        lock.unlink()
