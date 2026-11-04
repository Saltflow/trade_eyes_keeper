"""Run frozen company summaries sequentially under one shared tariff budget."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def load(path):
    return json.loads(Path(path).read_bytes())


def write_status(path, status):
    status["updated_at"] = now()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf8"
    )
    os.replace(temporary, path)


def accounted(report):
    """Charge unknown usage at its full reservation; never guess it was free."""
    return Decimal(report["known_usage_cost_usd"]) + Decimal(
        report["unknown_usage_reservation_usd"]
    )


def report_result(output):
    report = load(output / "执行结果.json")
    statuses = [r["status"] for r in report["receipts"]]
    return {
        "accounted_usd": str(accounted(report)),
        "requests_sent": report["requests_sent"],
        "statuses": statuses,
        "output": str(output),
    }


def wait_process(process, status, status_path, initial=False):
    while process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
        if initial:
            jobs = Path(status["initial_summary"]) / "jobs"
            receipts = list(jobs.glob("*/receipt.json")) if jobs.exists() else []
            active = (
                [
                    p.parent.name
                    for p in jobs.glob("*/attempt.json")
                    if not (p.parent / "receipt.json").exists()
                ]
                if jobs.exists()
                else []
            )
            status["initial_progress"] = {"receipts": len(receipts), "active": active}
        write_status(status_path, status)
        time.sleep(20)
    if not initial:
        process.wait()


def prepare_merge(job):
    target = Path(job["source"]) / job["company"] / "模型正文.md"
    target.parent.mkdir(parents=True, exist_ok=False)
    sections = [
        (
            "# 分年度片段摘要的再整合\n\n以下为前两次摘要的完整正文，不是逐项原始年报。"
            "请合并相同口径的跨年证据，保留各片段的失败与缺口；不得把缺失当作未披露。\n"
        )
    ]
    for index, name in enumerate(job["parts"], 1):
        p = Path(name)
        if not p.is_file():
            raise ValueError("required_part_summary_missing")
        text = p.read_text("utf8")
        if not text.strip():
            raise ValueError("required_part_summary_empty")
        years = job.get("part_years", [])[index - 1]
        sections.append(f"\n## 片段 {index}，报告年份 {years}：{p}\n\n{text}")
    target.write_text("\n".join(sections), encoding="utf8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    args = parser.parse_args()
    queue_path = args.queue.resolve()
    queue = load(queue_path)
    folder = queue_path.parent
    status_path = folder / "sequence_status.json"
    if status_path.exists():
        raise FileExistsError("existing_sequence_requires_manual_review")
    limit = Decimal(queue["budget_usd_tariff_comparable"])
    status = {
        "state": "waiting_initial_group",
        "created_at": now(),
        "queue_sha256": hashlib.sha256(queue_path.read_bytes()).hexdigest(),
        "budget_cny": queue["budget_cny"],
        "budget_usd_tariff_comparable": str(limit),
        "accounted_usd_upper_bound": "0",
        "results": [],
        "current": {"id": "non_alcohol_initial_group"},
        "initial_summary": queue["initial_summary"],
        "research_stage_started": False,
        "pricing_note": "Actual usage priced at frozen peak rates as a conservative upper bound; not account debit.",
    }
    write_status(status_path, status)
    try:
        initial = Path(queue["initial_summary"])
        pid = queue["initial_pid"]
        if psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            args = proc.cmdline()
            if "summary-run" not in args or not any(
                "non_alcohol_summary" in a for a in args
            ):
                raise ValueError("initial_pid_identity_mismatch")
            wait_process(proc, status, status_path, initial=True)
        if not (initial / "执行结果.json").is_file():
            raise ValueError("initial_group_result_missing_no_retry")
        first = report_result(initial)
        status["results"].append({"id": "non_alcohol_initial_group", **first})
        spent = Decimal(first["accounted_usd"])
        status["accounted_usd_upper_bound"] = str(spent)
        if spent > limit:
            raise ValueError("initial_group_exceeded_budget")
        if (initial / "execution_error.json").exists():
            raise ValueError("initial_group_execution_error_manual_review")
        for job in queue["items"]:
            if (
                hashlib.sha256(queue_path.read_bytes()).hexdigest()
                != status["queue_sha256"]
            ):
                raise ValueError("queue_changed")
            if (
                datetime.now(timezone.utc).date().isoformat()
                != queue["pricing_checked_utc_date"]
            ):
                raise ValueError("pricing_snapshot_expired")
            remaining = limit - spent
            if remaining <= 0:
                status.update(
                    state="budget_stop", current=job, remaining_usd=str(remaining)
                )
                break
            status.update(state="preparing", current=job, remaining_usd=str(remaining))
            write_status(status_path, status)
            if job["kind"] == "merge":
                prepare_merge(job)
            output = Path(job["output"])
            output.parent.mkdir(parents=True, exist_ok=True)
            prepare = [
                sys.executable,
                "-X",
                "utf8",
                str(REPO / "scripts/financial_pipeline.py"),
                "summary-prepare",
                "--source",
                job["source"],
                "--metadata",
                job["metadata"],
                "--output",
                str(output),
                "--budget-limit-usd",
                str(remaining),
                "--pricing-config",
                queue["pricing_config"],
                "--company",
                job["company"],
            ]
            completed = subprocess.run(
                prepare,
                cwd=REPO,
                capture_output=True,
                text=True,
                encoding="utf8",
                check=False,
            )
            if completed.returncode:
                if "planned_reservation_exceeds_budget" in completed.stderr:
                    status.update(
                        state="budget_stop",
                        error="next_request_reservation_exceeds_remaining_budget",
                    )
                    break
                raise ValueError(
                    "summary_preparation_failed: " + completed.stderr[-1200:]
                )
            plan = load(output / "plan.json")
            reserved = Decimal(plan["reserved_usd_peak"])
            if spent + reserved > limit:
                raise ValueError("cross_group_budget_guard_failed")
            status.update(
                state="running", current=job, current_reservation_usd=str(reserved)
            )
            write_status(status_path, status)
            cmd = [
                sys.executable,
                "-X",
                "utf8",
                str(REPO / "scripts/financial_pipeline.py"),
                "summary-run",
                "--source",
                str(output),
                "--paid",
            ]
            with (output.parent / (output.name + ".stdout.log")).open("xb") as stdout, (
                output.parent / (output.name + ".stderr.log")
            ).open("xb") as stderr:
                child = subprocess.Popen(cmd, cwd=REPO, stdout=stdout, stderr=stderr)
                status["child_pid"] = child.pid
                wait_process(psutil.Process(child.pid), status, status_path)
            status.pop("child_pid", None)
            status.pop("current_reservation_usd", None)
            if not (output / "执行结果.json").is_file():
                raise ValueError("summary_result_missing_unknown_cost_no_retry")
            item = report_result(output)
            spent += Decimal(item["accounted_usd"])
            status["accounted_usd_upper_bound"] = str(spent)
            status["results"].append({"id": job["id"], **item})
            if spent > limit:
                raise ValueError("summary_budget_exceeded")
            write_status(status_path, status)
            if (output / "execution_error.json").exists():
                raise ValueError("summary_execution_error_manual_review")
        else:
            status.update(state="finished", current=None)
    except (OSError, ValueError, KeyError, TypeError, psutil.Error) as error:
        status.update(
            state="blocked", error_type=type(error).__name__, error=str(error)
        )
    finally:
        write_status(status_path, status)
    return 0 if status["state"] == "finished" else 2


if __name__ == "__main__":
    raise SystemExit(main())
