"""Repository navigation uses text fixtures and never imports business modules."""

import hashlib
import json
import os
import time

import pytest

from src.interactive.assistant import repository as module
from src.interactive.assistant.repository import RepositoryKnowledge


@pytest.fixture
def repo(tmp_path):
    return RepositoryKnowledge(tmp_path)


def write(repo, name, text):
    path = repo.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_map_lists_source_modules_examples_and_explicit_exclusions(repo):
    for name in (
        "src/data/fetch.py",
        "src/core/run.py",
        "scripts/run.sh",
        "tests/test_run.py",
        "docs/guide.md",
        "config/config.yaml.example",
        "main.py",
        "requirements-dev.txt",
    ):
        write(repo, name, "# valid source\n")
    for name in (
        "data/secret.txt",
        "cache/data.py",
        "logs/log.txt",
        "docs/development/log.md",
        "docs/archive/notes.md",
        "config/config.yaml",
        "config/.env",
        "src/image.png",
        "reports/test.md",
        ".git/config",
        "src/__pycache__/code.py",
        "src/config.yaml",
    ):
        write(repo, name, "hidden-runtime-secret")
    result = repo.map()
    assert result["total_files"] == 8
    assert result["groups"]["src/data"]["files"] == 1
    assert result["groups"]["config"]["files"] == 1
    assert result["excluded"]
    assert result["truncated"] is False
    assert repo.search("hidden-runtime-secret")["results"] == []


def test_list_paging_prefix_and_new_files_are_discovered(repo):
    first = write(repo, "src/a.py", "first\n")
    write(repo, "src/sub/b.py", "second\n")
    write(repo, "tests/a.py", "third\n")
    page = repo.list_files("src", limit=1)
    assert page["total"] == 2
    assert page["files"] == [{"path": "src/a.py", "size": first.stat().st_size}]
    assert page["next_offset"] == 1
    assert repo.list_files("src/", offset=1)["files"][0]["path"] == "src/sub/b.py"
    write(repo, "src/new.py", "new\n")
    assert repo.list_files("src")["total"] == 3
    assert repo.list_files("src", offset=100)["next_offset"] is None


def test_read_reports_lines_hash_and_observes_updated_content(repo):
    path = write(repo, "src/core.py", "first\nsecond\nthird\n")
    old = repo.read("src/core.py", 2, 1)
    assert old["text"] == "second"
    assert old["citation"] == "src/core.py:L2-L2"
    assert old["next_start_line"] == 3
    repo.search("second")
    path.write_text("first\nchanged\nthird\n", encoding="utf-8")
    assert repo.search("changed")["results"][0]["path"] == "src/core.py"
    fresh = repo.read("src/core.py", 2, 1)
    assert fresh["text"] == "changed"
    assert fresh["sha256"] != old["sha256"]
    assert fresh["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_read_refreshes_even_if_size_and_mtime_are_preserved(repo):
    path = write(repo, "src/core.py", "first")
    before = path.stat()
    old = repo.read("src/core.py")
    path.write_text("other", encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    fresh = repo.read("src/core.py")
    assert fresh["text"] == "other"
    assert fresh["sha256"] != old["sha256"]


def test_search_chinese_english_symbols_and_literal_not_regex(repo):
    write(
        repo,
        "src/core.py",
        'def prepare_backtest_data():\n    """准备行情与财报回测数据。"""\n    return 1\n',
    )
    write(repo, "docs/guide.md", "# Documentation\nSolver optimization examples\n")
    assert repo.search("准备行情")["results"][0]["path"] == "src/core.py"
    assert repo.search("PREPARE_BACKTEST_DATA")["results"][0]["path"] == "src/core.py"
    assert repo.search("solver optimization")["results"][0]["path"] == "docs/guide.md"
    assert repo.search(".*")["results"] == []
    assert repo.search("prepare_backtest_data", prefix="docs")["results"] == []


def test_credentials_hidden_in_source_and_preserve_line_numbers(repo):
    text = (
        "token = 'private-token-value'\n"
        "PASSWORD: str = 'private-password-value'\n"
        'mapping = {"client_secret": "private-secret-value"}\n'
        "API_KEY = 'sk-abcdefghijklmnopqrstuvwx'\n"
        "# Bearer private-bearer-value\n"
        "# https://username:private-url-value@example.invalid/path\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "private-key-payload\n"
        "-----END PRIVATE KEY-----\n"
        "def visible_symbol(): pass\n"
    )
    write(repo, "src/secrets_example.py", text)
    result = repo.read("src/secrets_example.py")
    for secret in (
        "private-token-value",
        "private-password-value",
        "private-secret-value",
        "sk-abcdefghijklmnopqrstuvwx",
        "private-bearer-value",
        "private-url-value",
        "private-key-payload",
    ):
        assert secret not in result["text"]
    assert result["total_lines"] == 10
    assert (
        repo.read("src/secrets_example.py", 10, 1)["text"]
        == "def visible_symbol(): pass"
    )


def test_multiline_credential_literals_keep_line_count_and_hide_values(repo):
    text = (
        "TOKEN = (\n'parenthesized-sensitive-value'\n)\n"
        'PASSWORD = """\ntriple-sensitive-value\n"""\n'
        'mapping = {\n"client_secret":\n"dict-sensitive-value"\n}\n'
        "def visible(): pass\n"
    )
    write(repo, "src/multiline.py", text)
    result = repo.read("src/multiline.py")
    assert result["total_lines"] == len(text.splitlines())
    assert "sensitive-value" not in result["text"]
    assert result["text"].endswith("def visible(): pass")


@pytest.mark.parametrize(
    "name",
    [
        "../README.md",
        "src/../../config/.env",
        "src\\core.py",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "src//file.py",
        "src/./file.py",
        "config/config.yaml",
        "src/private.yaml",
        "src/.private.py",
        "docs/development/log.md",
        "data/file.py",
        "README.md\x00",
        None,
    ],
)
def test_rejected_paths_do_not_read_outside_approved_source(repo, name):
    with pytest.raises((ValueError, FileNotFoundError)):
        repo.read(name)


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("list_files", {"limit": True}),
        ("list_files", {"limit": 101}),
        ("list_files", {"offset": -1}),
        ("list_files", {"prefix": "../"}),
        ("search", {"query": ""}),
        ("search", {"query": "x" * 257}),
        ("search", {"query": "x", "limit": 9}),
        ("search", {"query": "x", "limit": True}),
        ("read", {"path": "README.md", "start_line": 0}),
        ("read", {"path": "README.md", "line_count": True}),
        ("read", {"path": "README.md", "line_count": 121}),
        ("symbols", {"path": "README.md"}),
        ("symbols", {"path": "src/a.py", "limit": 81}),
    ],
)
def test_bad_parameters_fail_before_reading(repo, method, kwargs):
    with pytest.raises(ValueError):
        getattr(repo, method)(**kwargs)


def test_binary_oversized_line_count_and_output_bounds(repo, monkeypatch):
    write(repo, "src/binary.py", "x\x00y")
    with pytest.raises(ValueError, match="二进制"):
        repo.read("src/binary.py")
    write(repo, "src/large.py", "x" * (module.MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="MiB"):
        repo.read("src/large.py")
    write(repo, "src/line.py", "x" * 10000)
    assert len(repo.read("src/line.py")["text"]) <= 6000
    assert repo.search("x")["truncated"] is True
    monkeypatch.setattr(module, "MAX_FILE_LINES", 2)
    write(repo, "src/many_lines.py", "one\ntwo\nthree\n")
    with pytest.raises(ValueError, match="行数"):
        repo.read("src/many_lines.py")


def test_truncation_reports_file_byte_entry_and_deadline_limits(repo, monkeypatch):
    write(repo, "src/a.py", "# first solver\n")
    write(repo, "src/b.py", "# second solver\n")
    monkeypatch.setattr(module, "MAX_FILES", 1)
    assert repo.list_files()["truncated"] is True
    assert repo.map()["total_files"] == 1
    monkeypatch.setattr(module, "MAX_FILES", 5000)
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 1)
    assert repo.map()["truncated"] is True
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(module, "MAX_ENTRIES", 1)
    assert repo.map()["truncated"] is True
    monkeypatch.setattr(module, "MAX_ENTRIES", 20000)
    monkeypatch.setattr(module, "MAX_SEARCH_SECONDS", -1)
    result = repo.search("solver")
    assert result["truncated"] is True
    assert result["scanned_files"] == 0
    assert "不能证明" in result["notice"]


def test_cache_is_bounded_and_deleted_files_are_removed(repo, monkeypatch):
    monkeypatch.setattr(module, "MAX_CACHE_BYTES", 20)
    first = write(repo, "src/a.py", "# first solver\n")
    write(repo, "src/b.py", "# second solver\n")
    repo.search("solver")
    assert sum(value[0][0] for value in repo._cache.values()) <= 20
    first.unlink()
    assert repo.list_files()["total"] == 1
    assert "src/a.py" not in repo._cache


def test_symbols_parse_without_execution_and_paginate_nested_names(repo):
    write(
        repo,
        "src/module.py",
        (
            "raise RuntimeError('MUST NOT EXECUTE')\n"
            "class Runner:\n"
            '    """Runner docs."""\n'
            "    async def execute(self):\n"
            '        """token=example-secret"""\n'
            "        def nested():\n"
            "            return 1\n"
            "        return nested()\n"
            "def outside():\n"
            "    return 2\n"
        ),
    )
    first = repo.symbols("src/module.py", limit=2)
    assert [value["name"] for value in first["symbols"]] == ["Runner", "Runner.execute"]
    assert first["symbols"][0]["start_line"] == 2
    assert first["symbols"][0]["end_line"] == 8
    assert "example-secret" not in json.dumps(first)
    assert first["next_offset"] == 2
    second = repo.symbols("src/module.py", offset=2)
    assert [value["name"] for value in second["symbols"]] == [
        "Runner.execute.nested",
        "outside",
    ]
    assert second["next_offset"] is None


def test_syntax_errors_are_clear_and_source_stays_readable(repo):
    write(repo, "src/broken.py", "def broken(\n")
    with pytest.raises(ValueError, match="语法"):
        repo.symbols("src/broken.py")
    assert repo.read("src/broken.py")["text"] == "def broken("


def test_hardlinked_files_are_rejected_for_listing_search_and_read(repo, tmp_path):
    target = write(repo, "private.txt", "private-hardlink-secret")
    destination = repo.root / "src/linked.py"
    destination.parent.mkdir()
    os.link(target, destination)
    with pytest.raises(ValueError, match="硬链接"):
        repo.read("src/linked.py")
    assert repo.list_files()["files"] == []
    assert repo.search("private-hardlink-secret")["results"] == []


@pytest.mark.parametrize("directory", [False, True])
def test_symlinked_files_and_directories_are_rejected(repo, directory):
    target = write(repo, "private/linked.py", "private-symlink-secret")
    link = repo.root / "src"
    if not directory:
        link.mkdir()
        link = link / "linked.py"
    try:
        link.symlink_to(
            target.parent if directory else target, target_is_directory=directory
        )
    except OSError as exc:
        pytest.skip(f"OS does not permit symlink fixtures: {exc}")
    with pytest.raises(ValueError, match="符号链接|重定向"):
        repo.read("src/linked.py")
    assert repo.list_files()["files"] == []


def test_large_single_line_search_completes_within_small_budget(repo):
    write(repo, "src/long.py", "x" * module.MAX_FILE_BYTES)
    started = time.monotonic()
    result = repo.search("unmatched-symbol")
    assert time.monotonic() - started < 3.0
    assert result["results"] == []


def test_source_templates_are_readable_without_opening_runtime_reports(repo):
    for suffix in ("html", "jinja", "j2", "tex"):
        name = f"src/templates/report.{suffix}"
        write(repo, name, "template-render-source")
        assert repo.read(name)["text"] == "template-render-source"
    write(repo, "reports/result.html", "runtime-private-value")
    write(repo, "docs/report.html", "runtime-private-value")
    assert len(repo.list_files("src/templates")["files"]) == 4
    for name in ("reports/result.html", "docs/report.html"):
        with pytest.raises(ValueError):
            repo.read(name)


def test_config_python_and_deploy_entry_are_readable_actual_settings_are_not(repo):
    write(repo, "config/config.py", "def load_config(): pass")
    write(repo, "ci_cd_deploy.py", "def deploy(): pass")
    assert repo.symbols("config/config.py")["symbols"][0]["name"] == "load_config"
    assert repo.read("ci_cd_deploy.py")["text"] == "def deploy(): pass"
    for name in ("config/config.yaml", "config/.env", "config/private.json"):
        write(repo, name, "credential-private-value")
        with pytest.raises(ValueError):
            repo.read(name)
    assert {row["path"] for row in repo.list_files()["files"]} == {
        "config/config.py",
        "ci_cd_deploy.py",
    }


def test_only_feishu_pi_tool_sources_and_manifests_are_opened(repo):
    allowed = [
        "tools/feishu-pi/bridge.mjs",
        "tools/feishu-pi/helper.js",
        "tools/feishu-pi/README.md",
        "tools/feishu-pi/package.json",
        "tools/feishu-pi/package-lock.json",
    ]
    forbidden = [
        "tools/other/main.js",
        "tools/feishu-pi/node_modules/module/index.js",
        "tools/feishu-pi/.env",
        "tools/feishu-pi/runtime.yaml",
        "tools/feishu-pi/private.py",
    ]
    for name in allowed:
        write(repo, name, "approved bridge source")
        assert repo.read(name)["text"] == "approved bridge source"
    for name in forbidden:
        write(repo, name, "forbidden runtime data")
        with pytest.raises(ValueError):
            repo.read(name)
    assert {row["path"] for row in repo.list_files("tools")["files"]} == set(allowed)
    with pytest.raises(ValueError):
        repo.list_files("tools/other")
