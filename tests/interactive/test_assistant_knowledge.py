"""Exercise document retrieval with local fixtures, without model/network calls."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.interactive.assistant.knowledge import (
    DOC_PATHS,
    DOCUMENT_NOTICE,
    MAX_BYTES,
    MAX_EXCERPT_CHARS,
    ProjectKnowledge,
)


@pytest.fixture
def knowledge(tmp_path):
    return ProjectKnowledge(tmp_path)


def write_doc(knowledge, name, text):
    target = knowledge.root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_chinese_search_finds_relevant_excerpt_and_citation(knowledge):
    write_doc(knowledge, "README.md", "# 项目概览\n这是股票监控项目。\n")
    write_doc(
        knowledge,
        "docs/configuration.md",
        "# 配置说明\n日报发送时间由调度配置控制。\n修改配置需要确认。\n",
    )
    result = knowledge.search("日报发送时间")
    assert result["results"][0]["path"] == "docs/configuration.md"
    assert "日报发送时间" in result["results"][0]["text"]
    assert result["results"][0]["citation"] == "docs/configuration.md:L1-L3"
    assert result["notice"] == DOCUMENT_NOTICE


def test_english_search_is_case_insensitive_and_prefers_matching_terms(knowledge):
    write_doc(knowledge, "README.md", "# Overview\nThe project monitors stocks.\n")
    write_doc(
        knowledge,
        "docs/architecture.md",
        "# Architecture\nThe solver evaluates Bayesian optimization candidates.\n",
    )
    result = knowledge.search("BAYESIAN optimization")
    assert [row["path"] for row in result["results"]] == ["docs/architecture.md"]
    assert "Bayesian optimization" in result["results"][0]["text"]


def test_search_no_match_still_lists_available_documents(knowledge):
    write_doc(knowledge, "README.md", "# Overview\nA stock monitor.\n")
    result = knowledge.search("unrelatedzzzz")
    assert result["results"] == []
    assert result["available_documents"] == ["README.md"]
    assert result["query"] == "unrelatedzzzz"


def test_search_trims_whitespace_before_work_and_return(knowledge):
    write_doc(knowledge, "README.md", "# solver\nconfiguration\n")
    result = knowledge.search(" " * 10000 + "solver" + "\n" * 10000)
    assert result["query"] == "solver"
    assert len(json.dumps(result, ensure_ascii=False)) < 10000


def test_read_is_one_based_inclusive_and_reports_next_page(knowledge):
    target = write_doc(knowledge, "README.md", "one\ntwo\nthree\nfour\nfive\n")
    result = knowledge.read("README.md", start_line=2, line_count=2)
    assert result["text"] == "two\nthree"
    assert result["start_line"] == 2
    assert result["end_line"] == 3
    assert result["total_lines"] == 5
    assert result["next_start_line"] == 4
    assert result["citation"] == "README.md:L2-L3"
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert knowledge.read("README.md", start_line=4)["next_start_line"] is None


def test_hash_and_content_refresh_when_document_changes(knowledge):
    target = write_doc(knowledge, "README.md", "# Original\nold answer\n")
    first = knowledge.read("README.md")
    target.write_text("# Revised\nnew answer\n", encoding="utf-8")
    second = knowledge.read("README.md")
    assert first["sha256"] != second["sha256"]
    assert second["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert "new answer" in second["text"]
    assert knowledge.search("new answer")["results"][0]["sha256"] == second["sha256"]


def test_bom_and_crlf_preserve_document_line_numbers(knowledge):
    target = knowledge.root / "README.md"
    target.write_bytes(b"\xef\xbb\xbf# Heading\r\nsecond\r\nthird\r\n")
    assert knowledge.catalog()["documents"][0]["title"] == "Heading"
    result = knowledge.read("README.md", 2, 1)
    assert result["text"] == "second"
    assert result["total_lines"] == 3


def test_catalog_includes_only_existing_approved_docs_and_bounds_title(knowledge):
    write_doc(knowledge, "README.md", "# " + "标题" * 200 + "\nbody\n")
    write_doc(knowledge, "docs/configuration.md", "body without heading\n")
    write_doc(knowledge, "docs/deployment.md", "# Private deployment\nsecret\n")
    write_doc(knowledge, "config/.env", "SECRET=not-for-document-tool\n")
    documents = knowledge.catalog()["documents"]
    assert {row["path"] for row in documents} == {"README.md", "docs/configuration.md"}
    assert len(documents[0]["title"]) == 160
    assert documents[1]["title"] == "docs/configuration.md"
    assert documents[0]["line_count"] == 2
    assert knowledge.search("secret")["results"] == []


@pytest.mark.parametrize(
    "path",
    [
        "../README.md",
        "docs/../README.md",
        "docs/deployment.md",
        "config/.env",
        "README.md/../../config/.env",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "README.md\x00",
        None,
        123,
    ],
)
def test_path_whitelist_rejects_unapproved_or_malformed_paths(knowledge, path):
    write_doc(knowledge, "README.md", "# Allowed\n")
    with pytest.raises(ValueError, match="路径"):
        knowledge.read(path)


@pytest.mark.parametrize("start", [0, -1, 1.5, "1", True, None])
def test_invalid_start_line_is_rejected(knowledge, start):
    write_doc(knowledge, "README.md", "one\n")
    with pytest.raises(ValueError, match="start_line"):
        knowledge.read("README.md", start_line=start)


@pytest.mark.parametrize("count", [0, -1, 121, 1.5, "1", True, None])
def test_invalid_line_count_is_rejected(knowledge, count):
    write_doc(knowledge, "README.md", "one\n")
    with pytest.raises(ValueError, match="line_count"):
        knowledge.read("README.md", line_count=count)


@pytest.mark.parametrize("query", ["", " \n\t", "a" * 257, None, 123, ["search"]])
def test_invalid_search_query_is_rejected(knowledge, query):
    with pytest.raises(ValueError, match="query"):
        knowledge.search(query)


@pytest.mark.parametrize("limit", [0, -1, 7, 1.5, "1", True, None])
def test_invalid_search_limit_is_rejected(knowledge, limit):
    with pytest.raises(ValueError, match="limit"):
        knowledge.search("valid query", limit)


def test_missing_empty_and_out_of_range_documents_fail_clearly(knowledge):
    with pytest.raises(ValueError, match="不存在"):
        knowledge.read("README.md")
    write_doc(knowledge, "README.md", "")
    with pytest.raises(ValueError, match="共 0 行"):
        knowledge.read("README.md")
    write_doc(knowledge, "README.md", "one\n")
    with pytest.raises(ValueError, match="共 1 行"):
        knowledge.read("README.md", start_line=2)


def test_credentials_are_redacted_in_read_search_and_catalog(knowledge):
    values = [
        "api-secret-value",
        "app-secret-value",
        "token-value",
        "pw-value",
        "hook-value",
    ]
    content = (
        '# API_KEY="api-secret-value"\n'
        "FEISHU_APP_SECRET: app-secret-value\n"
        "access_token = token-value\n"
        "password: pw-value\n"
        "webhook_url=https://example.invalid/hook-value\n"
        "Authorization: Bearer bearer-secret-value\n"
        "# Public answer\nconfiguration remains documented\n"
    )
    target = write_doc(knowledge, "README.md", content)
    result = knowledge.read("README.md")
    outputs = json.dumps(
        [result, knowledge.search("configuration"), knowledge.catalog()],
        ensure_ascii=False,
    )
    for value in [*values, "bearer-secret-value"]:
        assert value not in outputs
    assert "隐藏" in outputs
    assert result["total_lines"] == 8
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_bearer_redaction_does_not_join_document_lines(knowledge):
    write_doc(knowledge, "README.md", "# Usage\nBearer\nTokenExample\nfourth line\n")
    result = knowledge.read("README.md", 4, 1)
    assert result["total_lines"] == 4
    assert result["text"] == "fourth line"
    assert result["citation"] == "README.md:L4-L4"


def test_document_size_limit_is_bytes_and_oversized_docs_are_skipped(knowledge):
    write_doc(knowledge, "README.md", "中" * (MAX_BYTES // 3 + 1))
    write_doc(knowledge, "docs/configuration.md", "# Small\nworking configuration\n")
    with pytest.raises(ValueError, match="大小限制"):
        knowledge.read("README.md")
    assert [row["path"] for row in knowledge.catalog()["documents"]] == [
        "docs/configuration.md"
    ]
    assert knowledge.search("configuration")["available_documents"] == [
        "docs/configuration.md"
    ]


def test_exact_byte_limit_is_readable_but_single_line_excerpt_is_bounded(knowledge):
    target = knowledge.root / "README.md"
    target.write_bytes(b"x" * MAX_BYTES)
    # A prior sanitizer regex took quadratic time on a single long identifier.
    # Bound this regression in a subprocess so a failure cannot hang the suite.
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; from pathlib import Path; "
                "from src.interactive.assistant.knowledge import ProjectKnowledge; "
                "print(json.dumps(ProjectKnowledge(Path(sys.argv[1])).read('README.md')))"
            ),
            str(knowledge.root),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert len(result["text"]) == MAX_EXCERPT_CHARS
    assert result["text"].endswith("…")
    assert result["start_line"] == result["end_line"] == 1


def test_multiline_excerpt_limit_preserves_paging_at_complete_lines(knowledge):
    write_doc(knowledge, "README.md", "\n".join(["a" * 2000] * 6))
    result = knowledge.read("README.md")
    assert len(result["text"]) <= MAX_EXCERPT_CHARS
    assert result["end_line"] == 2
    assert result["next_start_line"] == 3
    assert knowledge.read("README.md", result["next_start_line"])["start_line"] == 3


def test_search_limits_results_and_does_not_return_overlapping_chunks(knowledge):
    for name in DOC_PATHS[:8]:
        write_doc(
            knowledge,
            name,
            "\n".join(f"solver search evidence line {index}" for index in range(100)),
        )
    results = knowledge.search("solver search", limit=6)["results"]
    assert len(results) == 6
    assert all(len(row["text"]) <= MAX_EXCERPT_CHARS for row in results)
    for index, left in enumerate(results):
        for right in results[index + 1 :]:
            if left["path"] == right["path"]:
                assert (
                    left["end_line"] < right["start_line"]
                    or right["end_line"] < left["start_line"]
                )


@pytest.mark.parametrize("link_kind", ["outside_file", "inside_file", "directory"])
def test_symbolic_links_are_rejected_even_when_target_is_inside_root(
    knowledge, tmp_path, link_kind
):
    target = write_doc(knowledge, "private.md", "private content must not be read\n")
    link = knowledge.root / "README.md"
    if link_kind == "outside_file":
        target = tmp_path.parent / f"outside-{tmp_path.name}.md"
        target.write_text("outside confidential content", encoding="utf-8")
    elif link_kind == "directory":
        target = knowledge.root / "redirected"
        target.mkdir()
        (target / "architecture.md").write_text("private content", encoding="utf-8")
        link = knowledge.root / "docs"
    try:
        link.symlink_to(target, target_is_directory=link_kind == "directory")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"OS does not permit creating test symlinks: {exc}")
    requested = "docs/architecture.md" if link_kind == "directory" else "README.md"
    with pytest.raises(ValueError, match="符号链接|目录重定向"):
        knowledge.read(requested)
    assert requested not in knowledge.search("private")["available_documents"]
    assert requested not in [row["path"] for row in knowledge.catalog()["documents"]]


def test_resolved_path_redirection_is_rejected_before_open(knowledge, monkeypatch):
    # Keep the rejection branch covered even on Windows without symlink privilege.
    write_doc(knowledge, "README.md", "public content")
    original_resolve = Path.resolve

    def redirected(path, *args, **kwargs):
        if path == knowledge.root / "README.md":
            return knowledge.root / "config/.env"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ValueError, match="符号链接|目录重定向"):
        knowledge.read("README.md")
