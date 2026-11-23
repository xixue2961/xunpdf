"""Recheck cross-page vocal notes from paired source arcs before export."""
from pathlib import Path
from hashlib import file_digest
import json
import os
import shutil

from source_accidental_lyric_roles import restore_flat_roles
from source_curved_rest_overlay import load, rows, transfer
from source_boundary_tie import repair


def sha(path):
    with path.open("rb") as f:
        return file_digest(f, "sha256").hexdigest()


def refine_vocal_boundary(root, source_job, work, app, stage_package, settings, device, env, cancel, runner, emit):
    source_job,work,app,stage_package=map(Path,(source_job,work,app,stage_package))
    receipt=source_job/"source-native-notation.json"
    if not receipt.is_file():
        return root,dict(state="no_verified_source_control",changes=[],flat_lyrics=[])
    context=json.loads(receipt.read_text(encoding="utf8"))
    source_book=context.get("accepted_native_book")
    if not source_book:
        return root,dict(state="no_verified_source_control",changes=[],flat_lyrics=[])
    seed=Path(source_book).resolve()
    native=seed.parent
    if not seed.is_relative_to(source_job.resolve()) or not all(f.is_file() for f in (
            seed,native/"head-ledger.tsv",native/"note-ledger.tsv",native/"result.mxl")):
        return root,dict(state="no_verified_source_control",changes=[],flat_lyrics=[])
    work.mkdir(parents=True,exist_ok=False)
    engine=Path(settings["engine_root"])
    python=engine/("ocr-gpu-env" if device=="gpu" else "ocr-env")/"Scripts/python.exe"
    java=engine/settings["java_relative"]/"bin/java.exe"
    cp=os.pathsep.join(map(str,(stage_package/"runtime",app/"rhythm-core/runtime",app/"piano-core/runtime",
                               app/settings.get("engine_classpath_prefix","engine-classes"),
                               engine/"Audiveris-Bandscore/lib/*")))
    flags=("-Xms64m","-Xmx768m","-XX:MaxMetaspaceSize=192m","-XX:ReservedCodeCacheSize=64m",
           "-XX:ActiveProcessorCount=2","-Djava.awt.headless=true",
           "--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED","--enable-native-access=ALL-UNNAMED")
    from runtime_resources import check_memory
    def run(args,label):
        if cancel.is_set():
            raise InterruptedError("已取消人声跨页核对")
        if shutil.disk_usage(work).free<2*1024**3:
            raise RuntimeError("人声复核磁盘余量不足")
        if Path(args[0])==java:
            check_memory(work,label)
        runner(list(map(str,args)),work/f"{label}.log",env,cancel,timeout=240,emit=emit)

    fixed=root
    changes=[]
    detail={}
    try:
        seed_hash=sha(seed)
        inbox=work/"scan"
        run([python,stage_package/"source_paired_curved_rests.py",seed,source_job,native/"note-ledger.tsv",inbox],
            "source-curved-rests")
        proof=json.loads((inbox/"paired-curved-rest-plan.json").read_text(encoding="utf8"))
        if proof["pairs"]:
            emit("按原谱复核跨页的人声休止符和延音")
            control,recovered=(work/f"rest-{mode}" for mode in ("control","apply"))
            for mode,destination in (("control",control),("apply",recovered)):
                run([java,*flags,"-cp",cp,"org.audiveris.omr.score.SourceCurvedRest400",seed,seed_hash,
                    source_job,inbox/"retire-plan.tsv",destination,mode],f"curved-rest-{mode}")
            fixed,restoration=transfer(root,load(native/"result.mxl"),load(control/"result.mxl"),
                                      load(recovered/"result.mxl"),proof,rows(native/"note-ledger.tsv"),
                                      rows(recovered/"note-ledger.tsv"))
            changes.extend(restoration["changes"])
            ties=work/"ties.tsv"
            with ties.open("x",encoding="utf8") as f:
                for i,pair in enumerate(proof["pairs"]):
                    for side in ("before","after"):
                        q=pair[side]
                        anchor=q["anchor"]
                        line=(i,side,q["unit"],q["system"],q["rest_glyph"],anchor["head"],anchor["head_glyph"],
                              *anchor["head_bounds"],*q["rest_bounds"])
                        f.write("\t".join(map(str,line))+"\n")
            book=recovered/"refined.omr"
            tie_control,tie_apply=(work/f"tie-{mode}" for mode in ("control","apply"))
            for mode,destination in (("control",tie_control),("apply",tie_apply)):
                run([java,*flags,"-cp",cp,"org.audiveris.omr.score.SourceBoundaryTie401",book,sha(book),
                     source_job,ties,destination,mode],f"boundary-tie-{mode}")
            meter=json.loads((source_job/"source-meter-evidence/source-time-plan.json").read_text(encoding="utf8"))
            fixed,tied=repair(fixed,load(tie_control/"result.mxl"),load(tie_apply/"result.mxl"),proof,
                             rows(tie_control/"note-ledger.tsv"),rows(tie_apply/"note-ledger.tsv"),meter)
            changes.extend(tied["changes"])
        fixed,flat=restore_flat_roles(fixed,native,source_job)
        state="restored" if changes or flat else "no_verified_projection"
        return fixed,dict(state=state,changes=changes,flat_lyrics=flat)
    except InterruptedError:
        raise
    except (OSError,ValueError,RuntimeError,KeyError) as exc:
        emit("人声跨页复核证据不足，已保留先前通过校验的内容")
        return fixed,dict(state="partially_restored" if changes else "unresolved",
                          changes=changes,flat_lyrics=[],reason=str(exc))

