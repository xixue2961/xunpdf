
"""Verify source-pixel evidence before supplementing a partial piano arpeggio."""
from hashlib import file_digest
import json
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import BadZipFile

from source_rolled_hollow_projection import recover


def digest(file):
    with file.open("rb") as stream:
        return file_digest(stream,"sha256").hexdigest()


def refine_rolled_hollow(root, job, app, settings, env, cancel, runner, emit):
    job=Path(job)
    receipt=job/"source-native-notation.json"
    if not receipt.is_file():
        return root,{"changes":[],"state":"no_source_mapping"}
    if cancel.is_set():
        raise InterruptedError("已取消原图琶音复核")
    try:
        native=json.loads(receipt.read_text(encoding="utf8"))
        archive=Path(native["accepted_native_book"])
        score=Path(native["accepted_native_xml"])
        tmp=job/f"source-rolled-hollow-heads-{uuid4().hex[:8]}.log"
        executable=Path(settings["engine_root"])/"ocr-env/Scripts/python.exe"
        script=Path(app)/"piano-core/source_rolled_hollow_heads.py"
        runner(list(map(str,[executable,"-B",script,job])),tmp,env,cancel,timeout=90,emit=emit)
        evidence=json.loads(tmp.read_text(encoding="utf-8-sig"))
        if not evidence["findings"]:
            return root,{"changes":[],"state":"no_proposals","scanned":evidence["standard_candidate_chords_scanned"]}
        if digest(archive)!=evidence["source_book_sha256"]:
            raise ValueError("source archive changed during rolled chord scan")
        for case in evidence["findings"]:
            image=job/f'unit-{case["unit"]:03d}.png'
            if digest(image)!=case["source_sha256"]:
                raise ValueError("source image changed during rolled chord scan")
        fixed,report=recover(root,score,evidence)
        report["state"]="restored" if report["changes"] else "needs_review"
        if report["changes"]:
            emit(f'按原谱补回 {sum(len(v["new_pitches"]) for v in report["changes"])} 个钢琴琶音和弦音')
        return fixed,report
    except InterruptedError:
        raise
    except (OSError,RuntimeError,ValueError,TypeError,KeyError,AttributeError,BadZipFile,ET.ParseError) as exc:
        return root,{"changes":[],"state":"needs_review","reason":str(exc)}

