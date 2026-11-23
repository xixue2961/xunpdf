"""Restore repeated printed V breath marks with physical staff ownership."""
from collections import defaultdict
from copy import deepcopy
from hashlib import file_digest
from pathlib import Path
import csv,json
from xml.etree import ElementTree as E
from zipfile import ZipFile
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import canonical,score,sha
from source_vocal_split_clock import coordinate,pitch


def component(image, mark, threshold):
    x,y,w,h=coordinate(mark); cx=x+w//2
    l,t,r,b=cx-22,y-3,cx+23,y+h+4
    crop=image.crop((l,t,r,b)); pix=crop.load()
    ink={(i,j) for j in range(crop.height) for i in range(crop.width) if pix[i,j]<threshold}
    match=[]
    while ink:
        stack=[ink.pop()]; blob=[]
        while stack:
            u,v=stack.pop(); blob.append((u,v))
            n={(u+du,v+dv) for du in (-1,0,1) for dv in (-1,0,1)}&ink
            stack.extend(n);ink.difference_update(n)
        if len(blob)<30:continue
        xx,yy=zip(*blob); lo,hi,top,bot=min(xx),max(xx),min(yy),max(yy)
        width,height=hi-lo+1,bot-top+1
        if not 12<=width<=24 or not 13<=height<=27 or not 2<=lo or hi>=crop.width-2:continue
        if not lo-3 <= cx-l <= hi+3 or not .15<=len(blob)/(width*height)<=.40:continue
        band=lambda row,n:[z for z,v in blob if row<=v<row+n]
        up=band(top,max(2,height//4)); down=band(bot-max(2,height//4)+1,max(2,height//4))
        mid=(hi+lo)/2
        if not any(z<lo+width*.25 for z in up) or not any(z>hi-width*.25 for z in up):continue
        if any(abs(z-mid)>width*.24 for z in down):continue
        match.append((l+lo,t+top,l+hi,t+bot))
    return match[0] if len(match)==1 else None


def rows(path):
    with path.open(encoding='utf8',newline='') as f:
        return [r for r in csv.DictReader(f,delimiter='\t') if r['repeat_copy']=='false' and r['implicit']=='false']


def recover(root,job):
    job=Path(job)
    if not any((n.text or '')=='V' for n in root.findall('part/measure/note/lyric/text')):
        return root,dict(state='no_candidate',changes=[])
    info=json.loads((job/'source-native-notation.json').read_text('utf8'))
    book,ctl=(Path(info[k]) for k in ('accepted_native_book','accepted_native_xml'))
    digest=info.get('accepted_native_sha256') or (info['source_book_sha256'] if str(book)==info['source_book'] else None)
    if not digest or sha(book)!=digest:raise ValueError('breath source book hash mismatch')
    base=rows(ctl.with_name('note-ledger.tsv')); raw,_=_score_events(score(ctl))
    fixed=deepcopy(root); events,_=_score_events(fixed)
    fallback=job/'source-vocal-boundary/rest-apply'
    authorized=set()
    if (fallback/'result.mxl').is_file():
        rpt=json.loads((job/'source-vocal-boundary.json').read_text('utf8'))
        authorized={(v['part'],v['measure']) for v in rpt['changes'] if 'restored_physical_ids' in v}
        alt=rows(fallback/'note-ledger.tsv')
        other,_=_score_events(score(fallback/'result.mxl'))
    led=defaultdict(list)
    for r in base:led[(r['unit'],r['system'],r['staff'])].append(r)
    out=[]
    with ZipFile(book) as z:
        for name in z.namelist():
            if not name.endswith('.xml') or not name.startswith('sheet#'):continue
            unit=name.split('/')[0].split('#')[1]
            sheet=E.fromstring(z.read(name))
            for sys,sy in enumerate(sheet.findall('.//system'),1):
                marks=[v for v in sy.findall('sig/inters/*') if v.tag in ('lyric-item','word') and v.get('value')=='V']
                if len(marks)<4:continue
                staffs=sy.findall('.//staff')
                for first in [m for m in marks if m.tag=='word']:
                    sid=int(first.get('staff')); left=coordinate(first)[0]
                    seq=[first]
                    for s in range(sid,sid+3):
                        group=[m for m in marks if m.tag=='lyric-item' and int(m.get('staff'))==s and abs(coordinate(m)[0]-left)<=19]
                        if len(group)!=1:break
                        seq+=group
                    if len(seq)!=4:continue
                    # The words are physically above a staff, not in its lyric band.
                    file=job/f'unit-{int(unit):03d}.png'
                    with Image.open(file) as source:
                        img=source.convert('L')
                    boxes=[]
                    for staff_id,node in zip(range(sid,sid+4),seq):
                        st=next(st for st in staffs if int(st.get('id'))==staff_id)
                        top=float(st.find('lines/line/point').get('y'))
                        c1,c2=(component(img,node,t) for t in (100,160))
                        if c1 is None or c2 is None or max(abs(a-b) for a,b in zip(c1,c2))>3 or not 8<=top-c1[3]<=42:break
                        boxes.append(c1)
                    if len(boxes)!=4 or max(b[0] for b in boxes)-min(b[0] for b in boxes)>15:continue
                    related=[]; glyph_ids=[]
                    for node in seq[1:]:
                        match=[g.get('source') for g in sy.findall('sig/relations/relation') if g.get('target')==node.get('id') and g.find('chord-syllable') is not None]
                        targets=[r for r in led[(unit,str(sys),node.get('staff'))] if r['chord'] in match]
                        if len(targets)!=1:break
                        r=targets[0]; related.append(r['measure']);glyph_ids.append(r)
                    if len(related)!=3 or len(set(related))!=1:continue
                    bar=related[0]; changes=[]
                    for k,(staff_id,bb) in enumerate(zip(range(sid,sid+4),boxes)):
                        bracket=[r for r in led[(unit,str(sys),str(staff_id))] if r['measure']==bar]
                        part_set={r['part'] for r in bracket}
                        if len(part_set)!=1:break
                        pid,=part_set; control=raw
                        if (pid,bar) in authorized:
                            bracket=[r for r in alt if (r['unit'],r['system'],r['staff'],r['part'],r['measure'])==(unit,str(sys),str(staff_id),pid,bar)]
                            control=other
                        before=[]; rests=[]; after=[]
                        for r in bracket:
                            key=(pid,bar,int(r['note_index']))
                            n=events.get(key); c=control.get(key)
                            if not n or not c or pitch(n['node'])[:2]!=pitch(c['node'])[:2]:continue
                            x=int(r['x'])
                            if (n['node'].find('pitch') is not None and x+int(r['w'])<bb[0]
                                    and n['start']==0 and n['duration']==.5):before.append((r,n))
                            elif n['node'].find('rest') is not None and x+int(r['w'])<bb[0] and n['start']==.5 and n['duration']==.5:rests.append(n)
                            elif n['node'].find('pitch') is not None and x>=bb[2]-4 and abs(x-bb[2])<=23 and n['start']==1:after.append(n)
                        if len(rests)!=1 or not before or not after:break
                        host=min(before,key=lambda r:int(r[0]['y']))[1]['node']
                        if host.find('notations/articulations/breath-mark') is not None:break
                        junk=None
                        if k<3:
                            v=events.get((pid,bar,int(glyph_ids[k]['note_index'])))
                            if not v or v not in after:break
                            texts=[ly for ly in v['node'].findall('lyric') if ly.findtext('text')=='V']
                            if len(texts)!=1 or len(texts[0].findall('text'))!=1 or not any(any('\u3400'<=c<='\u9fff' for c in t.text or '') for t in v['node'].findall('lyric/text')):break
                            junk=(v['node'],texts[0])
                        m=events[(pid,bar,0)]['measure']; old=None
                        if k==0:
                            ds=[d for d in m.findall('direction') if d.findtext('direction-type/words')=='V' and d.get('placement')=='above' and len(d)==1 and len(d[0])==1]
                            if len(ds)!=1:break
                            old=(m,ds[0])
                        changes.append((pid,host,junk,old,bb))
                    if len(changes)!=4:continue
                    for pid,host,junk,old,bb in changes:
                        nots=host.find('notations')
                        if nots is None:
                            nots=E.Element('notations')
                            host.insert(next((i for i,n in enumerate(host) if n.tag in ('lyric','play','listen')),len(host)),nots)
                        art=nots.find('articulations')
                        if art is None:art=E.SubElement(nots,'articulations')
                        E.SubElement(art,'breath-mark',placement='above').text='upbow'
                        for pair in (junk,old):
                            if pair:pair[0].remove(pair[1])
                        out.append(dict(part=pid,measure=bar,pixel_box=bb,removed_lyric=junk is not None,removed_words=old is not None))
    return (fixed if out else root),dict(state='restored' if out else 'no_verified_breath_column',changes=out)

