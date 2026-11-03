"""Execute company-specific Python only inside an offline, bounded container."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path

from .common import file_sha


def output_files(root, limit=20 * 1024 * 1024):
    """Never follow links or accept device files produced by model code."""
    total, files = 0, {}
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise ValueError("sandbox_output_link")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("sandbox_output_not_regular")
        total += info.st_size
        if total > limit or len(files) >= 200:
            raise ValueError("sandbox_output_limit")
        files[path.relative_to(root).as_posix()] = path
    return files


class ModelSandbox:
    def __init__(self, image=None):
        self.image = image or os.getenv(
            "RESEARCH_MODEL_IMAGE", "trade-eyes-research:local"
        )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,254}", self.image):
            raise ValueError("invalid_model_sandbox_image")

    def run(self, inputs: Path, output: Path, *, cancel, deadline):
        if cancel.is_set() or time.monotonic() >= deadline:
            return {
                "status": "cancelled" if cancel.is_set() else "timeout",
                "reason": "not_started",
            }
        executable = shutil.which("docker")
        if not executable:
            return {
                "status": "unavailable",
                "reason": "docker_required_no_host_execution",
            }
        try:
            inspect = subprocess.run(
                [executable, "image", "inspect", self.image, "--format", "{{.Id}}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "unavailable", "reason": "docker_image_probe_failed"}
        if inspect.returncode:
            return {"status": "unavailable", "reason": "local_image_required_no_pull"}
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", inspect.stdout.strip()):
            return {"status": "unavailable", "reason": "invalid_local_image_identity"}
        if cancel.is_set() or time.monotonic() >= deadline:
            return {
                "status": "cancelled" if cancel.is_set() else "timeout",
                "reason": "not_started",
            }
        output.mkdir(parents=True, exist_ok=False)
        result_dir = output / "files"
        result_dir.mkdir()
        # Only the dedicated immutable model/input directory is mounted.
        if os.name != "nt":
            inputs.chmod(0o755)
            for path in inputs.iterdir():
                path.chmod(0o444)
            result_dir.chmod(0o777)
        container = "research-model-" + uuid.uuid4().hex
        command = [
            executable,
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--user",
            "65534:65534",
            "--memory",
            "1g",
            "--cpus",
            "2",
            "--tmpfs",
            "/tmp:rw,nosuid,noexec,size=64m",
            "--mount",
            f"type=bind,src={inputs.resolve()},dst=/input,readonly",
            "--mount",
            f"type=bind,src={result_dir.resolve()},dst=/output",
            "--workdir",
            "/output",
            "--entrypoint",
            "python",
            inspect.stdout.strip(),
            "-I",
            "-B",
            "/input/model.py",
        ]
        status, reason = "failed", ""
        process = None
        until = min(deadline, time.monotonic() + 120)
        try:
            with (output / "stdout.txt").open("wb") as stdout, (
                output / "stderr.txt"
            ).open("wb") as stderr:
                process = subprocess.Popen(
                    command, stdout=stdout, stderr=stderr, shell=False
                )
                while process.poll() is None:
                    output_files(output)
                    if cancel.is_set() or time.monotonic() >= until:
                        status = "cancelled" if cancel.is_set() else "timeout"
                        break
                    time.sleep(0.1)
                else:
                    status = "computed" if process.returncode == 0 else "failed"
        except (OSError, ValueError) as error:
            reason = str(error)
        finally:
            if process is not None and (process.poll() is None or status != "computed"):
                try:
                    cleanup = subprocess.run(
                        [executable, "rm", "-f", container],
                        capture_output=True,
                        timeout=15,
                        check=False,
                    )
                    if (
                        cleanup.returncode
                        and b"no such container" not in (cleanup.stderr or b"").lower()
                    ):
                        reason = "container_cleanup_unconfirmed"
                except (OSError, subprocess.TimeoutExpired):
                    reason = "container_cleanup_unconfirmed"
                finally:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            reason = "container_cleanup_unconfirmed"
                if reason == "container_cleanup_unconfirmed":
                    status = "unknown"
        receipt = {
            "status": status,
            "reason": reason,
            "image_id": inspect.stdout.strip(),
            "exit_code": process.returncode if process else None,
            "financial_validation": "not_independently_verified",
            "container_name": container,
        }
        for name in ("stdout", "stderr"):
            path = output / (name + ".txt")
            if path.is_file():
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 6000))
                    receipt[name + "_tail"] = stream.read(6000).decode(
                        "utf-8", errors="replace"
                    )
        if status == "computed":
            try:
                output_files(output)
                files = output_files(result_dir)
                result = json.loads(files["results.json"].read_text("utf-8"))
                if not isinstance(result, dict):
                    raise TypeError("model_result_must_be_object")
                json.dumps(result, allow_nan=False)
                checks = result.get("checks", [])
                if not isinstance(checks, list) or any(
                    not isinstance(c, dict) or type(c.get("passed")) is not bool
                    for c in checks
                ):
                    raise ValueError("model_checks_require_explicit_boolean")
                receipt.update(
                    files={name: file_sha(path) for name, path in files.items()},
                    checks_status="failed"
                    if any(not c["passed"] for c in checks)
                    else "passed"
                    if checks
                    else "not_provided",
                )
            except (OSError, ValueError, KeyError, TypeError) as error:
                receipt.update(status="invalid_output", reason=str(error))
        return receipt
