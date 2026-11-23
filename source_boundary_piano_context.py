"""Recheck grand-staff page openings before writing the final MusicXML."""
from pathlib import Path
import hashlib
import json
import os
import sys

from source_boundary_piano_projection import project_boundary_repair, sha
from source_boundary_rest_plan import verified_rests


def refine_boundary_piano(root, source_job, work, app, settings, device, env, cancel, runner, emit):
    source_job, work, app = map(Path, (source_job, work, app))
    if not any(e.text == "2" for e in root.findall("part/measure/attributes/staves")):
        return root, dict(state="no_grandstaff", changes=[])
    control = source_job / "source-native-notation/time-head-conflicts-native"
    seed = control / "refined.omr"
    raw = source_job / "raw/input.omr"
    if not all(x.is_file() for x in (seed, raw, control / "result.mxl", control / "note-ledger.tsv")):
        return root, dict(state="no_verified_source_control", changes=[])

    engine = Path(settings["engine_root"])
    java = engine / settings["java_relative"] / "bin/java.exe"
    lib = app / "rhythm-core"
    python = engine / ("ocr-gpu-env" if device == "gpu" else "ocr-env") / "Scripts/python.exe"
    prefix = app / settings.get("engine_classpath_prefix", "engine-classes")
    classpath = os.pathsep.join(map(str, (lib/"boundary-runtime", lib/"runtime", app/"piano-core/runtime",
                                        prefix, engine/"Audiveris-Bandscore/lib/*")))
    flags = ("-Xms64m", "-Xmx768m", "-XX:MaxMetaspaceSize=192m", "-XX:ReservedCodeCacheSize=64m",
             "-XX:MaxDirectMemorySize=64m", "-Xss512k", "-XX:ActiveProcessorCount=2", "-Djava.awt.headless=true",
             "--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED", "--enable-native-access=ALL-UNNAMED")
    work.mkdir(parents=True, exist_ok=False)

    from runtime_resources import check_memory

    def run(arguments,label):
        if cancel.is_set():
            raise InterruptedError("已取消钢琴跨页核对")
        runner(list(map(str, arguments)), work/f"{label}.log", env, cancel, timeout=240, emit=emit)

    def native(class_name,arguments,label):
        check_memory(work,label)
        run([java,*flags,"-cp",classpath,f"org.audiveris.omr.score.{class_name}",*arguments],label)

    try:
        emit("按原谱核对钢琴跨页的拍号、休止和延音")
        scan = work / "meter-scan"
        run([python,lib/"source_boundary_meter_conflicts.py",seed,source_job,scan],"meter-ink")
        findings = json.loads((scan/"source-time-heads.json").read_text(encoding="utf-8"))
        if findings["source_book_sha256"] != sha(seed):
            raise ValueError("page-opening proof belongs to a different score")
        plan=scan/"plan.tsv"
        if not plan.stat().st_size:
            return root, dict(state="no_boundary_candidates", changes=[])
        meter_stage=work/"native-meter"
        native("SourceTimeHeads425",[seed,sha(seed),source_job,plan,sha(plan),meter_stage,"apply"],"native-meter")
        decisions=meter_stage/"time-head-decisions.tsv"
        meter_book=meter_stage/"refined.omr"
        rests=verified_rests(raw,meter_book,decisions,source_job)
        if not rests:
            return root, dict(state="unresolved_source_rest", changes=[])
        rest_plan=work/"rest-plan.tsv"
        with rest_plan.open("x",encoding="utf-8") as stream:
            for row in rests:
                stream.write("\t".join(map(str,row))+"\n")
        rest_stage=work/"native-rest"
        native("SourceMissedRest403",[meter_book,sha(meter_book),raw,sha(raw),source_job,rest_plan,
                                      rest_stage,"apply"],"native-rest")
        fixed,changes=project_boundary_repair(root,control,rest_stage,decisions,source_job)
        return fixed, dict(state="restored" if changes else "no_verified_projection", changes=changes)
    except InterruptedError:
        raise
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        emit("钢琴跨页复核证据不足，保留本次识别结果")
        return root, dict(state="unresolved", changes=[], reason=str(exc))

