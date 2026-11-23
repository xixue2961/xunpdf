
"""Validate native cross-staff reconstruction and capture only a verified rest lane."""
from collections import Counter
from fractions import Fraction as F
import copy
import csv
import json
from xml.etree import ElementTree as E
from source_native_notation import canonical, score, sha


def _music_events(measure,divisions):
    cursor=F(0)
    onset=F(0)
    out=[]
    for node in measure:
        count=F(node.findtext('duration','0'))/divisions
        if node.tag=='backup':
            cursor-=count
        elif node.tag=='forward':
            cursor+=count
        elif node.tag=='note':
            if node.find('chord') is None:
                onset=cursor
                cursor+=count
            out.append((onset,count,node))
    return out


def _musical_identity(bar,divisions):
    return [tuple((str(t),str(d),n.findtext('voice','1'),n.findtext('staff','1'),
                   n.findtext('pitch/step'),n.findtext('pitch/octave'),n.findtext('pitch/alter','0')))
            for t,d,n in _music_events(bar,divisions) if n.find('pitch') is not None]


def measures(root,pid,number):
    part=root.find(f"part[@id='{pid}']")
    div=1
    for ordinal,m in enumerate(part.findall('measure'),1):
        div=int(m.findtext('attributes/divisions',str(div)))
        if m.get('number')==number:
            yield ordinal,m,div


def _one_target_change(previous, changed, pid, ordinal):
    if tuple(canonical(n) for n in previous if n.tag!='part')!=tuple(canonical(n) for n in changed if n.tag!='part'):
        raise ValueError('cross-staff reconstruction changed document metadata')
    edits=[]
    for a,b in zip(previous.findall('part'),changed.findall('part'),strict=True):
        if a.attrib!=b.attrib:
            raise ValueError('part identity changed during cross-staff reconstruction')
        for i,(x,y) in enumerate(zip(a.findall('measure'),b.findall('measure'),strict=True),1):
            if canonical(x)!=canonical(y):
                edits.append((a.get('id'),i))
    if edits!=[(pid,ordinal)]:
        raise ValueError(f'cross-staff re-export changed unexpected bars: {edits}')


def verify_cross_staff(previous,patched,ledger,proof):
    old,new=score(previous),score(patched)
    supported=[h for run in proof['grouped_beams'] for h in run]
    with ledger.open(encoding='utf8',newline='') as f:
        records=[v for v in csv.DictReader(f,delimiter='	')
                 if v['unit']==str(proof['unit']) and v['system']==proof['system'] and v['repeat_copy']=='false'
                 and (v['chord'] in supported or v['head'] in
                      (proof['preserved_source_head'],proof['discarded_source_head']))]
    align={(x['part'],x['measure']) for x in records}
    if len(align)!=1 or len(records)!=14 or len({x['chord'] for x in records})!=14:
        raise ValueError('recovered cross-staff source did not retain its full note mapping')
    if proof['discarded_source_head'] in {x['head'] for x in records}:
        raise ValueError('the ink of an obscured rest remained a sounding note')
    [(pid,num)]=align
    [(ordinal,bar,divisions)]=list(measures(new,pid,num))
    _one_target_change(old,new,pid,ordinal)
    onsets=_music_events(bar,divisions)
    pitched=[(start,duration,n) for start,duration,n in onsets if n.find('pitch') is not None]
    if len(pitched)!=25 or len(bar.findall('note/notations/slur'))!=2:
        raise ValueError('recovered piano bar lacks source notes or connected phrasing')
    if [(a.get('type'),a.get('number')) for a in bar.findall('note/notations/slur')] not in [
        [('start',slur.get('number')),('stop',slur.get('number'))]
        for slur in bar.findall("note/notations/slur[@type='start']")]:
        raise ValueError('the printed cross-staff slur did not connect')
    notes=bar.findall('note')
    locations={v['chord']:int(v['note_index']) for v in records}
    expected_times=[F(i,4) for i in (*range(2,8),*range(9,16))]
    actual_times={id:onsets[locations[id]][0:2] for id in supported}
    if [actual_times[id] for id in supported]!=[(start,F(1,4)) for start in expected_times]:
        raise ValueError('a beamed note did not survive at its original spacing')
    if not any((t,d,n.findtext('staff'),n.findtext('type'))==(F(2),F(1,4),'2','16th')
               for t,d,n in onsets if n.find('rest') is not None):
        raise ValueError('the original rest did not start the lower run')
    if max(t+d for t,d,_ in onsets)!=4:
        raise ValueError('cross-staff recovery did not last four beats')
    return dict(part=pid,physical_measure=ordinal,musicxml_measure=num,pitched=25,
                original_slur_connected=True)


def extract_rest_lane(patched,alternative,scope):
    before,after=score(patched),score(alternative)
    pid,num,ord=scope['part'],scope['musicxml_measure'],scope['physical_measure']
    _one_target_change(before,after,pid,ord)
    [(_,a,da)]=list(measures(before,pid,num))
    [(_,b,db)]=list(measures(after,pid,num))
    if da!=db:
        raise ValueError('the supplemental engraved rest lane changed divisions')
    first=next(i for i,n in enumerate(b) if n.tag=='backup')+1
    end=next(i for i in range(first,len(b)) if b[i].tag=='backup')+1
    block=list(b)[first:end]
    if [n.tag for n in block]!=['forward','note','note','forward','note','backup']:
        raise ValueError('unverified engraved rest block structure')
    if [n.findtext('type') for n in block if n.tag=='note']!=['eighth','16th','quarter']:
        raise ValueError('the native rest lane does not match the original engraving')
    if any(n.find('rest') is None or n.findtext('staff')!='1' for n in block if n.tag=='note'):
        raise ValueError('the rest-only lane includes an unsupported event')
    for n in block:
        b.remove(n)
    if canonical(a)!=canonical(b):
        raise ValueError('the alternative rest export changed unrelated notation')
    return dict(part=pid,physical_measure=ord,musicxml_measure=num,divisions=da,
                anchor=_musical_identity(a,da),fragment=[E.tostring(n,encoding='unicode').strip() for n in block])


def try_cross_staff(job,work,current,xml,java,alternate_java,python,scanner,run):
    work.mkdir()
    archive=work/'refined.omr'
    proof=work/'source-cross-staff-proof.json'
    h=sha(current)
    run([python,scanner,current,job,archive,proof],'cross-staff-scan')
    findings=json.loads(proof.read_text(encoding='utf8'))
    if h!=sha(current):
        raise ValueError('cross-staff repair changed the original OMR')
    if not findings:
        return None
    [witness]=findings
    if sha(job/f"unit-{witness['unit']:03d}.png")!=witness['source_sha256']:
        raise ValueError('cross-staff repair source image changed')
    def export(command,folder,label):
        dst=work/folder
        run([*command,'org.audiveris.omr.score.SourceNativeLedger404',archive,sha(archive),job,dst],label)
        return dst
    normal=export(java,'export','cross-staff-export')
    fixed=normal/'result.mxl'
    delta=verify_cross_staff(xml,fixed,normal/'note-ledger.tsv',witness)
    supplementary=export(alternate_java,'engraved-rests','cross-staff-rest-lane')
    lane=extract_rest_lane(fixed,supplementary/'result.mxl',delta)
    return archive,fixed,dict(stage='cross-staff-piano',projection=delta,source_proof=str(proof),engraved_rest_lane=lane)

