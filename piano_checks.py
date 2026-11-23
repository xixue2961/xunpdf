"""Native piano beam/number cross-check. Flags candidates; never invents timing."""
from pathlib import Path
import zipfile,xml.etree.ElementTree as E,json
import re

def inspect_piano_tuplets(job):
    job=Path(job);findings=[]
    with zipfile.ZipFile(job/'raw/input.omr') as z:
        for path in z.namelist():
            if not path.endswith('.xml') or '/sheet#' not in path:continue
            root=E.fromstring(z.read(path))
            for system in root.findall('page/system'):
                nodes={e.get('id'):e for e in system.iter() if e.get('id') and e.find('bounds') is not None}
                rels=system.findall('.//relation')
                members={}
                for e in rels:
                    if e.find('containment') is not None:members.setdefault(e.get('source'),set()).add(e.get('target'))
                beam_stems={};stem_chords={};linked=set()
                for e in rels:
                    a,b=e.get('source'),e.get('target')
                    if e.find('beam-stem') is not None:beam_stems.setdefault(a,set()).add(b)
                    if e.find('chord-stem') is not None:stem_chords.setdefault(b,set()).add(a)
                    if e.find('chord-tuplet') is not None:linked.add(a)
                piano_ids=set();spacing={}
                for part in system.findall('part'):
                    if len(part.findall('staff'))!=2:continue
                    for st in part.findall('staff'):
                        piano_ids.add(st.get('id'));ys=[float(p.get('y')) for p in st.findall('lines/line/point')]
                        if ys:spacing[st.get('id')]=(max(ys)-min(ys))/4
                for group in system.iter('beam-group'):
                    beams=members.get(group.get('id'),set());stems=set().union(*(beam_stems.get(b,set()) for b in beams));chords=set().union(*(stem_chords.get(s,set()) for s in stems))
                    chords={c for c in chords if c in nodes and nodes[c].get('staff') in piano_ids}
                    if len(chords)<3 or len(chords)>9 or chords<=linked:continue
                    staffids={nodes[c].get('staff') for c in chords}
                    if len(staffids)!=1:continue
                    space=spacing.get(next(iter(staffids)),0)
                    if space<=0:continue
                    bounds=group.find('bounds');x,y,w,h=[float(bounds.get(k)) for k in ('x','y','w','h')]
                    numbers=[]
                    for e in nodes.values():
                        if e.tag not in ('lyric-item','word','number'):continue
                        text=e.get('value','').strip()
                        if text not in ('3','5','6','7','9') or int(text)!=len(chords):continue
                        b=e.find('bounds');nx,ny,nw,nh=[float(b.get(k)) for k in ('x','y','w','h')]
                        if x-space<=nx+nw/2<=x+w+space and -space<=y-(ny+nh)<=3*space:numbers.append(e)
                    if len(numbers)!=1:continue
                    n=numbers[0]
                    findings.append({'source_sheet':path,'system':system.get('id'),'staff':next(iter(staffids)),'digit':n.get('value'),'native_digit_id':n.get('id'),'native_digit_role':n.tag,'beam_group':group.get('id'),'chords':sorted(chords),'source_group_bounds':[x,y,w,h],'status':'possible_unrecognized_tuplet_requires_source_review','timing_changed':False})
    (job/'piano-tuplet-review.json').write_text(json.dumps({'rule':'native-number-beam-group-v1','findings':findings},ensure_ascii=False,indent=2),encoding='utf8')
    return findings



def split_resolved_tuplet_findings(findings, source_report):
    resolved = {(str(row["unit"]), str(row["system"]), str(row["digit"]))
                for row in source_report.get("resolved_sources", ())}
    pending, verified = [], []
    for finding in findings:
        sheet = re.fullmatch(r"sheet#([1-9][0-9]*)/sheet#\1\.xml", finding["source_sheet"])
        key = (sheet[1], str(finding["system"]), str(finding["native_digit_id"])) if sheet else None
        if key in resolved:
            verified.append(dict(finding, status="restored_by_source_tuplet_validation", timing_changed=True))
        else:
            pending.append(finding)
    return pending, verified


def continuous_cross_staff_voices(timelines, meter):
    complete = set()
    for voice, events in timelines.items():
        sounding_staves = {staff for start, end, staff, pitched in events if pitched}
        if len(sounding_staves) < 2:
            continue
        cursor = 0
        for start, end, staff, pitched in sorted(events):
            if start != cursor or end <= start:
                break
            cursor = end
        else:
            if cursor == meter:
                complete.add(voice)
    return complete
