'''Combine verified printed short lyrics, vocal clocks and octave-clef evidence.'''
from pathlib import Path
from xml.etree import ElementTree as ET
import json
import sys

from source_flat_arc_false_bass_clef_v2 import recover as recover_false_bass
from source_octave_clef_projection_v2 import digest, project, read_score, source_proof
from source_short_packed_lyrics_v3 import recover as recover_short_lyrics
from source_watermarked_vocal_pair import recover as recover_vocal_pair


def refine(root, job, native_book):
    job, native_book = Path(job), Path(native_book)
    corrected, lyrics = recover_short_lyrics(root, job)
    projection = []
    last = []
    flags = []
    scan_path = job / "source-clefs.json"
    meter_path = job / "source-meter-context.json"
    if all(p.is_file() for p in (scan_path, meter_path, native_book, native_book.with_suffix(".mxl"))):
        scan = json.loads(scan_path.read_text("utf8"))
        meter = json.loads(meter_path.read_text("utf8"))
        if digest(native_book) != meter.get("source_book_sha256"):
            flags.append("native_clef_source_hash_conflict")
        elif any(f.get("status") == "missing_clef_candidate" for f in scan["findings"]):
            try:
                systems, proofs = source_proof(native_book, job, scan)
                reference = read_score(native_book.with_suffix(".mxl"))
                corrected, projection = project(corrected, reference, systems, proofs)
                for one in projection:
                    corrected, tail = recover_false_bass(corrected, job, one, systems)
                    if tail:
                        last.append(tail)
            except (OSError, ValueError, KeyError, ET.ParseError) as error:
                flags.append(str(error))
    corrected, rhythm = recover_vocal_pair(corrected, job, lyrics)
    report = dict(short_lyrics=lyrics["changes"], octave_projection=projection,
                  false_bass_repairs=last, watermarked_pairs=rhythm, pending=flags)
    report["state"] = "restored" if any(v for k, v in report.items() if k != "pending") else "unchanged"
    return corrected, report


if __name__ == "__main__":
    job, book, source, output, report_path = map(Path, sys.argv[1:])
    source_hash = digest(source)
    root, report = refine(read_score(source), job, book)
    if digest(source) != source_hash:
        raise ValueError("input MusicXML changed during vocal verification")
    with output.open("xb") as f:
        ET.ElementTree(root).write(f, encoding="utf-8", xml_declaration=True)
    report.update(input_sha256=source_hash, output_sha256=digest(output))
    with report_path.open("x", encoding="utf8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(report["state"], {k: len(v) for k, v in report.items() if isinstance(v, list)})
