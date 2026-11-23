"""Reconstruct supported two-stroke tremolos and ties at system boundaries."""
from pathlib import Path
from hashlib import file_digest
import json
import os
import shutil

from source_system_tremolo_projection import restore_tremolo_span


def sha(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def refine_system_tremolo(root, source_job, work, app, stage_package, settings,
                          device, env, cancel, runner, emit):
    if not any(e.text == "2" for e in root.findall("part/measure/attributes/staves")):
        return root, dict(state="no_grandstaff", changes=[])
    source_job, work, app, stage_package = map(Path,(source_job,work,app,stage_package))
    receipt = source_job/"source-native-notation.json"
    if not receipt.is_file():
        return root, dict(state="no_verified_source_control",changes=[])
    native_context = json.loads(receipt.read_text(encoding="utf-8"))
    seed_value = native_context.get("accepted_native_book")
    if not seed_value:
        return root, dict(state="no_verified_source_control",changes=[])
    seed = Path(seed_value).resolve()
    if not seed.is_relative_to(source_job.resolve()) or not seed.is_file():
        return root, dict(state="no_verified_source_control",changes=[])
    work.mkdir(parents=True,exist_ok=False)

    from runtime_resources import check_memory

    engine=Path(settings["engine_root"])
    py_path=engine/("ocr-gpu-env" if device=="gpu" else "ocr-env")/"Scripts/python.exe"
    java=engine/settings["java_relative"]/"bin/java.exe"
    cp=os.pathsep.join(map(str,(stage_package/"runtime",app/"rhythm-core/runtime",app/"piano-core/runtime",
                              app/settings.get("engine_classpath_prefix","engine-classes"),
                              engine/"Audiveris-Bandscore/lib/*")))
    flags=("-Xms64m","-Xmx768m","-XX:MaxMetaspaceSize=192m","-XX:ReservedCodeCacheSize=64m",
           "-XX:MaxDirectMemorySize=64m","-Xss512k","-XX:ActiveProcessorCount=2","-Djava.awt.headless=true",
           "--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED","--enable-native-access=ALL-UNNAMED")
    def run(args,name):
        if cancel.is_set():
            raise InterruptedError("已取消钢琴震音复核")
        if shutil.disk_usage(work).free<2*1024**3:
            raise RuntimeError("钢琴震音复核磁盘余量不足")
        runner(list(map(str,args)),work/f"{name}.log",env,cancel,timeout=240,emit=emit)

    try:
        plan=work/"native-plan.tsv"
        seed_hash=sha(seed)
        run([py_path,stage_package/"plan_double_whole_tremolo.py",seed,source_job,plan],"two-stroke-ink")
        if not plan.stat().st_size:
            return root,dict(state="no_boundary_candidates",changes=[])
        emit("按原谱核对钢琴两笔震音与跨行延音")
        for mode in ("control","apply"):
            check_memory(work,f"two-stroke-{mode}")
            run([java,*flags,"-cp",cp,"org.audiveris.omr.score.SourceDoubleWhole532",seed,seed_hash,
                 source_job,plan,sha(plan),work/mode,mode],f"native-{mode}")
        if sha(seed)!=seed_hash:
            raise ValueError("source native book changed")
        fixed,changes=restore_tremolo_span(root,work/"apply",work/"control",source_job)
        return fixed,dict(state="restored" if changes else "no_verified_projection",changes=changes)
    except InterruptedError:
        raise
    except (OSError,ValueError,RuntimeError,KeyError) as exc:
        emit("钢琴震音与跨行延音证据不足，保留本次识别结果")
        return root,dict(state="unresolved",changes=[],reason=str(exc))

