"""Recover half-beat gaps where parallel staves prove a split vocal clock."""
from collections import defaultdict
from copy import deepcopy
import csv
from fractions import Fraction as F
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import score, sha


def quarter_meter_columns(result, x):
    prior = [c for c in result['source_columns'] if c['x'] < x]
    if not prior:
        return False
    col = max(prior, key=lambda c: c['x'])
    return (col['time'] == '2/4' and not col['conflicting_time_at_same_column']
            and len({v['staff'] for v in col['votes'] if v['score'] >= .85}) >= 3)


def coordinate(node):
    return tuple(int(node.find('bounds').get(k)) for k in ('x','y','w','h'))


def native_anchor(row, nodes):
    node = nodes.get(row['head'])
    return (node is not None and node.get('shape') == row['shape']
            and node.get('staff') == row['staff']
            and coordinate(node) == tuple(int(row[k]) for k in ('x','y','w','h')))


def pitch(node):
    return tuple(node.findtext('pitch/'+k,'') for k in ('step','alter','octave'))


def text(node):
    han = {c for x in node.findall('lyric/text') for c in (x.text or '') if '\u3400'<=c<='\u9fff'}
    return next(iter(han)) if len(han)==1 else None


def recover(root, job):
    job = Path(job)
    meta = json.loads((job/'source-native-notation.json').read_text('utf8'))
    book, ctl = (Path(meta[k]) for k in ('accepted_native_book','accepted_native_xml'))
    expected = meta.get('accepted_native_sha256') or (meta.get('source_book_sha256') if meta.get('source_book') == str(book) else None)
    if not expected or sha(book)!=expected:
        raise ValueError('split clock source hash mismatch')
    proof = json.loads((job/'source-tuplet-evidence/meter/source-meters.json').read_text('utf8'))
    meters = {(str(v['unit']),str(v['system'])):v for v in proof['results']}
    by_staff = defaultdict(list)
    with ctl.with_name('note-ledger.tsv').open(encoding='utf8',newline='') as f:
        for row in csv.DictReader(f,delimiter='\t'):
            if row['repeat_copy']=='false' and row['implicit']=='false':
                k = tuple(row[x] for x in ('unit','system','measure','part','staff'))
                by_staff[k].append(row)
    groups = defaultdict(list)
    for k, rows in by_staff.items():
        if len(rows) in (3,5):
            groups[k[:3]].append((k,sorted(rows,key=lambda r:int(r['note_index']))))
    raw,_ = _score_events(score(ctl)); changed = deepcopy(root); events,dirs = _score_events(changed)
    def identify(k, rows):
        unit,system,bar,pid,staff=k
        ev=[events.get((pid,bar,i)) for i in range(len(rows))]
        native=[raw.get((pid,bar,i)) for i in range(len(rows))]
        if any(v is None for v in ev+native) or (pid,bar,len(rows)) in events:
            return
        pitches=[(pitch(a['node']),pitch(b['node'])) for a,b in zip(ev,native)]
        if any(a[:2]!=b[:2] or (not a[2])!=(not b[2]) for a,b in pitches):return
        shifts={int(a[2])-int(b[2]) for a,b in pitches if a[2]}
        # Clef normalization can move a complete transposing vocal staff by one octave.
        if len(shifts)!=1 or not shifts.issubset({-1,0,1}):return
        if any(
               a['node'].findtext('voice','1')!=b['node'].findtext('voice','1') or
               a['node'].find('chord') is not None for a,b in zip(ev,native)):
            return
        m = ev[0]['measure']
        t=m.find('attributes/time')
        if t is None or (t.findtext('beats'),t.findtext('beat-type'))!=('2','4'):
            return
        if any(x['node'].find('unpitched') is not None or x['node'].find('grace') is not None for x in ev):
            return
        ns=[x['node'] for x in ev]
        rests=[n.find('rest') is not None for n in ns]
        starts=[x['start'] for x in ev]; durations=[x['duration'] for x in ev]
        word=text(ns[-2] if len(ns)==5 else ns[-1])
        if not word:return
        if len(rows)==3 and rests==[False,True,False] and starts==[0,F(1,2),1] and durations==[F(1,2),F(1,2),1]:
            return 'peer',word,ev
        if len(rows)==5 and rests==[False,True,False,False,False] and starts==[0,F(1,2),0,F(1,2),F(1,2)] and durations[0]==durations[2]==F(1,2) and durations[3:]==[1,1] and ns[1].findtext('type')=='eighth' and durations[1] in (F(1,2),F(3,4)):
            if [n.findtext('voice','1') for n in ns]!=['1','1','2','2','3']:return
            return 'split',word,ev
    result=[]; pages={}
    with ZipFile(book) as z:
        for (u,s,bar),staves in groups.items():
            meter=meters.get((u,s))
            if not meter:continue
            marked=[(k,rows,*v) for k,rows in staves if (v:=identify(k,rows))]
            for k,rows,role,word,ev in marked:
                if role!='split':continue
                x=[int(r['x']) for r in rows]; h=max(int(r['w']) for r in rows)
                if max(abs(x[0]-x[2]),abs(x[3]-x[4]))>h/4 or not x[0]+h<x[1]<x[3]-h or not quarter_meter_columns(meter,x[0]):
                    continue
                align=lambda other: max(abs(x[i]-int(r['x'])) for i,r in zip((0,1,3),other)) <= h/3
                peers=[(pk,rs) for pk,rs,pr,pw,_ in marked if pk!=k and pw==word and
                       ((pr=='peer' and align(rs)) or (pr=='split' and align([rs[i] for i in (0,1,3)])))]
                kinds={pk:pr for pk,_,pr,_,_ in marked}
                if len(peers)<2 or not any(kinds[pk]=='peer' for pk,_ in peers):continue
                if u not in pages:pages[u]=ET.fromstring(z.read(f'sheet#{u}/sheet#{u}.xml'))
                system=pages[u].findall('.//system')[int(s)-1]
                nodes={n.get('id'):n for n in system.iter() if n.get('id') is not None and n.find('bounds') is not None}
                if any(not native_anchor(r,nodes) for r in rows+[r for _,rs in peers for r in rs]):continue
                m=ev[0]['measure']; notes=[v['node'] for v in ev]
                backups=m.findall('backup'); forwards=m.findall('forward')
                if len(backups)!=2 or len(forwards)!=1 or [n.tag for n in m if n.tag in ('note','backup','forward')]!=['note','note','backup','note','note','backup','forward','note']:continue
                div=int(notes[0].findtext('duration'))*2
                if div<=0 or int(notes[3].findtext('duration'))!=div:continue
                # Future measure directions are untouched; allow only prelude or first-note markings here.
                if any(list(m).index(n)>list(m).index(notes[1]) for n in m.findall('direction')):continue
                dot=notes[1].find('dot'); dotted=ev[1]['duration']!=F(1,2)
                if dotted:
                    links=[rel for rel in system.iter('relation') if rel.get('target')==rows[1]['head'] and rel.find('augmentation') is not None]
                    if dot is None or len(notes[1].findall('dot'))!=1 or len(links)!=1:continue
                    blob=nodes.get(links[0].get('source'))
                    if blob is None or blob.tag!='augmentation-dot':continue
                    dx,dy,dw,dh=coordinate(blob)
                    _,ry,_,rh=coordinate(nodes[rows[1]['head']])
                    # A tall mark above the rest is not its augmentation dot.
                    if dh<2*dw or dy+dh>ry+1 or dh<rh*.45:continue
                    notes[1].remove(dot); notes[1].find('duration').text=str(div//2)
                elif dot is not None:continue
                backups[0].find('duration').text=str(div)
                extra=ET.Element('forward'); ET.SubElement(extra,'duration').text=str(div//2)
                ET.SubElement(extra,'voice').text='2'
                m.insert(list(m).index(notes[3]),extra)
                backups[1].find('duration').text=str(2*div)
                forwards[0].find('duration').text=str(div)
                result.append(dict(part=k[3],measure=bar,phrase=word,recovered_quarter_onset='1',false_dot_removed=dotted,
                                   source_staff=k[4],peer_staff=[pk[4] for pk,_ in peers]))
    if not result:return root,dict(changes=[],state='no_verified_split_clock')
    final,_=_score_events(changed)
    touched={(c['part'],c['measure']):c for c in result}
    for key,v in final.items():
        old=events[key]
        if key[:2] in touched and key[2] in (3,4):
            assert v['start']==1 and v['duration']==1
        elif key[:2] in touched and key[2]==1:
            assert v['start']==old['start'] and v['duration']==F(1,2)
        else:
            assert (v['start'],v['duration'])==(old['start'],old['duration'])
    return changed,dict(changes=result,state='restored',source_book_sha256=expected)

