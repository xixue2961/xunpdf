"""Remove a printed triplet numeral duplicated as text on the same physical glyph."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction as F
import json
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile
from source_native_notation import score,sha
from source_lyric_roles import _score_events
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate,pitch,native_anchor


def is_digit(d):
    dt=d.find('direction-type')
    return (dt is not None and len(dt)==1 and dt.findtext('words')=='3' and d.find('sound') is None
            and {x.tag for x in d} <= {'direction-type','offset','staff'})


def recover(root,job):
    if not any(is_digit(d) for d in root.findall('part/measure/direction')):
        return root,dict(state='no_candidate',changes=[])
    job=Path(job); cfg=json.loads((job/'source-native-notation.json').read_text('utf8'))
    book,ctl=(Path(cfg[k]) for k in ('accepted_native_book','accepted_native_xml'))
    digest=cfg.get('accepted_native_sha256') or (cfg['source_book_sha256'] if cfg['source_book']==str(book) else None)
    if not digest or sha(book)!=digest:raise ValueError('tuplet source hash mismatch')
    fixed=deepcopy(root); events,dirs=_score_events(fixed); native,_=_score_events(score(ctl)); changes=[]
    led=defaultdict(list)
    for r in rows(ctl.with_name('note-ledger.tsv')):led[r['unit'],r['system'],r['chord']].append(r)
    with ZipFile(book) as z:
        for f in z.namelist():
            if not f.startswith('sheet#') or not f.endswith('.xml'):continue
            unit=f.split('/')[0].split('#')[1]
            for sys,sy in enumerate(E.fromstring(z.read(f)).findall('.//system'),1):
                ns=sy.findall('sig/inters/*'); nodes={n.get('id'):n for n in ns if n.find('bounds') is not None}
                rels=sy.findall('sig/relations/relation')
                for group in [n for n in ns if n.tag=='tuplet' and n.get('shape')=='TUPLET_THREE']:
                    x,y,w,h=coordinate(group); staff=group.get('staff')
                    words=[]
                    for n in ns:
                        if n.tag!='word' or n.get('staff')!=staff or n.get('value')!='3':continue
                        a,b,j,k=coordinate(n)
                        # OCR often boxes only the center strip of the actual digit.
                        if j>w or not x<=a<a+j<=x+w or not b<=y<y+h<=b+k or abs((y+h/2)-(b+k/2))>2:continue
                        owner=[nodes.get(r.get('source')) for r in rels if r.get('target')==n.get('id') and r.find('containment') is not None]
                        if len(owner)!=1 or owner[0] is None or owner[0].tag!='sentence':continue
                        if sum(r.get('source')==owner[0].get('id') and r.find('containment') is not None for r in rels)==1:words.append(n)
                    if len(words)!=1:continue
                    attached=[r.get('source') for r in rels if r.get('target')==group.get('id') and r.find('chord-tuplet') is not None]
                    if len(set(attached))!=3 or len(attached)!=3:continue
                    anchors=[]; keys=set()
                    for chord in attached:
                        rr=led[unit,str(sys),chord]
                        if not rr or any(r['staff']!=staff or not native_anchor(r,nodes) for r in rr):break
                        lead=min(rr,key=lambda r:int(r['note_index']))
                        key=(lead['part'],lead['measure'],int(lead['note_index']))
                        event=events.get(key); ref=native.get(key)
                        if not event or not ref or pitch(event['node'])!=pitch(ref['node']) or event['node'].find('chord') is not None:break
                        if event['duration']!=ref['duration'] or event['start']!=ref['start']:break
                        keys.add((key[0],key[1],lead['staff_number']))
                        anchors.append(event)
                    if len(anchors)!=3 or len(keys)!=1:continue
                    anchors.sort(key=lambda a:a['start'])
                    nd=[e['node'] for e in anchors]; onset=[e['start'] for e in anchors]
                    if any(n.findtext('time-modification/actual-notes')!='3' or n.findtext('time-modification/normal-notes')!='2' for n in nd):continue
                    if any(a['duration']!=b-a['start'] for a,b in zip(anchors,onset[1:])):continue
                    first=nd[0].findall('notations/tuplet'); last=nd[-1].findall('notations/tuplet')
                    if len(first)!=1 or len(last)!=1 or first[0].get('type')!='start' or last[0].get('type')!='stop' or first[0].get('number','1')!=last[0].get('number','1'):continue
                    if first[0].get('show-number')=='none':continue
                    pid,bar,st=next(iter(keys)); m=anchors[0]['measure']
                    duplicate=[d for d in dirs if (d['part'],d['measure'],d['staff'],d['start'])==(pid,bar,st,onset[1])
                               and is_digit(d['node']) and d['node'].get('placement')==first[0].get('placement') and d['node'] in m]
                    if len(duplicate)!=1:continue
                    m.remove(duplicate[0]['node'])
                    changes.append(dict(part=pid,measure=bar,staff=st,source_tuplet=group.get('id'),source_word=words[0].get('id'),beat=str(onset[1])))
    return (fixed if changes else root),dict(state='restored' if changes else 'no_proven_duplicate',changes=changes)
