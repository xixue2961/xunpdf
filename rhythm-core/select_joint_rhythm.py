"""Select a joint flag and triplet proposal for an overfull grand-staff stack."""
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as E
import hashlib
import json
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from scan_paired_rests import box, overlaps


def strong_flag(finding):
    matches=[v for v in finding['matches'] if v['reference']['shape']=='FLAG_1' and v['quality']>=.975]
    if not matches or matches[0]['quality']<.999:
        return None
    first=matches[0]
    x,y,w,h=first['bounds']
    matches=[m for m in matches if abs(m['bounds'][0]-x)<=1 and abs(m['bounds'][1]-y)<=2]
    physical={(m['reference']['unit'],m['reference']['system'],m['reference']['staff']) for m in matches}
    if len(physical)<3 or len({v[0] for v in physical})<2:
        return None
    return first


def strong_triplet(finding):
    if len(finding['matches'])!=1 or finding['existing_tuplets'] or len(set(finding['chords']))!=3:
        return None
    match=finding['matches'][0]
    refs=[r['reference'] for r in match['references'] if r['quality']>=.999]
    identities={(ref['unit'],ref['system'],ref['staff'],ref['group'],ref['word']['id']) for ref in refs
                if (ref['unit'],ref['system'],ref['staff'])!=(finding['unit'],finding['system'],finding['staff'])}
    if len(identities)<2:
        return None
    return match


def select(book,source,flags,triplets):
    by_flag={(f['unit'],f['system'],f['chord']):(f,m) for f in flags['findings'] if (m:=strong_flag(f))}
    by_triplet=defaultdict(list)
    for f in triplets['findings']:
        match=strong_triplet(f)
        if match:
            by_triplet[f['unit'],f['system']].append((f,match))
    chosen=[]
    with ZipFile(book) as z:
        for page in z.namelist():
            found=re.fullmatch(r'sheet#([1-9][0-9]*)/sheet#\1\.xml',page)
            if not found:continue
            u=int(found[1]);file=source/f'unit-{u:03d}.png';digest=hashlib.sha256(file.read_bytes()).hexdigest()
            if any(doc['source_images'].get(str(u))!=digest for doc in (flags,triplets)):
                raise ValueError('original page changed')
            for sy in E.fromstring(z.read(page)).findall('page/system'):
                system=sy.get('id');nodes={n.get('id'):n for n in sy.findall('sig/inters/*')}
                inside=defaultdict(list)
                for edge in sy.findall('sig/relations/*'):
                    if edge[0].tag=='containment':
                        inside[edge.get('source')].append(edge.get('target'))
                stacks={m.get('id'):m for m in sy.findall('stack')}
                for part in sy.findall('part'):
                    staff=[x.get('id') for x in part.findall('staff')]
                    if len(staff)!=2:continue
                    upper,lower=staff
                    for bar in part.findall('measure'):
                        stack=stacks.get(bar.get('id'))
                        if stack is None or not stack.get('excess') or not stack.get('expected'):continue
                        chords=bar.findtext('head-chords','').split()
                        trip_options=[(f,m) for f,m in by_triplet[u,system] if f['staff']==upper and set(f['chords'])<=set(chords)]
                        if len(trip_options)!=1:continue
                        if sum(nodes[r].get('shape')=='EIGHTH_REST' and nodes[r].get('staff')==lower
                               for c in bar.findtext('rest-chords','').split() for r in inside[c])<2:continue
                        options=[by_flag[u,system,c] for c in chords if (u,system,c) in by_flag]
                        if {f['staff'] for f,_ in options}!={upper,lower} or len(options)!=2:continue
                        suspect=[box(nodes[h]) for c in chords if nodes[c].get('staff')==lower for h in inside[c]
                                 if nodes[h].get('shape')=='WHOLE_NOTE' and float(nodes[h].get('grade','1'))<=.55]
                        if sum(overlaps(v,m['bounds']) for f,m in options if f['staff']==lower for v in suspect)!=1:continue
                        trip,m=trip_options[0]
                        rows=[['FLAG',u,system,f['chord'],f['stem'],match['reference']['shape'],*match['bounds'],
                               ','.join(sorted(h['id'] for h in f['heads'])),digest] for f,match in options]
                        chosen.append(dict(unit=u,system=system,measure=bar.get('id'),staff=staff,expected_whole_notes=stack.get('expected'),
                                           flags=rows,triplet=[u,system,trip['group'],','.join(trip['chords']),*m['bounds'],digest]))
    return chosen


if __name__=='__main__':
    book,source,flag_path,triplet_path,out=map(Path,sys.argv[1:])
    found=select(book,source,*[json.loads(p.read_text(encoding='utf8')) for p in (flag_path,triplet_path)])
    with out.open('x',encoding='utf8') as f:json.dump(found,f,ensure_ascii=False,indent=2)
    print(found,flush=True)
