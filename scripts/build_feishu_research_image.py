"""Build and smoke-test the dedicated research image on the deployment host.

Only Dockerfile, requirements.txt and optional verified wheels enter the context. Existing service
configuration and containers are untouched; the final tag is assigned only
after the candidate passes the real container probes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shlex
import subprocess
import sys
import tarfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.deploy_feishu_assistant import deploy, digest, source_file, ssh_arguments

OFFICIAL_BASES = (
    "python:3.10-slim-bookworm",
    "public.ecr.aws/docker/library/python:3.10-slim-bookworm",
)

SMOKE_PROGRAM = r"""
import errno, importlib.metadata, json, os, pathlib, socket, sys
checks = {'nonroot': os.geteuid() != 0}
checks['secret_env_absent'] = not any(x in os.environ for x in (
    'DEEPSEEK_API_KEY', 'FEISHU_APP_SECRET', 'EMAIL_PASSWORD', 'TELEGRAM_BOT_TOKEN'))
for key, path in [('input_readonly', '/input/forbidden'), ('root_readonly', '/forbidden')]:
    try:
        pathlib.Path(path).write_text('must fail')
    except OSError as exc:
        checks[key] = exc.errno in (errno.EROFS, errno.EACCES)
    else:
        checks[key] = False
try:
    connection = socket.create_connection(('1.1.1.1', 443), timeout=2)
    connection.close()
    checks['network_disabled'] = False
except OSError:
    checks['network_disabled'] = True
memory_paths = ['/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes']
memory = next(int(pathlib.Path(p).read_text().strip()) for p in memory_paths if pathlib.Path(p).is_file())
checks['memory_limit'] = memory == 768 * 1024**2
cpu_paths = ['/sys/fs/cgroup/cpu.max', '/sys/fs/cgroup/cpu/cpu.cfs_quota_us',
             '/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us']
cpu = next(pathlib.Path(p).read_text().strip() for p in cpu_paths if pathlib.Path(p).is_file())
checks['cpu_limit'] = cpu.startswith('100000')
assert all(checks.values()), checks
packages = {name: importlib.metadata.version(name) for name in (
    'numpy', 'pandas', 'numba', 'scipy', 'pydantic', 'lark-oapi', 'exchange-calendars')}
import numpy, pandas, numba
@numba.njit(cache=False)
def calculation(values):
    return values.sum()
assert calculation(numpy.array([1., 2., 3.])) == 6.
checks['numba_compilation'] = True
result = {'checks': checks, 'python': sys.version.split()[0], 'packages': packages,
          'uid': os.geteuid(), 'memory_limit_bytes': memory, 'cpu_limit': cpu}
pathlib.Path('/output/probe.json').write_text(json.dumps(result))
print(json.dumps(result), flush=True)
"""

BUILD_PROGRAM = r"""
import base64, hashlib, json, pathlib, subprocess, tarfile, time, uuid
from datetime import datetime, timezone
root = pathlib.Path(request['root'])
if not root.is_absolute() or root.resolve() != root or not root.is_dir():
    raise ValueError('invalid project root')
identifier = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
stage = root / 'data' / 'deployments' / ('feishu_research_image_' + identifier)
for folder in (root / 'data', root / 'data/deployments', stage):
    if folder.is_symlink():
        raise ValueError('unsafe build stage')
    folder.mkdir(exist_ok=True)
stage.chmod(0o700)
context = stage / 'context'
context.mkdir()
for name, item in request['files'].items():
    if name not in {'Dockerfile', 'requirements.txt'}:
        raise ValueError('unexpected build file')
    data = base64.b64decode(item['content'], validate=True)
    if hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError('build checksum mismatch')
    (context / name).write_bytes(data)
manifest = {'stage': str(stage), 'context_hashes': {k: v['sha256'] for k, v in request['files'].items()},
            'base_image': request['base_image'], 'started_at': datetime.now(timezone.utc).isoformat(),
            'status': 'building'}
if request.get('wheelhouse'):
    wheelhouse = request['wheelhouse']
    archive = pathlib.Path(wheelhouse['archive'])
    if archive.is_symlink() or not archive.resolve().is_relative_to(root / 'data/deployments'):
        raise ValueError('wheel archive outside managed deployment area')
    def file_digest(path):
        value = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                value.update(chunk)
        return value.hexdigest()
    if file_digest(archive) != wheelhouse['sha256']:
        raise ValueError('wheel archive SHA-256 mismatch')
    expected = {item['filename']: item for item in wheelhouse['files']}
    destination = context / 'wheelhouse'
    destination.mkdir()
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        if len(members) != len(expected) or {item.name for item in members} != set(expected):
            raise ValueError('wheel archive inventory mismatch')
        for member in members:
            item = expected[member.name]
            if not member.isfile() or pathlib.PurePosixPath(member.name).name != member.name or not member.name.endswith('.whl'):
                raise ValueError('non-wheel or non-regular archive entry')
            if member.size != item['size'] or member.size > 200 * 1024**2:
                raise ValueError('wheel size mismatch')
            path = destination / member.name
            with bundle.extractfile(member) as source, path.open('wb') as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    output.write(chunk)
            if file_digest(path) != item['sha256']:
                raise ValueError('individual wheel SHA-256 mismatch')
    dockerfile = context / 'Dockerfile'
    content = dockerfile.read_text()
    online = 'RUN pip install --no-cache-dir -r /tmp/research-requirements.txt'
    offline = 'COPY wheelhouse /tmp/wheelhouse\nRUN pip install --no-cache-dir --no-index --find-links=/tmp/wheelhouse -r /tmp/research-requirements.txt'
    if content.count(online) != 1:
        raise ValueError('Dockerfile no longer matches reviewed offline adapter')
    dockerfile.write_text(content.replace(online, offline))
    manifest['offline_dockerfile_sha256'] = file_digest(dockerfile)
    manifest['wheelhouse'] = wheelhouse
def save():
    (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2))
def command(args, timeout=60):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if p.returncode:
        (stage / 'command_failure.log').write_text(p.stdout + '\n' + p.stderr)
        raise RuntimeError('command failed: ' + ' '.join(args[:3]) + ' (exit ' + str(p.returncode) + ')')
    return p.stdout.strip()
save()
print(json.dumps({'status': 'building', 'stage': str(stage)}), flush=True)
candidate = 'trade-eyes-research:build-' + identifier.lower()
smoke_name = 'feishu-research-smoke-' + identifier.lower()
cancel_name = 'feishu-research-cancel-' + identifier.lower()
owned_names = []
try:
    shared_before = sorted(command(['docker', 'ps', '-q']).splitlines())
    with (stage / 'build.log').open('wb') as log:
        p = subprocess.Popen(['docker', 'build', '--memory=768m', '--memory-swap=768m',
            '--cpu-period=100000', '--cpu-quota=100000', '--build-arg',
            'BASE_IMAGE=' + request['base_image'], '-t', candidate, str(context)],
            stdout=log, stderr=subprocess.STDOUT)
        started = time.monotonic()
        while p.poll() is None:
            if time.monotonic() - started > request['timeout']:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                raise TimeoutError('image build timed out')
            time.sleep(15)
            print(json.dumps({'status': 'building', 'elapsed_seconds': round(time.monotonic() - started)}), flush=True)
        if p.returncode:
            manifest['build_exit_code'] = p.returncode
            raise RuntimeError('dedicated Docker image build failed; see build.log')
    image_id = command(['docker', 'image', 'inspect', candidate, '--format', '{{.Id}}'])
    manifest.update({'candidate_image': candidate, 'image_id': image_id, 'status': 'smoke_testing'})
    save()
    print(json.dumps({'status': 'smoke_testing', 'image_id': image_id}), flush=True)
    inputs, outputs = stage / 'smoke_input', stage / 'smoke_output'
    inputs.mkdir()
    outputs.mkdir()
    outputs.chmod(0o777)
    (inputs / 'readonly.json').write_text('{"probe":true}\n')
    common = ['docker', 'run', '--rm', '--pull=never', '--init', '--network=none',
        '--read-only', '--user=65532:65532', '--cap-drop=ALL',
        '--security-opt=no-new-privileges=true', '--pids-limit=128', '--cpus=1',
        '--memory=768m', '--memory-swap=768m', '--tmpfs',
        '/tmp:rw,nosuid,nodev,size=256m,mode=1777', '--env=HOME=/tmp',
        '--env=NUMBA_CACHE_DIR=/tmp/numba', '--env=OPENBLAS_NUM_THREADS=1',
        '--env=OMP_NUM_THREADS=1', '--env=MPLCONFIGDIR=/tmp/matplotlib', '--workdir=/tmp']
    owned_names.append(smoke_name)
    output = command(common + ['--name', smoke_name, '--mount',
        'type=bind,source=' + str(inputs) + ',target=/input,readonly', '--mount',
        'type=bind,source=' + str(outputs) + ',target=/output', '--entrypoint=python',
        image_id, '-B', '-c', request['smoke']], timeout=120)
    smoke_result = json.loads(output.splitlines()[-1])
    if json.loads((outputs / 'probe.json').read_text()) != smoke_result:
        raise RuntimeError('stdout and artifact differ')
    manifest['smoke'] = smoke_result
    owned_names.append(cancel_name)
    command(common + ['--detach', '--name', cancel_name, '--entrypoint=python',
        image_id, '-c', 'import time; time.sleep(120)'])
    if command(['docker', 'inspect', cancel_name, '--format', '{{.State.Running}}']) != 'true':
        raise RuntimeError('cancellation probe did not start')
    cancel_started = time.monotonic()
    command(['docker', 'rm', '-f', cancel_name], timeout=15)
    inspected = subprocess.run(['docker', 'inspect', cancel_name],
        capture_output=True, text=True, timeout=10, check=False)
    if inspected.returncode == 0:
        raise RuntimeError('cancelled container remains')
    manifest['cancellation'] = {'removed': True, 'elapsed_seconds': round(time.monotonic() - cancel_started, 2)}
    shared_after = sorted(command(['docker', 'ps', '-q']).splitlines())
    manifest['shared_container_ids_unchanged'] = shared_before == shared_after
    if shared_before != shared_after:
        raise RuntimeError('shared container inventory changed; inspect before tagging')
    command(['docker', 'tag', image_id, 'trade-eyes-research:local'])
    manifest.update({'final_image': 'trade-eyes-research:local', 'status': 'ready',
                     'completed_at': datetime.now(timezone.utc).isoformat()})
    save()
    print(json.dumps({key: value for key, value in manifest.items() if key != 'wheelhouse'}), flush=True)
except Exception as exc:
    manifest.update({'status': 'failed', 'error_type': type(exc).__name__, 'error': str(exc)})
    save()
    print(json.dumps({key: value for key, value in manifest.items() if key != 'wheelhouse'}), flush=True)
    raise SystemExit(1)
finally:
    for name in owned_names:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True,
            text=True, timeout=15, check=False)
"""


def prepare_wheelhouse(folder: Path, resume_stage: str | None = None) -> dict:
    """Upload only official-hash-verified wheels, without their source tree."""
    folder = folder.resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("errors") or manifest.get("source") != "https://pypi.org":
        raise ValueError("wheelhouse requires a successful official PyPI manifest")
    files = manifest.get("files", [])
    if not files or len(files) > 200:
        raise ValueError("wheelhouse file count must be between 1 and 200")
    paths = []
    for item in files:
        name = item["filename"]
        path = folder / name
        if Path(name).name != name or not name.endswith(".whl"):
            raise ValueError("wheelhouse entry must be a plain wheel filename")
        if path.is_symlink() or not path.resolve().is_relative_to(folder):
            raise ValueError("unsafe wheelhouse entry")
        if (
            path.stat().st_size != item["size"]
            or digest(path.read_bytes()) != item["sha256"]
        ):
            raise ValueError("wheel changed after official SHA-256 verification")
        paths.append(path)
    if resume_stage and not re.fullmatch(
        r"feishu_wheelhouse_[a-f0-9]{12}", resume_stage
    ):
        raise ValueError("resume stage must name this helper's dedicated wheel upload")
    identifier = resume_stage or "feishu_wheelhouse_" + uuid.uuid4().hex[:12]
    archive = folder / (identifier + ".tar")
    if resume_stage:
        if archive.is_symlink() or not archive.is_file():
            raise ValueError("resume requires the original local wheel archive")
        expected = {item["filename"]: item for item in files}
        with tarfile.open(archive) as bundle:
            if {member.name for member in bundle.getmembers()} != set(expected):
                raise ValueError("resume archive inventory changed")
            for member in bundle.getmembers():
                if not member.isfile() or member.size != expected[member.name]["size"]:
                    raise ValueError("resume archive member changed")
                data = bundle.extractfile(member).read()
                if digest(data) != expected[member.name]["sha256"]:
                    raise ValueError("resume archive differs from the verified wheels")
    else:
        with tarfile.open(archive, "w") as bundle:
            for path in paths:
                bundle.add(path, arcname=path.name, recursive=False)
    hasher = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    code = (
        "import json, pathlib\n"
        f"root=pathlib.Path({deploy.REMOTE_DIR!r})\n"
        f"stage=root/'data/deployments'/{identifier!r}\n"
        "assert root.is_absolute() and root.resolve()==root\n"
        "for item in (root/'data',root/'data/deployments',stage):\n"
        "    assert not item.is_symlink()\n"
        "    item.mkdir(exist_ok=True)\n"
        "stage.chmod(0o700)\n"
        "print(json.dumps(str(stage/'wheels.tar')))\n"
    )
    args = ssh_arguments()
    made = subprocess.run(
        args,
        input=code,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    remote_path = json.loads(made.stdout)
    print(
        json.dumps(
            {
                "status": "uploading_verified_wheels",
                "count": len(files),
                "bytes": archive.stat().st_size,
            }
        ),
        flush=True,
    )
    # A few bounded SSH streams avoid slow SFTP windows on this host. Each part
    # and the final assembled archive must match the locally verified content.
    chunk_size = 2 * 1024 * 1024
    chunks = []
    with archive.open("rb") as stream:
        while data := stream.read(chunk_size):
            chunks.append({"size": len(data), "sha256": digest(data)})
    existing = set()
    if resume_stage:
        probe = (
            "import hashlib,json,pathlib\n"
            f"stage=pathlib.Path({remote_path!r}).parent\n"
            "assert stage.resolve()==stage and not stage.is_symlink()\n"
            f"chunks={chunks!r}\n"
            "existing=[]\n"
            "for index,chunk in enumerate(chunks):\n"
            "    path=stage/f'part-{index:04d}'\n"
            "    if path.exists():\n"
            "        assert path.is_file() and not path.is_symlink()\n"
            "        assert path.stat().st_size==chunk['size']\n"
            "        assert hashlib.sha256(path.read_bytes()).hexdigest()==chunk['sha256']\n"
            "        existing.append(index)\n"
            "print(json.dumps(existing))\n"
        )
        checked = subprocess.run(
            args,
            input=probe,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        if checked.returncode:
            raise RuntimeError("existing wheel chunks failed integrity validation")
        existing = set(json.loads(checked.stdout))
        print(
            json.dumps(
                {
                    "status": "resuming_verified_wheels",
                    "verified_parts": len(existing),
                    "missing_parts": len(chunks) - len(existing),
                }
            ),
            flush=True,
        )

    def upload_chunk(index: int) -> int:
        chunk = chunks[index]
        with archive.open("rb") as stream:
            stream.seek(index * chunk_size)
            data = stream.read(chunk["size"])
        command = (
            "import hashlib,pathlib,sys;"
            f"root=pathlib.Path({deploy.REMOTE_DIR!r});"
            f"stage=root/'data/deployments'/{identifier!r};"
            "assert stage.resolve()==stage and not stage.is_symlink();"
            f"path=stage/'part-{index:04d}';"
            f"data=sys.stdin.buffer.read({chunk_size + 1});"
            f"assert len(data)=={chunk['size']};"
            f"assert hashlib.sha256(data).hexdigest()=={chunk['sha256']!r};"
            "assert not path.is_symlink();"
            "assert not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()==hashlib.sha256(data).hexdigest();"
            "path.write_bytes(data) if not path.exists() else None"
        )
        for attempt in range(3):
            try:
                result = subprocess.run(
                    [*args[:-1], "python3 -B -c " + shlex.quote(command)],
                    input=data,
                    capture_output=True,
                    timeout=360,
                    check=False,
                )
                if result.returncode == 0:
                    return chunk["size"]
            except subprocess.TimeoutExpired:
                pass
        raise RuntimeError(
            f"verified wheel chunk {index} upload failed after 3 attempts"
        )

    completed_bytes = sum(chunks[index]["size"] for index in existing)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(upload_chunk, index)
            for index in range(len(chunks))
            if index not in existing
        ]
        for future in as_completed(futures):
            completed_bytes += future.result()
            print(
                json.dumps(
                    {"status": "uploading_verified_wheels", "bytes": completed_bytes}
                ),
                flush=True,
            )
    assemble = (
        "import hashlib,os,pathlib\n"
        f"root=pathlib.Path({deploy.REMOTE_DIR!r})\n"
        f"stage=root/'data/deployments'/{identifier!r}\n"
        "assert stage.resolve()==stage and not stage.is_symlink()\n"
        f"chunks={chunks!r}\n"
        "hasher=hashlib.sha256()\n"
        "with (stage/'wheels.tar.partial').open('xb') as destination:\n"
        "    for index,chunk in enumerate(chunks):\n"
        "        path=stage/f'part-{index:04d}'\n"
        "        assert not path.is_symlink()\n"
        "        data=path.read_bytes()\n"
        "        assert len(data)==chunk['size']\n"
        "        assert hashlib.sha256(data).hexdigest()==chunk['sha256']\n"
        "        hasher.update(data)\n"
        "        destination.write(data)\n"
        f"assert hasher.hexdigest()=={hasher.hexdigest()!r}\n"
        "os.replace(stage/'wheels.tar.partial',stage/'wheels.tar')\n"
        "for index in range(len(chunks)):\n"
        "    (stage/f'part-{index:04d}').unlink()\n"
    )
    assembled = subprocess.run(
        args,
        input=assemble,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if assembled.returncode:
        raise RuntimeError("verified wheel archive assembly failed")
    return {"archive": remote_path, "sha256": hasher.hexdigest(), "files": files}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-image", choices=OFFICIAL_BASES, default=OFFICIAL_BASES[0]
    )
    parser.add_argument("--timeout", type=int, default=1500)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--resume-wheelhouse")
    args = parser.parse_args()
    if not 60 <= args.timeout <= 3600:
        parser.error("timeout must be between 60 and 3600 seconds")
    if args.resume_wheelhouse and not args.wheelhouse:
        parser.error("--resume-wheelhouse requires --wheelhouse")
    files = {}
    for name in ("Dockerfile", "requirements.txt"):
        source = source_file("docker/feishu-research/" + name)
        data = source.read_bytes()
        files[name] = {
            "content": base64.b64encode(data).decode(),
            "sha256": digest(data),
        }
    request = {
        "root": deploy.REMOTE_DIR,
        "files": files,
        "base_image": args.base_image,
        "timeout": args.timeout,
        "smoke": SMOKE_PROGRAM,
    }
    if args.wheelhouse:
        request["wheelhouse"] = prepare_wheelhouse(
            args.wheelhouse, args.resume_wheelhouse
        )
    program = "request = " + repr(request) + "\n" + BUILD_PROGRAM
    process = subprocess.Popen(
        ssh_arguments(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    process.stdin.write(program)
    process.stdin.close()
    for line in process.stdout:
        print(line, end="", flush=True)
    return process.wait()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Research image build failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
