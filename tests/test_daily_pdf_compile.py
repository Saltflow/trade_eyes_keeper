"""PDF conversion failure boundaries and sequential process ownership."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.notification import daily_pdf
from src.notification.daily_pdf import _compile_pdf


@pytest.fixture
def tex(tmp_path, monkeypatch):
    # Compiler protocol tests isolate platform discovery; probe tests below
    # exercise the real discovery helper independently on every platform.
    monkeypatch.setattr(daily_pdf, "os", SimpleNamespace(name="nt"))
    directory = tmp_path / "report with spaces"
    directory.mkdir()
    path = directory / "report.tex"
    path.write_text("frozen rendered report input", encoding="utf-8")
    return path


def test_unix_tex_receives_cached_probe_before_both_passes(monkeypatch, tex):
    monkeypatch.setattr(daily_pdf, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(daily_pdf, "_converter_probe_preamble", lambda: "% probe\n")
    process_stub(monkeypatch, tex)
    assert _compile_pdf(tex).startswith(b"%PDF-")
    assert tex.read_text("utf-8") == "% probe\nfrozen rendered report input"


@pytest.mark.parametrize(
    "header",
    [
        "This is xdvipdfmx Version 20210609 by the DVIPDFMx project team,",
        "This is dvipdfmx Version 20260101 by the DVIPDFMx project team,",
        "xdvipdfmx Version 20260929",
    ],
)
def test_probe_retains_actual_version_without_setting_backend_internals(
    monkeypatch, header
):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=header + "\nignored license text")

    monkeypatch.setattr(subprocess, "run", run)
    preamble = daily_pdf._converter_probe_preamble()
    assert len(calls) == 1
    assert calls[0] == (
        ["extractbb", "--version"],
        {"capture_output": True, "text": True, "timeout": 10, "check": False},
    )
    assert header.replace(" ", "~") in preamble
    assert header.replace(" ", "~") + "~}" in preamble
    assert "c__kernel_sys_dvipdfmx_version_int" not in preamble
    assert r"\str_if_eq:nnTF {#1} {extractbb~--version}" in preamble
    assert r"\tl_set_rescan:Nnn #3 {#2}" in preamble
    assert r"\sys_if_shell:TF" in preamble
    assert (
        r"\cs_gset_eq:NN \sys_get_shell:nnNTF \tradeeyes_original_shell:nnNTF"
        in preamble
    )
    assert r"\tradeeyes_original_shell:nnNTF {#1} {#2} #3 {#4} {#5}" in preamble


def test_probe_accepts_actual_linux_banner_with_leading_blank_line(monkeypatch):
    stdout = (
        "\nThis is extractbb Version 20210609\n\n"
        "Copyright (C) 2008-2021 by Jin-Hwan Cho and Matthias Franz\n"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout),
    )
    preamble = daily_pdf._converter_probe_preamble()
    assert "This~is~extractbb~Version~20210609~}" in preamble
    assert "Copyright" not in preamble


@pytest.mark.parametrize(
    "exit_code,header",
    [
        (1, "This is xdvipdfmx Version 20210609"),
        (0, ""),
        (0, "\n \n\t\n"),
        (0, "unknown version"),
        (0, "xdvipdfmx Version 2021060"),
        (0, r"xdvipdfmx Version 20210609 \input{other}"),
        (0, "xdvipdfmx Version 20210609 % comment"),
        (0, "xdvipdfmx Version 20210609 #1"),
        (0, "xdvipdfmx Version 20210609 中文"),
        (0, "xdvipdfmx Version 20210609 \x00"),
        (0, "xdvipdfmx Version 20210609 \x7f"),
        (0, "xdvipdfmx Version 20210609 Version 20210610 other"),
    ],
)
def test_probe_unknown_or_unsafe_output_fails_closed(monkeypatch, exit_code, header):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=exit_code, stdout=header),
    )
    with pytest.raises(ValueError, match="Unrecognized converter version"):
        daily_pdf._converter_probe_preamble()


@pytest.mark.parametrize("error_type", [subprocess.TimeoutExpired, FileNotFoundError])
def test_probe_errors_do_not_fall_back_to_forking_tex(monkeypatch, error_type):
    def run(command, **kwargs):
        if error_type is subprocess.TimeoutExpired:
            raise error_type(command, kwargs["timeout"])
        raise error_type(command[0])

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(error_type):
        daily_pdf._converter_probe_preamble()


def process_stub(monkeypatch, tex, *, converter_exit=0, pdf=b"%PDF-1.5\nproof"):
    calls = []
    running = set()

    def run(command, **kwargs):
        assert not running, "the previous child must have returned before next spawn"
        running.add(len(calls))
        calls.append((command, kwargs))
        try:
            if command[0] == "xelatex":
                tex.with_suffix(".xdv").write_bytes(b"final XDV")
                return SimpleNamespace(returncode=0, stderr="")
            assert len(calls) == 3, "converter must wait for both TeX passes"
            if pdf is not None:
                Path(command[command.index("-o") + 1]).write_bytes(pdf)
            return SimpleNamespace(returncode=converter_exit, stderr="conversion error")
        finally:
            running.clear()

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_both_tex_passes_finish_before_independent_converter(monkeypatch, tex):
    calls = process_stub(monkeypatch, tex)
    assert _compile_pdf(tex) == b"%PDF-1.5\nproof"
    assert [command[0] for command, _ in calls] == ["xelatex", "xelatex", "xdvipdfmx"]
    assert all("-no-pdf" in command for command, _ in calls[:2])
    assert calls[0] == calls[1]
    converter, _ = calls[2]
    assert converter == [
        "xdvipdfmx",
        "-q",
        "-E",
        "-o",
        str(tex.with_suffix(".pdf")),
        str(tex.with_suffix(".xdv")),
    ]
    assert all(
        options == {"capture_output": True, "text": True, "timeout": 60, "check": False}
        for _, options in calls
    ), "direct argv must retain inherited cwd/env and bounded child waits"
    assert tex.read_text("utf-8") == "frozen rendered report input"


@pytest.mark.parametrize(
    "exit_code,pdf",
    [(1, b"%PDF-1.5\npartial"), (0, None), (0, b""), (0, b"not a pdf")],
)
def test_incomplete_or_invalid_conversion_is_not_report_success(
    monkeypatch, tex, exit_code, pdf
):
    calls = process_stub(monkeypatch, tex, converter_exit=exit_code, pdf=pdf)
    assert _compile_pdf(tex) is None
    assert len(calls) == 3, "conversion failures must not retry or return partial bytes"


def test_missing_xdv_never_spawns_converter(monkeypatch, tex, caplog):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stderr="missing XDV")

    monkeypatch.setattr(subprocess, "run", run)
    assert _compile_pdf(tex) is None
    assert len(calls) == 2
    assert "未产出 XDV" in caplog.text


def test_failed_second_pass_cannot_reuse_first_pass_xdv(monkeypatch, tex):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            tex.with_suffix(".xdv").write_bytes(b"first pass only")
            return SimpleNamespace(returncode=0, stderr="")
        return SimpleNamespace(returncode=1, stderr="second pass failed")

    monkeypatch.setattr(subprocess, "run", run)
    assert _compile_pdf(tex) is None
    assert len(calls) == 2


def test_converter_without_output_cannot_reuse_earlier_pdf(monkeypatch, tex):
    tex.with_suffix(".pdf").write_bytes(b"%PDF-1.5\nstale")
    process_stub(monkeypatch, tex, pdf=None)
    assert _compile_pdf(tex) is None


def test_tex_soft_error_can_still_convert_existing_final_xdv(monkeypatch, tex, caplog):
    calls = process_stub(monkeypatch, tex)
    original = subprocess.run

    def run(command, **kwargs):
        result = original(command, **kwargs)
        if command[0] == "xelatex":
            tex.with_suffix(".log").write_text(
                "context\n! soft error\ncontinued output", encoding="utf-8"
            )
            result.returncode = 1
        return result

    monkeypatch.setattr(subprocess, "run", run)
    assert _compile_pdf(tex).startswith(b"%PDF-")
    assert len(calls) == 3
    assert "! soft error" in caplog.text


@pytest.mark.parametrize("failure_call", [1, 2, 3])
@pytest.mark.parametrize("error_type", [subprocess.TimeoutExpired, FileNotFoundError])
def test_child_exception_propagates_to_report_failure_without_retry(
    monkeypatch, tex, failure_call, error_type
):
    calls = process_stub(monkeypatch, tex)
    original = subprocess.run
    count = 0

    def run(command, **kwargs):
        nonlocal count
        count += 1
        if count == failure_call:
            if error_type is subprocess.TimeoutExpired:
                raise error_type(command, kwargs["timeout"])
            raise error_type(command[0])
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(error_type):
        _compile_pdf(tex)
    assert count == failure_call
    assert len(calls) == failure_call - 1
