"""Verify an immutable completed SYMBOLS checkpoint before continuing a job."""

import hashlib
import json
from pathlib import Path
import shutil

from source_native_preflight import steps
from runtime_resources import is_memory_failure


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save_symbols(job, device):
    book = job / "raw/input.omr"
    stages = steps(book)
    if not stages or any(not stage or stage[-1] != "SYMBOLS" for stage in stages):
        raise ValueError("符号识别存档未完整结束，无法续跑。")
    sources = sorted([*job.glob("unit-*.png"), *job.glob("original-*.png")])
    files = [book, job / "input.tif", job / "source-map.json", *sources]
    record = {"device": device, "files": {p.relative_to(job).as_posix(): digest(p) for p in files}}
    pending = job / "symbols-checkpoint.pending"
    pending.write_text(json.dumps(record), encoding="utf8")
    pending.replace(job / "symbols-checkpoint.json")


def resumable(job, manifest):
    state = manifest.get("state")
    if state in {"失败", "已停止", "识别中", "因内存不足暂停"}:
        return True
    if state != "初稿已导出，待音乐核对":
        return False
    # Older builds could export after a verifier caught a memory error.
    for component in ("piano", "rhythm", "lyrics"):
        if manifest.get(f"source_{component}_context_state") != "unresolved":
            continue
        paths = [job / f"source-{component}-context.json"]
        if component == "piano":
            paths.append(job / "source-piano-core.log")
        for path in paths:
            if path.is_file() and is_memory_failure(path.read_text(encoding="utf8", errors="replace")):
                return True
    return False


def checkpoint_markers(jobs, preferred_manifest):
    if preferred_manifest is None:
        yield from sorted(jobs.glob("*/symbols-checkpoint.json"), reverse=True)
        return
    manifest_path = Path(preferred_manifest).resolve()
    if manifest_path.name != "manifest.json":
        return
    jobs_folder = manifest_path.parent.parent
    visited = set()
    while manifest_path not in visited:
        visited.add(manifest_path)
        yield manifest_path.parent / "symbols-checkpoint.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf8"))
            previous = manifest.get("resumed_from")
        except (OSError, ValueError, AttributeError):
            return
        if not isinstance(previous, str) or previous in {"", ".", ".."} or Path(previous).name != previous:
            return
        ancestor = (jobs_folder / previous / "manifest.json").resolve()
        if ancestor.parent.parent != jobs_folder:
            return
        manifest_path = ancestor


def find_symbols(jobs, sha256, options, *, preferred_manifest=None):
    for marker in checkpoint_markers(jobs, preferred_manifest):
        job = marker.parent
        try:
            manifest = json.loads((job / "manifest.json").read_text(encoding="utf8"))
            if (manifest.get("sha256") != sha256 or manifest.get("options") != options
                    or (preferred_manifest is None and not resumable(job, manifest))):
                continue
            record = json.loads(marker.read_text(encoding="utf8"))
            files = record["files"]
            if record["device"] not in {"gpu", "cpu"} or not {
                    "raw/input.omr", "input.tif", "source-map.json"} <= files.keys():
                continue
            for name, expected in files.items():
                path = Path(name)
                if path.is_absolute() or ".." in path.parts or digest(job / path) != expected:
                    break
            else:
                return job, record
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def restore_symbols(checkpoint, job):
    source, record = checkpoint
    for name, expected in record["files"].items():
        path = job / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, path)
        if digest(path) != expected:
            raise ValueError("续跑断点在复制时发生变化。")
    data = json.loads((job / "source-map.json").read_text(encoding="utf8"))
    return data["units"] if isinstance(data, dict) else data
