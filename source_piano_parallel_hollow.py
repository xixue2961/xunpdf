"""Recover a missing repeated half-note piano chord from actual original ink."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction as F
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile
import json
from PIL import Image
from source_native_notation import score,sha
from source_lyric_roles import _score_events
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate,pitch,native_anchor
from source_piano_horizontal_accent import accent,sim


def ink(im,x,y,w,h,threshold):
    crop=im.crop((x,y,x+w,y+h))
    return {(i%w,i//w) for i,v in enumerate(crop.getdata()) if v<threshold}


def earlier_accidental(img,naturals,x,col2,notes):
    refs=[coordinate(n) for n in naturals if float(n.get('ctx-grade','0'))>=.82]
    refs=[(a,b,w,h) for a,b,w,h in refs if 11<=w<=14 and 35<=h<=40]
    found=[]
    near_top=min(int(n['y']) for n in notes)
    for g in (105,155):
        ranked=[]
        for a,b,w,h in refs:
            model=ink(img,a,b,w,h,g)
            if not model:continue
            value=max((sim(model,mask),ix,iy,w,h) for ix in range(x-28,x-5) for iy in range(near_top-18,near_top+40)
                       if (mask:=ink(img,ix,iy,w,h,g)))
            if value[0]>=.81:ranked.append(value)
        if len(ranked)<2:return
        best=max(ranked)
        if best[0]<.94 or sum(abs(t[1]-best[1])<=1 and abs(t[2]-best[2])<=2 for t in ranked)<2:return
        found.append(best)
        _,a,b,w,h=best
        # No repeated explicit accidental beside the second chord.
        ref=ink(img,a,b,w,h,g)
        dx=col2-x
        if any(sim(ref,ink(img,a+dx+dr,b+dy,w,h,g))>=.50 for dr in (-2,-1,0,1,2) for dy in (-2,-1,0,1,2)):return
    if max(abs(a-b) for a,b in zip(found[0][1:3],found[1][1:3]))>1:return
    _,a,b,w,h=found[0]
    at=b+(h-1)/2
    distances=sorted((abs(at-int(n['y'])-(int(n['h'])-1)/2),i) for i,n in enumerate(notes))
    if distances[0][0]>2.5 or distances[1][0]-distances[0][0]<4:return
    return distances[0][1],(a,b,w,h),[f[0] for f in found]


def recover(root,job):
    if not any(n.text=='V' for n in root.findall('part/measure/note/lyric/text')):
        return root,dict(state='no_candidate',changes=[])
    job=Path(job); info=json.loads((job/'source-native-notation.json').read_text('utf8'))
    book,ctl=(Path(info[k]) for k in ('accepted_native_book','accepted_native_xml'))
    expected=info.get('accepted_native_sha256') or (info['source_book_sha256'] if str(book)==info['source_book'] else None)
    if not expected or sha(book)!=expected:raise ValueError('hollow chord source hash mismatch')
    native,_=_score_events(score(ctl)); fixed=deepcopy(root); events,_=_score_events(fixed)
    by_system=defaultdict(list)
    for row in rows(ctl.with_name('note-ledger.tsv')):by_system[(row['unit'],row['system'])].append(row)
    done=[]
    with ZipFile(book) as z:
        for name in z.namelist():
            if not name.startswith('sheet#') or not name.endswith('.xml'):continue
            u=name.split('/')[0].split('#')[1]
            systems=E.fromstring(z.read(name)).findall('.//system')
            naturals=[n for sy in systems for n in sy.findall('sig/inters/alter') if n.get('shape')=='NATURAL']
            if len(naturals)<2:continue
            for s,sy in enumerate(systems,1):
                rs=by_system[u,str(s)]
                objects=sy.findall('sig/inters/*'); nodes={n.get('id'):n for n in objects if n.find('bounds') is not None}
                rels=sy.findall('sig/relations/relation')
                for line in [n for n in objects if n.tag=='lyric-line']:
                    linked=[r.get('target') for r in rels if r.get('source')==line.get('id') and r.find('containment') is not None]
                    words=[nodes[t] for t in linked if t in nodes]
                    if len(words)!=2 or any(n.tag!='lyric-item' or n.get('value')!='V' for n in words):continue
                    words.sort(key=lambda n:coordinate(n)[0])
                    pairs=[]
                    for w in words:
                        src=[r.get('source') for r in rels if r.get('target')==w.get('id') and r.find('chord-syllable') is not None]
                        rr=[r for r in rs if r['chord'] in src and r['staff']==w.get('staff')]
                        if len(rr)!=1:break
                        pairs.append(rr[0])
                    if len(pairs)!=2 or len({(r['part'],r['measure'],r['staff']) for r in pairs})!=1:continue
                    bar=pairs[0]['measure']; lane=int(pairs[0]['staff'])+1
                    pinned=[r for r in rs if r['measure']==bar and r['staff']==str(lane) and r['staff_number']=='1']
                    if len(pinned)!=4 or len({r['chord'] for r in pinned})!=1:continue
                    pid=pinned[0]['part']; lower=[r for r in rs if r['measure']==bar and r['part']==pid and r['staff_number']=='2']
                    if len(lower)!=4 or len({r['chord'] for r in lower})!=2:continue
                    pinned.sort(key=lambda r:int(r['note_index']))
                    ev=lambda r:events.get((r['part'],r['measure'],int(r['note_index'])))
                    allrows=pinned+lower+pairs
                    if any(ev(r) is None or not native_anchor(r,nodes) or pitch(ev(r)['node'])!=pitch(native[(r['part'],r['measure'],int(r['note_index']))]['node']) for r in allrows):continue
                    chords=[ev(r) for r in pinned]; notes=[v['node'] for v in chords]
                    if [n.find('chord') is not None for n in notes]!=[False,True,True,True]:continue
                    if any(v['start']!=2 or v['duration']!=2 or v['node'].findtext('type')!='half' or v['node'].find('tie') is not None for v in chords):continue
                    if sorted(v['start'] for v in map(ev,lower))!=[0,0,2,2] or any(ev(r)['duration']!=2 for r in lower+pairs):continue
                    x1=min(int(r['x']) for r in lower);x2=max(int(r['x']) for r in lower)
                    q=x2-x1
                    if not 46<=q<=160 or any(abs(int(r['x'])-(x1+i*q))>3 or abs(coordinate(words[i])[0]-(x1+i*q))>9 for i,r in enumerate(pairs)):continue
                    if any(abs(int(r['x'])-x2)>3 or r['shape']!='NOTEHEAD_VOID' for r in pinned):continue
                    m=chords[0]['measure']; f=m.findall('forward')
                    if len(f)!=1 or list(m).index(f[0])+1!=list(m).index(notes[0]) or f[0].findtext('duration')!=notes[0].findtext('duration') or f[0].findtext('staff')!='1' or f[0].findtext('voice')!=notes[0].findtext('voice'):continue
                    if len(m.findall('backup'))!=1 or int(m.findtext('backup/duration'))!=2*int(notes[0].findtext('duration')):continue
                    junk=[]
                    for row in pairs:
                        n=ev(row)['node']; hits=[t for t in n.findall('lyric') if t.findtext('text')=='V']
                        if len(hits)!=1 or len(hits[0].findall('text'))!=1 or not any(any('\u3400'<=c<='\u9fff' for c in g.text or '') for g in n.findall('lyric/text')):break
                        junk.append((n,hits[0]))
                    if len(junk)!=2:continue
                    with Image.open(job/f'unit-{int(u):03d}.png') as file:img=file.convert('L')
                    metrics=[]
                    for g in (105,155):
                        opt=[]
                        for dx in (-1,0,1):
                            for dy in (-1,0,1):
                                fit=[]
                                for r in pinned:
                                    x,y,w,h=(int(r[a]) for a in ('x','y','w','h'))
                                    w-=3;x+=2
                                    template=ink(img,x,y,w,h,g); candidate=ink(img,x-q+dx,y+dy,w,h,g)
                                    fit.append(sim(template,candidate))
                                opt.append((min(fit),sum(fit),dx,dy,fit))
                        metrics.append(max(opt))
                    if min(v[0] for v in metrics)<.85 or metrics[0][2:4]!=metrics[1][2:4]:continue
                    a=earlier_accidental(img,naturals,x1,x2,pinned)
                    if a is None:continue
                    ai,box,certainty=a
                    if notes[ai].findtext('pitch/alter','0') not in ('-1','1') or any(n.find('accidental') is not None for n in notes):continue
                    marks=[[accent(img,coordinate(w)[0]+1,coordinate(w)[1]+3,g) for w in words] for g in (130,165)]
                    if any(not a for group in marks for a in group) or min(sim(*[a[1] for a in group]) for group in marks)<.70:continue
                    if any(max(abs(x-y) for x,y in zip(a[0],b[0]))>2 for a,b in zip(*marks)):continue
                    st=notes[0].find('notations/articulations')
                    if st is None or [n.tag for n in st]!=['staccato']:continue
                    related=[nodes.get(r.get('target')) for r in rels if r.get('source')==pinned[0]['chord'] and r.find('chord-articulation') is not None]
                    false=[n for n in related if n is not None and n.get('shape')=='STACCATO']
                    if len(false)!=1:continue
                    l,t,r,b=marks[0][1][0]; fx,fy,fw,fh=coordinate(false[0])
                    if not (l<=fx<=r and t<=fy<=b and fx+fw<=r+4 and fy+fh<=b+4):continue
                    st[0].tag='accent';st[0].set('placement','above')
                    notes[ai].find('pitch').remove(notes[ai].find('pitch/alter'))
                    new=deepcopy(notes)
                    follow=next(r for r in lower if ev(r)['start']==0)
                    fx=ev(follow)['node'].get('default-x')
                    if fx is not None:
                        for n in new:n.set('default-x',fx)
                    n=new[ai];n.insert(list(n).index(n.find('stem')),E.Element('accidental'));n.find('accidental').text='natural'
                    place=list(m).index(f[0]);m.remove(f[0])
                    for i,node in enumerate(new):m.insert(place+i,node)
                    for node,bad in junk:node.remove(bad)
                    done.append(dict(part=pid,measure=bar,head_overlap=[v[4] for v in metrics],
                                     restored_heads=len(new),accidental_source=box,accidental_match=certainty,
                                     restored_pitch=pitch(n),removed_lyric_count=len(junk)))
    return (fixed if done else root),dict(state='restored' if done else 'no_verified_parallel_hollow',changes=done)
