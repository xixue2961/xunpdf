
"""Run original-image marking checks and verify their source files on return."""
import json
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import BadZipFile

from source_piano_paired_projection import restore
from source_rolled_hollow_context import digest


def refine_piano_paired_marks(root, job, app, settings, env, cancel, runner, emit):
    job=Path(job)
    status=job/"source-piano-context.json"
    empty={"words":[],"wedges":[]}
    if not status.is_file():
        return root,dict(state="no_mapping",**empty)
    if cancel.is_set():
        raise InterruptedError("已取消原谱表情记号复核")
    try:
        native=json.loads(status.read_text(encoding="utf8")).get("native_recovery",{})
        entries=[native.get(k) for k in ("native_book","native_xml","native_head_ledger")]
        if not all(entries) or not all(Path(p).is_file() for p in entries):
            return root,dict(state="no_mapping",**empty)
        archive,control,ledger=map(Path,entries)
        original_ledger=digest(ledger)
        program=Path(app)/"piano-core/source_piano_paired_marks.py"
        python=Path(settings["engine_root"])/"ocr-env/Scripts/python.exe"
        log=job/f"source-piano-paired-marks-{uuid4().hex[:8]}.log"
        runner(list(map(str,[python,"-B",program,job])),log,env,cancel,timeout=90,emit=emit)
        evidence=json.loads(log.read_text(encoding="utf-8-sig"))
        cases=evidence["wedges"]+evidence["words"]
        if not cases:
            return root,dict(state="no_proposals",**empty)
        if (digest(archive)!=evidence["native_book_sha256"]
                or digest(control)!=evidence["native_xml_sha256"] or digest(ledger)!=original_ledger):
            raise ValueError("native notation changed during paired marking verification")
        hashes={int(u):h for u,h in evidence["ref_images"].items()}
        for case in cases:
            unit=case["unit"]
            if unit in hashes and hashes[unit]!=case["source_sha256"]:
                raise ValueError("paired marking source digest conflict")
            hashes[unit]=case["source_sha256"]
        if any(digest(job/f"unit-{u:03d}.png")!=h for u,h in hashes.items()):
            raise ValueError("original pixels changed during markings check")
        fixed,report=restore(root,control,evidence)
        total=len(report["words"])+len(report["wedges"])
        if total:
            emit(f"按原谱复核 {len(report['words'])} 处速度文字及 {len(report['wedges'])} 处双谱表渐强渐弱线")
        return fixed,dict(state="restored" if total else "needs_review",**report)
    except InterruptedError:
        raise
    except (OSError,ValueError,RuntimeError,TypeError,KeyError,ArithmeticError,BadZipFile,ET.ParseError) as exc:
        return root,dict(state="needs_review",reason=str(exc),**empty)

