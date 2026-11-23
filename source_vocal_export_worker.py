
"""Apply source-confirmed vocal clock, note, tie and lyric corrections."""
from pathlib import Path
import argparse
import csv
import json
import sys
import xml.etree.ElementTree as ET

from source_tempo_context import _score
from source_vocal_candidates import requests


def load(path):
    return json.loads(path.read_text(encoding='utf8'))


def target_chords(ledger, keys):
    wanted=set(keys)
    with ledger.open(encoding='utf8') as stream:
        return {(int(row['unit']),row['system'],row['chord']) for row in csv.DictReader(stream,delimiter='\t')
                if row['repeat_copy']=='false' and (row['part'],row['measure'],int(row['note_index'])) in wanted}


def main():
    parser=argparse.ArgumentParser()
    for name in ('input','control','book','source','ledger','phonetics','work'):
        parser.add_argument(name,type=Path)
    args=parser.parse_args()
    score=ET.parse(args.input).getroot()
    native=_score(args.control)
    flags,boxed,phrases=requests(score)
    report={'flag_durations':[],'chord_ties':[],'boxed_symbol_lanes':[],'lyric_phrases':[]}
    pending=[]
    if flags:
        addresses=target_chords(args.ledger,flags)
        if addresses:
            sys.path.insert(0,str(Path(__file__).resolve().parent/'rhythm-core'))
            from source_missing_flags import scan
            from single_flag_clock import repair as fix_flags
            from source_chord_tie import repair as fix_ties

            witness=scan(args.book,args.source,args.work/'flag-ink',target_addresses=addresses)
            score,report['flag_durations']=fix_flags(score,native,args.book,args.source,args.ledger,witness)
            score,report['chord_ties']=fix_ties(score,args.book,args.ledger,args.source,report['flag_durations'])
    if boxed:
        from source_box_symbol_clock import repair as fix_boxed_symbols

        score,report['boxed_symbol_lanes']=fix_boxed_symbols(score,native,args.book,args.ledger,args.source)
    if phrases or report['boxed_symbol_lanes']:
        from source_choral_phrase import repair as fix_phrases

        try:
            fonts=load(args.phonetics/'source-fonts.json')
            chars=load(args.phonetics/'source-characters.json')
            score,report['lyric_phrases']=fix_phrases(score,native,args.book,args.source,args.ledger,fonts,chars)
        except (OSError,ValueError) as exc:
            pending.append(dict(stage='lyric_phrases',reason=str(exc)))
    any_changes=any(report.values())
    if any_changes:
        ET.ElementTree(score).write(args.work/'output.musicxml',encoding='utf-8',xml_declaration=True)
    state='partially_restored' if any_changes and pending else 'restored' if any_changes else 'needs_review' if pending else 'no_supported_change'
    with (args.work/'changes.json').open('x',encoding='utf8') as stream:
        json.dump(dict(state=state,changes=report,pending=pending),stream,ensure_ascii=False,indent=2)


if __name__=='__main__':
    main()

