"""Run source-verified SATB repair after a successful piano rhythm projection."""
from hashlib import file_digest
import json
from pathlib import Path

from source_linear_vocal_projection import restore

def _sha(file):
    with file.open("rb") as stream:
        return file_digest(stream,"sha256").hexdigest()

def _read(file):
    return json.loads(file.read_text(encoding="utf8"))

def refine_linear_vocal(root, job, piano, result, app, settings, device, env, cancel, runner, emit):
    if result["state"]!="restored":
        return root,{"state":"no_confirmed_source_bar","changes":[]}
    job,piano,app=map(Path,(job,piano,app))
    proposals=piano/"source-evidence.json"
    ledger=piano/"apply/head-ledger.tsv"
    raw=job/"raw/input.omr"
    files=(proposals,ledger,raw)
    if not all(p.is_file() for p in files) or len(result["changes"])!=1:
        return root,{"state":"no_physical_source_mapping","changes":[]}
    if cancel.is_set():
        raise InterruptedError("已取消歌唱节奏与歌词复核")
    evidence=piano/"vocal-source.json"
    try:
        initial={p:_sha(p) for p in files}
        measure=result["changes"][0]["measure"]
        engine=Path(settings["engine_root"])
        python=engine/("ocr-gpu-env" if device=="gpu" else "ocr-env")/"Scripts/python.exe"
        program=app/"rhythm-core/linear-piano/source_linear_vocal_ink.py"
        runner(list(map(str,[python,program,raw,job,proposals,ledger,measure,evidence])),
               piano/"source-vocal-ink.log",env,cancel,timeout=90,emit=emit)
        ink=_read(evidence)
        entry=_read(proposals)
        if (len(entry)!=1 or ink["source_sha256"]!=entry[0]["source_sha256"] or ink["measure"]!=measure
                or any(_sha(p)!=h for p,h in initial.items())):
            raise ValueError("source evidence changed")
        fixed,change=restore(root,ink)
        emit("已按原图恢复人声漏音、附点和逐字歌词")
        return fixed,{"state":"restored","changes":[change]}
    except InterruptedError:
        raise
    except (OSError,RuntimeError,ValueError,IndexError,KeyError,ArithmeticError) as exc:
        return root,{"state":"needs_review","changes":[],"reason":str(exc)}
