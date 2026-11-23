'''Run source-backed vocal consistency before writing the definitive MusicXML.'''
from pathlib import Path
from xml.etree import ElementTree as ET
import hashlib
import json

from runtime_resources import check_memory


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recover(root, job, native_source, settings, env, cancel, runner, emit=print):
    job = Path(job)
    book = Path(native_source) / "raw/input.omr"
    if not book.is_file() or not (job / "source-piano-context.json").is_file():
        return root, {"state": "no_source_evidence"}
    python = Path(settings["engine_root"]) / "ocr-env/Scripts/python.exe"
    worker = Path(__file__).resolve().with_name("source_vocal_consistency_pipeline.py")
    if not python.is_file() or not worker.is_file():
        return root, {"state": "local_runtime_unavailable"}
    work = job / "source-vocal-consistency"
    try:
        check_memory(job, "source-vocal-consistency")
        work.mkdir()
        src, output, audit = (work / name for name in ("input.musicxml", "output.musicxml", "report.json"))
        ET.ElementTree(root).write(src, "utf-8", xml_declaration=True)
        original_sha = sha(src)
        runner([str(p) for p in (python, worker, job, book, src, output, audit)],
               work / "worker.log", env, cancel, timeout=150, emit=emit)
        report = json.loads(audit.read_text("utf8"))
        if report["input_sha256"] != original_sha or sha(src) != original_sha or sha(output) != report["output_sha256"]:
            raise ValueError("vocal consistency projection does not match recorded source")
        if report["state"] != "restored":
            return root, report
        return ET.parse(output).getroot(), report
    except InterruptedError:
        raise
    except (OSError, RuntimeError, ValueError, KeyError, ET.ParseError) as exc:
        emit("人声谱号与歌词对位复核未完成，原因已写入报告")
        return root, {"state": "unresolved", "reason": str(exc)}
