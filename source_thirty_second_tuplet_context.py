"""Read three-beam tuplets after piano-core has mapped the complete grand staff."""
import json
from pathlib import Path

from source_lyric_roles import _score_events
from source_long_tuplet_context import projection, read, sha
from source_tempo_context import _score
from runtime_resources import check_memory


def remove_confirmed_digits(root, changes):
    """Replace a matching misplaced text number with its proven tuplet annotation."""
    events,directions=_score_events(root)
    removed=[]
    for change in changes:
        part,measure=change["part"],change["measure"]
        for tuplet in change["tuplet_groups"]:
            keys=[(part,measure,i) for i in tuplet["note_indices"]]
            first,last=events[keys[0]],events[keys[-1]]
            if first["node"].findtext("type")!="32nd":
                continue
            staff=first["node"].findtext("staff","1")
            picked=[]
            for d in directions:
                if ((d["part"],d["measure"],d["staff"])!=(part,measure,staff)
                        or not first["start"]<d["start"]<=last["start"]):
                    continue
                node=d["node"]
                kinds=node.findall("direction-type")
                words=node.find("direction-type/words")
                shown=first["node"].find("notations/tuplet[@type='start']")
                if (words is not None and len(kinds)==len(kinds[0])==1 and node.find("sound") is None
                        and shown is not None and node.get("placement")==shown.get("placement")
                        and words.get("default-y") is not None and (words.text or "").strip()==str(tuplet["actual"])):
                    picked.append(node)
            if len(picked)>1:
                raise ValueError("more than one eligible duplicate printed tuplet number")
            if picked:
                first["measure"].remove(picked[0])
                removed.append({"part":part,"measure":measure,"digit":str(tuplet["actual"])})
    return removed


def refine_thirty_second_tuplets(root, job, work, app, settings, device, env, cancel, runner, emit=print):
    job,work,app=map(Path,(job,work,app))
    if not any(n.text=="32nd" for n in root.findall("part/measure/note/type")):
        return root,{"state":"no_thirty_second_note","changes":[],"pending":[]}
    path=job/"source-piano-context.json"
    if not path.is_file():
        return root,{"state":"no_mapped_piano_book","changes":[],"pending":[]}
    info=read(path).get("native_recovery",{})
    paths=[info.get(k) for k in ("native_book","native_xml","native_head_ledger")]
    if not all(paths) or not all(Path(p).is_file() for p in paths):
        return root,{"state":"no_mapped_piano_book","changes":[],"pending":[]}
    if cancel.is_set():
        raise InterruptedError("已取消六连音复核")
    book,control,ledger=map(Path,paths)
    try:
        work.mkdir()
        initial={p:sha(p) for p in (book,control,ledger)}
        engine=Path(settings["engine_root"])
        python=engine/("ocr-gpu-env" if device=="gpu" else "ocr-env")/"Scripts/python.exe"
        model=engine/"cache/paddlex/official_models/en_PP-OCRv5_mobile_rec"
        out=work/"source-digit-evidence.json"
        script=app/"rhythm-core/source_long_tuplet_pixels.py"
        check_memory(work,"source-thirty-second-tuplets")
        runner(list(map(str,[python,script,"--book",book,"--source",job,"--model",model,
                              "--output",out,"--thirty-second-only"])),
               work/"scan.log",env,cancel,timeout=120,emit=emit)
        evidence=read(out)
        if evidence["source_book_sha256"]!=initial[book] or any(sha(p)!=h for p,h in initial.items()):
            raise ValueError("native source and detected digit evidence differ")
        picked=evidence["beamed_findings"]
        if not picked:
            return root,{"state":"no_confirmed_groups","changes":[],"pending":evidence["pending"]}
        fixed,changes,pending=projection(root,_score(control),ledger,job,picked,beamed=True)
        if not changes:
            return root,{"state":"needs_review","changes":[],"pending":pending+evidence["pending"]}
        omitted=remove_confirmed_digits(fixed,changes)
        emit(f"按原图恢复 {sum(len(c['tuplet_groups']) for c in changes)} 组三十二分音符连音")
        return fixed,{"state":"restored","changes":changes,"pending":pending+evidence["pending"],
                      "duplicated_digits_omitted":omitted}
    except InterruptedError:
        raise
    except (OSError,RuntimeError,ValueError,IndexError,KeyError,ArithmeticError) as exc:
        return root,{"state":"unresolved","changes":[],"pending":[],"reason":str(exc)}
