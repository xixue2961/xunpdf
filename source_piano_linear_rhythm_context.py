"""Reflow a source-confirmed short grand-staff figure in the final export."""
from hashlib import file_digest
import json
import os
from pathlib import Path
import shutil

from runtime_resources import check_memory
from source_native_notation import score
from source_piano_linear_rhythm_projection import project


def sha(path):
    with path.open("rb") as f:
        return file_digest(f,"sha256").hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def refine_linear_piano(root, job, work, scripts, app, settings, device, env, cancel, runner, emit):
    job,work,scripts,app=map(Path,(job,work,scripts,app))
    if not any(n.text=="2" for n in root.findall("part/measure/attributes/staves")):
        return root,{"state":"no_grandstaff","changes":[]}
    meta=job/"source-native-notation.json"
    raw=job/"raw/input.omr"
    if not meta.is_file() or not raw.is_file():
        return root,{"state":"no_verified_native_input","changes":[]}
    info=read(meta)
    value=info.get("accepted_native_book")
    if not value:
        return root,{"state":"no_verified_native_input","changes":[]}
    source=Path(value).resolve()
    if not source.is_file() or not source.is_relative_to(job.resolve()):
        return root,{"state":"no_verified_native_input","changes":[]}
    engine=Path(settings["engine_root"])
    python=engine/("ocr-gpu-env" if device=="gpu" else "ocr-env")/"Scripts/python.exe"
    java=engine/settings["java_relative"]/"bin/java.exe"
    cp=os.pathsep.join(str(n) for n in (scripts/"runtime",app/"rhythm-core/runtime",
            app/"piano-core/runtime",app/settings.get("engine_classpath_prefix","engine-classes"),engine/"Audiveris-Bandscore/lib/*"))
    flags=("-Xms64m","-Xmx768m","-XX:MaxMetaspaceSize=192m","-XX:ActiveProcessorCount=2","-Djava.awt.headless=true",
           "--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED","--enable-native-access=ALL-UNNAMED")
    def run(args,label):
        if cancel.is_set():
            raise InterruptedError("已取消钢琴符杠和休止符复核")
        if shutil.disk_usage(work).free<2*1024**3:
            raise RuntimeError("钢琴节奏复核磁盘余量不足")
        if args[0]==java:
            check_memory(work,label)
        runner(list(map(str,args)),work/f"{label}.log",env,cancel,timeout=180,emit=emit)

    try:
        work.mkdir()
        checksum=sha(source)
        raw_digest=sha(raw)
        expected=info.get("accepted_native_sha256")
        if not expected and info.get("source_book")==value:
            expected=info.get("source_book_sha256")
        if expected!=checksum:
            raise ValueError("verified input changed")
        proposal=work/"source-evidence.json"
        plan=work/"plan.tsv"
        run([python,scripts/"source_piano_linear_rhythm_plan.py",source,raw,job,proposal,plan],"source-plan")
        if not plan.is_file():
            return root,{"state":"no_confirmed_figures","changes":[]}
        planhash=sha(plan)
        [evidence]=read(proposal)
        if plan.read_text(encoding="utf8").splitlines()!=evidence["plan"]:
            raise ValueError("plan differs from physical source evidence")
        emit("按原图恢复同型钢琴符杠、漏识休止符及连线")
        for mode in ("control","apply"):
            run([java,*flags,"-cp",cp,"org.audiveris.omr.score.SourceShortPianoRhythm542",source,checksum,
                 job,plan,planhash,work/mode,mode],f"native-{mode}")
        proof=work/"source-curve-evidence.json"
        run([python,scripts/"source_piano_linear_rhythm_marks.py",work/"apply",job,proposal,proof],"source-curve-roles")
        ink=read(proof)
        if sha(source)!=checksum or sha(raw)!=raw_digest or sha(plan)!=planhash:
            raise ValueError("source context changed")
        if (sha(work/"apply/refined.omr")!=ink["book_sha256"] or sha(work/"apply/head-ledger.tsv")!=ink["ledger_sha256"]
                or evidence["source_sha256"]!=ink["source_sha256"]):
            raise ValueError("physical source proof changed")
        repaired,changes=project(root,score(work/"control/result.mxl"),score(work/"apply/result.mxl"),ink)
        return repaired,{"state":"restored","changes":[changes],"plan_sha256":planhash}
    except InterruptedError:
        raise
    except (OSError,ValueError,RuntimeError,KeyError,IndexError,ArithmeticError) as exc:
        return root,{"state":"unresolved","changes":[],"reason":str(exc)}
