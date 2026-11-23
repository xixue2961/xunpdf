"""Offer an original-image-bound second pass for a repeated piano rhythm."""
from hashlib import file_digest
import json
import os
from pathlib import Path
import shutil

from runtime_resources import check_memory
from source_native_notation import score
from source_repeated_beam_projection import project


def _sha(path):
    with path.open("rb") as stream:
        return file_digest(stream,"sha256").hexdigest()


def refine_repeated_beams(root, job, work, app, settings, device, env, cancel, runner, emit):
    job, work, app = map(Path,(job,work,app))
    if not any(s.text=="2" for s in root.findall("part/measure/attributes/staves")):
        return root, {"state":"no_grandstaff","changes":[]}
    context=job/"source-native-notation.json"
    meter=job/"source-tuplet-evidence/meter/source-meters.json"
    if not context.is_file() or not meter.is_file():
        return root, {"state":"no_verified_native_input","changes":[]}
    receipt=json.loads(context.read_text(encoding="utf8"))
    source=receipt.get("accepted_native_book")
    if not source:
        return root, {"state":"no_verified_native_input","changes":[]}
    book=Path(source).resolve()
    if not book.is_file() or not book.is_relative_to(job.resolve()):
        return root, {"state":"no_verified_native_input","changes":[]}
    work.mkdir()
    group=app/"rhythm-core/repeated-beams"
    engine=Path(settings["engine_root"])
    python=engine/("ocr-gpu-env" if device=="gpu" else "ocr-env")/"Scripts/python.exe"
    java=engine/settings["java_relative"]/"bin/java.exe"
    cp=os.pathsep.join(str(p) for p in (group/"runtime",app/"rhythm-core/runtime",app/"piano-core/runtime",
            app/settings.get("engine_classpath_prefix","engine-classes"),engine/"Audiveris-Bandscore/lib/*"))
    flags=("-Xms64m","-Xmx768m","-XX:MaxMetaspaceSize=192m","-XX:ActiveProcessorCount=2","-Djava.awt.headless=true",
           "--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED","--enable-native-access=ALL-UNNAMED")

    def run(argv,name):
        if cancel.is_set():
            raise InterruptedError("已取消钢琴重复音型复核")
        if shutil.disk_usage(work).free<2*1024**3:
            raise RuntimeError("钢琴符杠复核磁盘余量不足")
        if argv[0]==java:
            check_memory(work,name)
        runner(list(map(str,argv)),work/f"{name}.log",env,cancel,timeout=180,emit=emit)

    try:
        checksum=_sha(book)
        expected_digest=receipt.get("accepted_native_sha256")
        if expected_digest is None and receipt.get("source_book")==source:
            expected_digest=receipt.get("source_book_sha256")
        if checksum!=expected_digest:
            raise ValueError("source-native score content changed")
        rough=work/"primary-beam-scan.json"
        run([python,group/"scan_primary_beams.py",book,job,rough],"scan-primary-beams")
        if not json.loads(rough.read_text(encoding="utf8")):
            return root, {"state":"no_candidates","changes":[]}
        plan=work/"source-plan.tsv"
        run([python,group/"source_repeated_beam_plan.py",book,job,rough,meter,plan],"verify-beam-and-head-plan")
        if not plan.is_file():
            return root, {"state":"no_verified_pair","changes":[]}
        plan_sha=_sha(plan)
        emit("按原图恢复钢琴重复音型的符杠、漏音和演奏标记")
        for mode in ("control","apply"):
            run([java,*flags,"-cp",cp,"org.audiveris.omr.score.SourcePrimaryBeams541",
                 book,checksum,job,plan,plan_sha,work/mode,mode],f"native-{mode}")
        evidence=work/"expression.json"
        run([python,group/"source_repeated_beam_expression.py",work/"apply",job,plan,evidence],"verify-expression")
        proof=json.loads(evidence.read_text(encoding="utf8"))
        # The supported repeated figure has complete source evidence for both hands.
        from collections import Counter
        counts=Counter((r["measure"],r["staff"],k) for r in proof["articulations"] for k in r["marks"])
        bars={r["measure"] for r in proof["articulations"]}
        expected=Counter({(m,staff,kind):count for m in bars for staff,kind,count in
                          (("1","accent",4),("2","accent",8),("2","staccato",8))})
        if len(bars)!=2 or counts!=expected:
            raise ValueError("original printed articulations cannot all be confirmed")
        if _sha(book)!=checksum or _sha(plan)!=plan_sha:
            raise ValueError("source evidence changed during native recovery")
        fixed, report=project(root,score(work/"control/result.mxl"),score(work/"apply/result.mxl"),proof)
        return fixed, {"state":"restored","changes":[report],"source_plan_sha256":plan_sha}
    except InterruptedError:
        raise
    except (OSError, RuntimeError, ValueError, KeyError, ArithmeticError) as exc:
        return root, {"state":"unresolved","changes":[],"reason":str(exc)}
