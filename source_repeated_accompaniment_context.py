"""Final score projection from a verified repeated accompaniment pattern."""
from pathlib import Path
from xml.etree import ElementTree as ET
import json

from source_native_notation import sha


def canon(node):
    return (node.tag,tuple(sorted(node.attrib.items())),(node.text or "").strip(),tuple(map(canon,node)))


def recover(root, job, settings, actual, env, cancel, runner, emit):
    job=Path(job)
    record=json.loads((job/"source-native-preflight.json").read_text("utf8"))
    early=record.get("source_piano_graph_artifacts",{})
    qualified=[c for c in early.get("changes",[]) if c["unit"]==1 and c["system"]=="1"]
    if len(qualified)!=1:
        return root,dict(state="not_applicable",changes=[])
    preflight=job/"native-preflight"
    book=preflight/"source-inline-common-time/input.omr"
    if not book.is_file() or not record.get("source_inline_common_time",{}).get("changes"):
        book=preflight/"source-barline-splits/input.omr"
    p=Path(settings["engine_root"])/("ocr-gpu-env" if actual=="gpu" else "ocr-env")/"Scripts/python.exe"
    worker=Path(__file__).with_name("reconstruct_repeated_accompaniment.py")
    work=job/"source-repeated-accompaniment"
    work.mkdir(exist_ok=False)
    inp=work/"input.musicxml"
    ET.ElementTree(root).write(inp,encoding="utf-8",xml_declaration=True)
    native_hash,image_hash,input_hash=sha(book),sha(job/"unit-001.png"),sha(inp)
    try:
        args=[p,worker,book,job,inp,work]
        runner(list(map(str,args)),job/"source-repeated-accompaniment.log",env,cancel,timeout=180,emit=emit)
        final=ET.parse(work/"projected.musicxml").getroot()
        report=json.loads((work/"repeated-accompaniment.json").read_text("utf8"))
        if (sha(book)!=native_hash or sha(inp)!=input_hash or sha(job/"unit-001.png")!=image_hash
                or report["source_sha256"]!=image_hash or qualified[0]["source_sha256"]!=image_hash):
            raise ValueError("Repeated accompaniment source changed")
        originals={part.get("id"):part for part in root.findall("part")}
        outputs={part.get("id"):part for part in final.findall("part")}
        bars={report["repaired"],report["continuation"]}
        if list(originals)!=list(outputs) or canon(root.find("part-list"))!=canon(final.find("part-list")):
            raise ValueError("Repeated accompaniment changed part mapping")
        for pid,old in originals.items():
            new=outputs[pid]
            if pid!=report["part"]:
                if canon(old)!=canon(new):raise ValueError("Non-piano content changed")
                continue
            for a,b in zip(old.findall("measure"),new.findall("measure"),strict=True):
                if a.get("number")!=b.get("number") or (a.get("number") not in bars and canon(a)!=canon(b)):
                    raise ValueError("Other accompaniment measures changed")
        emit(f"按原谱重复墨迹恢复钢琴第 {report['repaired']} 小节及其跨小节延音")
        return final,dict(state="restored",changes=[report])
    except InterruptedError:
        raise
    except (OSError,ValueError,RuntimeError,ET.ParseError) as exc:
        return root,dict(state="unresolved",changes=[],reason=str(exc))
