"""Source-pixel classification of dynamics misassigned to vocal lyric lines.
No fixed score, measure, part name or glyph IDs. Produces conservative native repair proposals.
"""
from pathlib import Path
import csv,hashlib,json,re,zipfile,xml.etree.ElementTree as E
from collections import defaultdict
TOKENS={'p','pp','ppp','f','ff','fff','mp','mf','sf','sfz','fp','fz','rfz'}
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def box(e):return [int(e.find('bounds').get(k)) for k in ('x','y','w','h')]
def han(s):return sum('\u3400'<=c<='\u9fff' for c in s)
def y_at(line,x):
 pts=sorted((float(v.get('x')),float(v.get('y'))) for v in line.findall('point'))
 return float(np.interp(x,[p[0] for p in pts],[p[1] for p in pts]))
def context_reason(c):
 if c['target_staff'] is None:return 'no_following_staff_in_same_system'
 if c['han_characters']<4:return 'no_separate_source_chinese_lyric_context'
 if not c['line_tokens'] or any(t not in TOKENS for t in c['line_tokens']):return 'mixed_or_real_lyric_line_preserved'
 if c['line_owners']!=1:return 'ambiguous_lyric_line_ownership'
 if c['chord_owners']!=1:return 'ambiguous_original_lyric_chord'
 return None

def _peer_dynamic(source, staff, peer, rows, chinese, nodes, relations, image, verify_row):
 """Find a second printed mark at the same musical column on an adjacent staff."""
 lines=staff.findall('lines/line');peer_lines=peer.findall('lines/line')
 if len(lines)!=5 or len(peer_lines)!=5:return None
 x,y,w,h=source['full_bounds'];cx=x+w/2;space=source['interline']
 if abs((y_at(peer_lines[-1],cx)-y_at(peer_lines[0],cx))/4-space)>.05*space:return None
 top=y_at(peer_lines[0],cx);drop=top-y_at(lines[0],cx)
 column=float(source['target_note']['x'])+float(source['target_note']['w'])/2
 anchors={}
 for row in rows:
  if row['measure']!=source['target_note']['measure'] or not row['shape'].startswith('NOTEHEAD_'):continue
  if abs(float(row['x'])+float(row['w'])/2-column)>.25*space:continue
  verify_row(row)
  key=row['chord']
  if key not in anchors or int(row['note_index'])<int(anchors[key]['note_index']):anchors[key]=row
 if len(anchors)!=1:return None
 target=next(iter(anchors.values()))
 if target['part']==source['target_note']['part']:return None
 if sum(han(n.get('value','')) for n in chinese)<4:return None
 near=[n for n in chinese if abs(box(n)[0]+box(n)[2]/2-column)<space]
 if len(near)!=1:return None
 child=near[0]
 if sum(v.get('source')==target['chord'] and v.get('target')==child.get('id') and v.find('chord-syllable') is not None for v in relations)!=1:return None
 baseline=float(np.median([box(n)[1]+box(n)[3] for n in chinese]))
 if abs(box(child)[1]+box(child)[3]-baseline)>.4*space:return None
 pad=max(2,round(.30*space));px=x-pad;py=round(y+drop)-pad
 roi=image[py:py+h+2*pad,px:px+w+2*pad]
 if roi.shape!=(h+2*pad,w+2*pad):return None
 proof=[]
 for threshold in (160,208):
  template=(image[y:y+h,x:x+w]<threshold).astype(np.uint8)
  search=(roi<threshold).astype(np.uint8)
  scores=cv2.matchTemplate(search,template,cv2.TM_CCOEFF_NORMED)
  _,score,_,(ix,iy)=cv2.minMaxLoc(scores)
  found=search[iy:iy+h,ix:ix+w]
  shared=np.logical_and(found,template).sum()
  precision=float(shared/max(1,found.sum()));recall=float(shared/max(1,template.sum()))
  if score<.95 or min(precision,recall)<.98:return None
  proof.append(dict(threshold=threshold,score=score,precision=precision,recall=recall,bounds=[px+ix,py+iy,w,h]))
 if proof[0]['bounds']!=proof[1]['bounds']:return None
 fx,fy,fw,fh=proof[0]['bounds']
 if fy<=y_at(peer_lines[-1],cx) or fy+fh>=baseline-.5*space:return None
 for n in nodes.values():
  if n.tag!='dynamics':continue
  a,b,c,d=box(n)
  if a<fx+fw and fx<a+c and b<fy+fh and fy<b+d:return None
 return dict(unit=source['unit'],system=source['system'],item='peer:'+source['item']+':'+peer.get('id'),token=source['token'],source_note=source['target_note'],target_note=target,source_staff=staff.get('id'),target_staff=peer.get('id'),full_bounds=proof[0]['bounds'],interline=space,target_staff_top=top,source_sha256=source['source_sha256'],placement='below',state='source_proven_peer_dynamic_ink',peer_ink_proof=proof,source_role_item=source['item'])


def _same_column_dynamic_peer(candidate, peers):
    unit, system = candidate['unit'], candidate['system']
    left, _, width, _ = candidate['full_bounds']
    note = candidate['target_note']
    tolerance = candidate['interline'] * .25
    for other in peers:
        target = other['target_note']
        if other is candidate or (other['unit'], other['system']) != (unit, system):
            continue
        if other['token'] != candidate['token'] or target['measure'] != note['measure']:
            continue
        if abs(other['staff_index'] - candidate['staff_index']) != 1:
            continue
        if target['part'] == note['part'] or abs(float(target['x']) - float(note['x'])) > tolerance:
            continue
        peer_left, _, peer_width, _ = other['full_bounds']
        if abs(left + width / 2 - peer_left - peer_width / 2) <= tolerance:
            return other
    return None


def _repeated_p_below_staff(candidate, row, lines, chinese, images, references):
    '''Find two printed piano glyphs on one baseline between the staff and lyric.'''
    x, y, width, height = candidate['item_bounds']
    column = float(row['x']) + float(row['w']) / 2
    if len(lines) != 5 or not row['shape'].startswith('NOTEHEAD_'):
        return None
    top = y_at(lines[0], column)
    bottom = y_at(lines[-1], column)
    space = (bottom - top) / 4
    local_words = [box(word) for word in chinese if box(word)[0] - space <= column <= box(word)[0] + box(word)[2] + space]
    if not local_words:
        return None
    left, upper = max(0, round(x - 2*space)), max(0, round(y - .6*space))
    raster = images[candidate['unit']]
    crop = raster[upper:round(y+height+space), left:round(x+width+2*space)]
    matches = []
    for ref in references:
        if ref['token'] != 'p':
            continue
        rx, ry, w, h = ref['bounds']
        if not (.8*space <= w <= 3*space and .8*space <= h <= 3*space):
            continue
        source = images[ref['unit']][ry:ry+h, rx:rx+w]
        for advance in range(round(.72*w), round(.84*w)+1):
            pair_width = advance+w
            if not advance or h > crop.shape[0] or pair_width > crop.shape[1]:
                continue
            dual = []
            for t in (160, 208):
                single = source < t
                if not single.any():
                    break
                pair = np.zeros((h,pair_width),dtype=np.uint8)
                pair[:,:w] |= single
                pair[:,advance:] |= single
                search = (crop<t).astype(np.uint8)
                _, score, _, (cx,cy) = cv2.minMaxLoc(cv2.matchTemplate(search,pair,cv2.TM_CCOEFF_NORMED))
                found = search[cy:cy+h,cx:cx+pair_width]
                intersection = np.logical_and(pair,found).sum()
                precision = intersection/max(1,found.sum())
                recall = intersection/pair.sum()
                if score < .90 or precision < .92 or recall < .975:
                    break
                dual.append(dict(threshold=t,bounds=[left+cx,upper+cy,pair_width,h],
                                 score=score,precision=float(precision),recall=float(recall)))
            if len(dual)!=2 or max(abs(a-b) for a,b in zip(dual[0]['bounds'],dual[1]['bounds']))>1:
                continue
            fx,fy,fw,fh=dual[0]['bounds']
            if not (fy >= bottom-.25*space and abs(fx+fw/2-column)<=space):
                continue
            if not any(fy+.45*fh <= ly and ly+lh >= fy+fh+.45*space for _,ly,_,lh in local_words):
                continue
            matches.append(dict(reference=ref,thresholds=dual))
    if not matches:
        return None
    best = max(matches,key=lambda m: min(q['score'] for q in m['thresholds']))
    bounds = best['thresholds'][0]['bounds']
    support = [m for m in matches if max(abs(a-b) for a,b in zip(bounds,m['thresholds'][0]['bounds']))<=2]
    if len({(m['reference']['unit'],m['reference']['system'],m['reference']['staff']) for m in support})<2:
        return None
    return dict(full_bounds=bounds,font_matches=support,interline=space,target_staff_top=top,
                target_staff=row['staff'],target_chord=row['chord'],target_note=row,
                placement='below',state='source_proven_repeated_p_dynamic')

def _above_staff_fp_pair(c, oldrow, source_staff, next_staff, refs, rows, nodes, relations, image, verify_row):
    """Recognize the same printed fp three times at one column, two above vocal staves."""
    if c['token']!='fp' or c['han_characters']!=0 or c['line_tokens']!=['fp']:
        return []
    if (c['line_owners'],c['chord_owners'])!=(1,1):
        return []
    x,y,w,h=c['item_bounds']
    old_center=float(oldrow['x'])+float(oldrow['w'])/2
    first=source_staff.findall('lines/line')
    second=next_staff.findall('lines/line')
    if len(first)!=5 or len(second)!=5:
        return []
    space=(y_at(first[-1],old_center)-y_at(first[0],old_center))/4
    if not 10<=space<=28:
        return []
    top1,top2=[y_at(lines[0],old_center) for lines in (first,second)]

    def aligned_note(staff_rows):
        notes={}
        for row in staff_rows:
            center=float(row['x'])+float(row['w'])/2
            if row['measure']!=oldrow['measure'] or not (row['shape'].startswith('NOTEHEAD_') or row['shape']=='WHOLE_NOTE') or abs(center-old_center)>.50*space:
                continue
            verify_row(row)
            notes.setdefault(row['chord'],[]).append(row)
        if len(notes)!=1:
            return None
        return min(next(iter(notes.values())),key=lambda row:int(row['note_index']))

    other=aligned_note(rows)
    if other is None or other['part']==oldrow['part']:
        return []
    donors=[]
    for ref in refs:
        if (ref['token'],ref['unit'],ref['system'])!=('fp',c['unit'],c['system']):
            continue
        rx,ry,rw,rh=ref['bounds']
        if abs(rx+rw/2-old_center)>1.25*space or ref['staff'] in (c['source_staff'],c['target_staff']):
            continue
        chord_links=[v.get('source') for v in relations if v.get('target')==ref['id']
                    and v.find('chord-dynamics') is not None]
        if len(chord_links)==1:
            anchor=aligned_note(ref['rows'])
            if anchor is not None and anchor['chord']==chord_links[0] and anchor['part'] not in (oldrow['part'],other['part']):
                donors.append(ref)
    if len(donors)!=1:
        return []
    ref=donors[0]
    rx,ry,rw,rh=ref['bounds']
    donor=image[ry:ry+rh,rx:rx+rw]

    def locate(patch, staff_top, scales, coverage):
        x0=max(0,round(old_center-3*space))
        x1=min(image.shape[1],round(old_center+2.5*space))
        y0=max(0,round(staff_top-7.4*space))
        y1=min(image.shape[0],round(staff_top-1.5*space))
        out=[]
        for threshold in (128,160,208):
            roi=(image[y0:y1,x0:x1]<threshold).astype(np.uint8)
            best=None
            for ratio in scales:
                target=(cv2.resize(patch,None,fx=ratio,fy=ratio,interpolation=cv2.INTER_NEAREST)<threshold).astype(np.uint8)
                h0,w0=target.shape
                if h0>roi.shape[0] or w0>roi.shape[1] or not target.var():
                    continue
                _,corr,_,(xx,yy)=cv2.minMaxLoc(cv2.matchTemplate(roi,target,cv2.TM_CCOEFF_NORMED))
                match=roi[yy:yy+h0,xx:xx+w0]
                shared=np.logical_and(match,target).sum()
                triple=(float(corr),float(shared/max(1,match.sum())),float(shared/target.sum()))
                entry=(triple,(x0+xx,y0+yy,w0,h0),ratio)
                if best is None or entry[0][0]>best[0][0]:
                    best=entry
            if best is None or any(a<b for a,b in zip(best[0],coverage)):
                return []
            out.append(dict(threshold=threshold,scores=list(best[0]),bounds=list(best[1]),scale=best[2]))
        if len({tuple(p['bounds']) for p in out})!=1:
            return []
        return out

    scales=(.94,.95,.96,.97,.98,.99,1.0,1.01,1.02)
    upper=locate(donor,top1,scales,(.82,.85,.90))
    lower=locate(donor,top2,scales,(.82,.85,.90))
    if not upper or not lower:
        return []
    lx,ly,lw,lh=lower[0]['bounds']
    if not (x<=lx+lw/2<=x+w and y<=ly+lh/2<=y+h):
        return []
    pair=locate(image[ly:ly+lh,lx:lx+lw],top1,(1.0,),(.98,.98,.98))
    if not pair or any(abs(a-b)>1 for a,b in zip(pair[0]['bounds'],upper[0]['bounds'])):
        return []
    upper_box=pair[0]['bounds']
    if abs(upper_box[0]-lx)>.25*space:
        return []
    original=dict(**c,source_chord=oldrow['chord'],source_note=oldrow,target_chord=other['chord'],
                  target_note=other,full_bounds=lower[0]['bounds'],interline=space,target_staff_top=top2,
                  state='source_proven_cross_staff_fp_pair',placement='above',donor_evidence=lower)
    peer=dict(unit=c['unit'],system=c['system'],item='peer:'+c['item']+':'+c['source_staff'],
              token='fp',source_note=other,target_note=oldrow,source_staff=next_staff.get('id'),
              target_staff=source_staff.get('id'),full_bounds=upper_box,interline=space,
              target_staff_top=top1,placement='above',state='source_proven_peer_dynamic_ink',
              peer_ink_proof=pair,donor_evidence=upper)
    return [original,peer]



def scan(book,source,ledger):
 global cv2,np
 import cv2,numpy as np
 book=Path(book);source=Path(source);ledger=Path(ledger);rows=list(csv.DictReader(ledger.open(encoding='utf8'),delimiter='\t'));bychord=defaultdict(list);bystaff=defaultdict(list)
 for row in rows:
  if row['repeat_copy']!='false':continue
  key=(int(row['unit']),row['system'],row['chord']);bychord[key].append(row);bystaff[(int(row['unit']),row['system'],row['staff'])].append(row)
 report=dict(version=444,source_book_sha256=sha(book),head_ledger_sha256=sha(ledger),scope='Dynamics-only lyric line below a separate CJK line, above the next physical staff, dual-threshold same-source-font agreement and unique source target chord',proposals=[],pending=[],source_images=[])
 systems=[];refs=[];images={}
 with zipfile.ZipFile(book) as z:
  for name in z.namelist():
   m=re.fullmatch(r'sheet#([1-9][0-9]*)/sheet#\1\.xml',name)
   if not m:continue
   u=int(m[1]);t=E.fromstring(z.read(name));path=source/f'unit-{u:03d}.png';digest=sha(path);images[u]=cv2.imdecode(np.fromfile(path,np.uint8),0)
   if images[u] is None:raise ValueError('source PNG unreadable')
   report['source_images'].append(dict(unit=u,sha256=digest))
   for sy in t.findall('page/system'):
    sid=sy.get('id');nodes={e.get('id'):e for e in sy.find('sig/inters')};rels=list(sy.find('sig/relations'));staffs=sy.findall('part/staff');systems.append((u,sid,nodes,rels,staffs,digest))
    for e in nodes.values():
     token=e.get('shape','').removeprefix('DYNAMICS_').lower()
     grade=float(e.get('grade','0'))
     cross_reference=(token=='mp' and .59<=grade<.65 and float(e.get('ctx-grade','0'))>=.82)
     if e.tag!='dynamics' or token not in TOKENS or (grade<.65 and not cross_reference):continue
     links=[v.get('source') for v in rels if v.get('target')==e.get('id') and v.find('chord-dynamics') is not None]
     if len(links)!=1:continue
     mapped=bychord.get((u,sid,links[0]),[])
     if not mapped:continue
     refs.append(dict(unit=u,system=sid,staff=mapped[0]['staff'],id=e.get('id'),glyph=e.get('glyph'),token=token,bounds=box(e),source_sha256=digest,
                      cross_reference=cross_reference))
 report['font_reference_count']=len(refs)
 same_column_candidates=[]
 for u,sid,nodes,rels,staffs,digest in systems:
  positions={s.get('id'):i for i,s in enumerate(staffs)}
  contains=defaultdict(list)
  for rel in rels:
   if rel.find('containment') is not None:contains[rel.get('source')].append(rel.get('target'))
  for item in nodes.values():
   fragment=item.get('value','').lower()
   if item.tag!='lyric-item' or fragment not in TOKENS | {'m'}:continue
   token='mp' if fragment=='m' else fragment
   if fragment=='m' and (box(item)[2] > .26*box(item)[3] or box(item)[2] < 3):
    continue
   staff=item.get('staff');i=positions.get(staff);nextstaff=staffs[i+1] if i is not None and i+1<len(staffs) else None
   lines=[nodes.get(v.get('source')) for v in rels if v.get('target')==item.get('id') and v.find('containment') is not None and nodes.get(v.get('source')) is not None and nodes[v.get('source')].tag=='lyric-line']
   owners=[v.get('source') for v in rels if v.get('target')==item.get('id') and v.find('chord-syllable') is not None]
   chinese=[v for v in nodes.values() if v.tag=='lyric-item' and v.get('staff')==staff and han(v.get('value',''))]
   line=lines[0] if len(lines)==1 else None
   lineitems=[nodes[j] for j in contains.get(line.get('id'),[]) if j in nodes] if line is not None else []
   c=dict(unit=u,system=sid,item=item.get('id'),item_glyph=item.get('glyph'),item_bounds=box(item),token=token,source_staff=staff,target_staff=nextstaff.get('id') if nextstaff is not None else None,line=line.get('id') if line is not None else None,line_number=line.get('number') if line is not None else None,line_owners=len(lines),chord_owners=len(owners),line_tokens=[v.get('value','').lower() for v in lineitems],han_characters=sum(han(v.get('value','')) for v in chinese),source_sha256=digest)
   if fragment=='m':
    c['source_text']=fragment
    c['line_tokens']=['mp' if t=='m' else t for t in c['line_tokens']]
   reason=context_reason(c)
   paired_p = token=='pp' and reason in (None,'mixed_or_real_lyric_line_preserved') and len(lines)==len(owners)==1
   possible_pair=reason=='no_separate_source_chinese_lyric_context' and token=='fp' and nextstaff is not None and c['line_owners']==c['chord_owners']==1
   if reason and not (paired_p or possible_pair):report['pending'].append(dict(**c,reason=reason));continue
   sourcechord=owners[0];old=bychord.get((u,sid,sourcechord),[])
   if not old:report['pending'].append(dict(**c,reason='source_lyric_owner_not_exported'));continue
   def verify_row(row):
    h=nodes.get(row['head']);ch=nodes.get(row['chord'])
    if h is None or ch is None or h.get('staff')!=row['staff'] or h.get('shape')!=row['shape'] or box(h)!=[int(row[k]) for k in ('x','y','w','h')] or row['source_sha256']!=digest:
     raise ValueError('source head ledger physical identity changed')
    if row['head'] not in contains.get(row['chord'],[]):raise ValueError('source head/chord ownership changed')
   for row in old:verify_row(row)
   oldrow=min(old,key=lambda v:int(v['note_index']));c['source_chord']=sourcechord;c['source_note']=oldrow
   st=staffs[i];lines0=st.findall('lines/line');lines1=nextstaff.findall('lines/line')
   if possible_pair:
    peer_refs=[dict(ref,rows=bystaff.get((u,sid,ref['staff']),[])) for ref in refs
               if ref['unit']==u and ref['system']==sid and ref['token']==token]
    base={key:value for key,value in c.items() if key not in ('source_chord','source_note')}
    paired=_above_staff_fp_pair(base,oldrow,st,nextstaff,peer_refs,
                                bystaff[u,sid,nextstaff.get('id')],nodes,rels,images[u],verify_row)
    if not paired:report['pending'].append(dict(**c,reason=reason));continue
    report['proposals'].extend(paired);continue
   if paired_p:
    repeated=_repeated_p_below_staff(c,oldrow,lines0,chinese,images,refs)
    if repeated is not None:
     c.update(repeated,staff_index=i)
     same_column_candidates.append(c)
     continue
   if reason:report['pending'].append(dict(**c,reason=reason));continue
   if len(lines0)!=5 or len(lines1)!=5:report['pending'].append(dict(**c,reason='non_standard_staff_context'));continue
   x,y,bw,bh=c['item_bounds'];space=float(np.median(np.diff([y_at(v,x) for v in lines1])));padx=round(3*space);pady=round(2*space);x0=max(0,x-padx);y0=max(0,y-pady);roi=images[u][y0:y+bh+pady+1,x0:x+bw+padx+1];matches=[];cross_staff_mp=[]
   for ref in refs:
    if ref['token']!=token:continue
    rx,ry,rw,rh=ref['bounds'];template=images[ref['unit']][ry:ry+rh,rx:rx+rw];dual=[]
    for threshold in (160,208):
     best=None
     for scale in ((1.,) if ref['cross_reference'] else (.97,.985,1.,1.015,1.03)):
      tpl=(cv2.resize(template,None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)<threshold).astype(np.uint8);search=(roi<threshold).astype(np.uint8);h,w0=tpl.shape
      if h>search.shape[0] or w0>search.shape[1]:continue
      scores=cv2.matchTemplate(search,tpl,cv2.TM_CCOEFF_NORMED);_,score,_,pos=cv2.minMaxLoc(scores);found=search[pos[1]:pos[1]+h,pos[0]:pos[0]+w0];inter=np.logical_and(found,tpl).sum();entry=dict(threshold=threshold,score=score,precision=float(inter/max(1,found.sum())),recall=float(inter/max(1,tpl.sum())),bounds=[x0+pos[0],y0+pos[1],w0,h],scale=scale)
      if best is None or score>best['score']:best=entry
     if best:dual.append(best)
    if len(dual)!=2 or max(abs(a-b) for a,b in zip(dual[0]['bounds'],dual[1]['bounds']))>1:continue
    evidence=dict(reference=ref,thresholds=dual)
    if not ref['cross_reference'] and all(v['score']>=.95 and min(v['precision'],v['recall'])>=.98 for v in dual):
     matches.append(evidence)
    elif (token=='mp' and dual[0]['score']>=.88 and min(dual[0]['precision'],dual[0]['recall'])>=.92
          and dual[1]['score']>=.91 and min(dual[1]['precision'],dual[1]['recall'])>=.95):
     cross_staff_mp.append(evidence)
   confirmed_cross_staff_mp=False
   if not matches and len(cross_staff_mp)>=3:
    for witness in cross_staff_mp:
     region=witness['thresholds'][0]['bounds']
     near=[e for e in cross_staff_mp if max(abs(a-b) for a,b in zip(region,e['thresholds'][0]['bounds']))<=1]
     independent={ (e['reference']['unit'],e['reference']['system'],e['reference']['staff']) for e in near}
     if len(independent)>=3 and len({u for u,_,_ in independent})>=2:
      matches=near
      confirmed_cross_staff_mp=True
      break
   if not matches:report['pending'].append(dict(**c,reason='insufficient_dual_threshold_source_font_agreement',supported_references=0));continue
   full=matches[0]['thresholds'][0]['bounds'];fx,fy,fw,fh=full
   matches=[v for v in matches if max(abs(a-b) for a,b in zip(full,v['thresholds'][0]['bounds']))<=1]
   independent=len({(v['reference']['unit'],v['reference']['system'],v['reference']['staff']) for v in matches})
   cx=fx+fw/2;nexttop=y_at(lines1[0],cx);hanbottom=float(np.median([box(v)[1]+box(v)[3] for v in chinese]));center=fy+fh/2
   hantop=float(np.median([box(v)[1] for v in chinese]));ownbottom=y_at(lines0[-1],cx);owntop=y_at(lines0[0],cx)
   if fragment=='m':
    inside=fx<=x and x+bw<=fx+fw and fy<=y+bh/2<=fy+fh
    physical_refs={(v['reference']['unit'],v['reference']['system'],v['reference']['staff']) for v in matches}
    if not inside or len(physical_refs)<3 or len({r[0] for r in physical_refs})<2:
     report['pending'].append(dict(**c,reason='incomplete_m_requires_three_complete_mp_witnesses'))
     continue
   if fragment!='m' and not confirmed_cross_staff_mp and ownbottom+.2*space<center<hantop-.5*space and abs(float(oldrow['x'])+float(oldrow['w'])/2-cx)<=3*space:
    c.update(full_bounds=full,font_matches=matches,interline=space,target_staff_top=owntop,chinese_baseline=hanbottom,target_staff=staff,target_chord=sourcechord,target_note=oldrow,target_distance=abs(float(oldrow['x'])+float(oldrow['w'])/2-cx),placement='below',state='source_proven_same_staff_dynamic_above_lyrics')
    if independent<2:
     if any(all(v['score']>=.98 for v in match['thresholds']) for match in matches):
      c['staff_index']=i
      same_column_candidates.append(c)
     else:
      report['pending'].append(dict(**c,reason='insufficient_dual_threshold_source_font_agreement',supported_references=independent))
     continue
    report['proposals'].append(c)
    for peer_index in (i-1,i+1):
     if not 0<=peer_index<len(staffs):continue
     peer=staffs[peer_index];peer_id=peer.get('id')
     peer_chinese=[n for n in nodes.values() if n.tag=='lyric-item' and n.get('staff')==peer_id and han(n.get('value',''))]
     found=_peer_dynamic(c,st,peer,bystaff.get((u,sid,peer_id),[]),peer_chinese,nodes,rels,images[u],verify_row)
     if found is not None:report['proposals'].append(found)
    continue
   if independent<2:report['pending'].append(dict(**c,reason='font_references_not_from_two_physical_staves'));continue
   if confirmed_cross_staff_mp and (independent<3 or abs((x+bw/2)-cx)>space
       or not (fx<=x+bw/2<=fx+fw)):
    report['pending'].append(dict(**c,reason='mp_source_box_does_not_support_a_unique_cross_staff_mark'));continue
   if not (center>hanbottom+.5*space and 0<=nexttop-(fy+fh)<=4*space):report['pending'].append(dict(**c,reason='source_symbol_not_between_lyric_baseline_and_next_staff'));continue
   targets={}
   for row in bystaff.get((u,sid,nextstaff.get('id')),[]):
    verify_row(row)
    if row['shape'].endswith('REST'):continue
    key=row['chord'];dist=abs(float(row['x'])+float(row['w'])/2-cx)
    if key not in targets or dist<targets[key][0]:targets[key]=(dist,row)
   ordered=sorted(targets.values(),key=lambda v:v[0])
   if not ordered or ordered[0][0]>3*space or len(ordered)>1 and ordered[1][0]-ordered[0][0]<space:report['pending'].append(dict(**c,reason='source_target_chord_not_unique'));continue
   targetchord=ordered[0][1]['chord'];new=min(bychord[u,sid,targetchord],key=lambda v:int(v['note_index']))
   if oldrow['part']==new['part']:report['pending'].append(dict(**c,reason='not_a_cross_part_ownership_conflict'));continue
   c.update(full_bounds=full,font_matches=matches,interline=space,target_staff_top=nexttop,chinese_baseline=hanbottom,target_chord=targetchord,target_note=new,target_distance=ordered[0][0],state='source_proven_cross_staff_dynamic')
   report['proposals'].append(c)
 for candidate in same_column_candidates:
  peer=_same_column_dynamic_peer(candidate,same_column_candidates)
  if peer is None:
   report['pending'].append(dict(**candidate,reason='no_same_column_peer_dynamic'))
   continue
  if candidate['state']!='source_proven_repeated_p_dynamic':
   candidate['state']='source_proven_same_staff_dynamic_with_column_peer'
  candidate['peer_role_item']=peer['item']
  report['proposals'].append(candidate)
 return report

def validate_plan(plan,book,source,ledger):
 fresh=scan(book,source,ledger)
 if fresh!=plan:raise ValueError('source dynamics/lyrics evidence changed or plan was modified')
 return fresh
def _score_events(root):
 from fractions import Fraction as F
 out={};directions=[]
 for p in root.findall('part'):
  div=F(1)
  for m in p.findall('measure'):
   cur=F();last=None;index=0
   for e in m:
    if e.tag=='attributes' and e.find('divisions') is not None:div=F(e.findtext('divisions'))
    if div<=0:raise ValueError('invalid divisions')
    if e.tag=='backup':cur-=F(e.findtext('duration'))/div
    elif e.tag=='forward':cur+=F(e.findtext('duration'))/div
    elif e.tag=='direction':directions.append(dict(part=p.get('id'),measure=m.get('number'),staff=e.findtext('staff','1'),start=cur+F(e.findtext('offset','0'))/div,node=e))
    elif e.tag=='note':
     duration=F(e.findtext('duration','0'))/div;start=last if e.find('chord') is not None else cur
     if start is None:raise ValueError('orphan chord follower')
     key=(p.get('id'),m.get('number'),index);index+=1
     out[key]=dict(node=e,start=start,duration=duration,measure=m)
     if e.find('chord') is None:last=start;cur+=duration
 return out,directions

def _music_signature(event):
 n=event['node'];p=n.find('pitch')
 return (event['start'],event['duration'],None if p is None else (p.findtext('step'),p.findtext('alter','0'),p.findtext('octave')),n.findtext('voice','1'),n.findtext('staff','1'),n.find('chord') is not None,n.find('grace') is not None,n.find('rest') is not None)

def _role_anchor_signature(event):
 # A source-backed dynamic belongs to a staff/time/voice, independent of a
 # previously corrected pitch or octave clef. Pitch is preserved, never copied.
 n=event['node']
 return (event['start'],event['duration'],n.findtext('voice','1'),n.findtext('staff','1'),n.find('chord') is not None,n.find('grace') is not None,n.find('rest') is not None)

def apply_roles(original,control,plan,book,source,ledger):
 import copy
 from source_expression_overlay import canonical
 validate_plan(plan,book,source,ledger)
 if not plan['proposals']:return original,dict(changes=[],covered=[],pending=plan['pending'],all_other_score_fields_exact=True)
 oldev,olddirs=_score_events(original);control_ev,control_dirs=_score_events(control);result=copy.deepcopy(original);ev,dirs=_score_events(result);neutral=copy.deepcopy(original);neutral_ev,_=_score_events(neutral);changes=[];covered=[];removed=[];added=[];seen=set()
 for c in plan['proposals']:
  addr=lambda r:(r['part'],r['measure'],int(r['note_index']))
  src=addr(c['source_note']);dst=addr(c['target_note']);identity=(c['unit'],c['system'],c['item'])
  if identity in seen:raise ValueError('duplicate source role proposal')
  seen.add(identity)
  for key in (src,dst):
   if key not in ev or key not in control_ev or _role_anchor_signature(ev[key])!=_role_anchor_signature(control_ev[key]):raise ValueError('caller/source music at role anchor changed')
  srcnode=ev[src]['node'];dstnode=ev[dst]['node'];target=c['target_note'];token=c['token'];start=ev[dst]['start'];staff=target['staff_number']
  if dstnode.find('chord') is not None:raise ValueError('source dynamic target is not the lead note')
  peer=c['state']=='source_proven_peer_dynamic_ink'
  existing=[d for d in dirs if (d['part'],d['measure'],d['staff'],d['start'])==(dst[0],dst[1],staff,start) and d['node'].find('direction-type/dynamics') is not None]
  same=[d for d in existing if d['node'].find('direction-type/dynamics/'+token) is not None]
  if existing and len(same)!=len(existing):raise ValueError('conflicting target dynamics require source review')
  if peer:
   if _role_anchor_signature(ev[src])[0]!=start:raise ValueError('peer dynamic notes are not simultaneous')
   if same:
    covered.append(dict(unit=c['unit'],item=c['item'],token=token,target_part=dst[0],target_measure=dst[1]))
    continue
  else:
   source_text=c.get('source_text',token)
   lyrics=[v for v in srcnode.findall('lyric') if v.get('number','1')==c['line_number'] and v.findtext('text','').lower()==source_text]
   original_lyrics=[v for v in control_ev[src]['node'].findall('lyric') if v.get('number','1')==c['line_number'] and v.findtext('text','').lower()==source_text]
   if len(original_lyrics)!=1:raise ValueError('native source lyric identity changed')
   if not lyrics:
    if len(same)!=1:raise ValueError('incomplete prior source role transfer')
    covered.append(dict(unit=c['unit'],item=c['item'],token=token,target_part=dst[0],target_measure=dst[1]));continue
   if len(lyrics)!=1 or len(same)>1:raise ValueError('ambiguous lyric or target dynamic')
   srcnode.remove(lyrics[0]);removed.append((src,canonical(lyrics[0])))
   neutralnote=neutral_ev[src]['node'];match=[v for v in neutralnote.findall('lyric') if canonical(v)==canonical(lyrics[0])]
   if len(match)!=1:raise ValueError('source lyric neutralization mismatch')
   neutralnote.remove(match[0])
  if not same:
   levels={d['node'].find('sound').get('dynamics') for d in control_dirs if d['node'].find('direction-type/dynamics/'+token) is not None and d['node'].find('sound') is not None and d['node'].find('sound').get('dynamics') is not None}
   if not levels and c['state']=='source_proven_repeated_p_dynamic':
    # DynamicsInter defaults to 45 for DYNAMICS_PP.
    levels={'45'}
   if len(levels)!=1:raise ValueError('source dynamic has no unique existing native sound level')
   fx,fy,fw,fh=c['full_bounds'];space=c['interline'];dr=E.Element('direction',placement=c.get('placement','above'));dt=E.SubElement(dr,'direction-type');dy=round(10*(c['target_staff_top']-(fy+fh/2))/space);dx=round(10*(fx-int(target['x']))/space);ds=E.SubElement(dt,'dynamics',{'default-y':str(dy),'relative-x':str(dx)});E.SubElement(ds,token)
   part=next(p for p in result.findall('part') if p.get('id')==dst[0])
   if any(int(v.text)>1 for v in part.findall('measure/attributes/staves')):E.SubElement(dr,'staff').text=staff
   E.SubElement(dr,'sound',dynamics=next(iter(levels)))
   measure=ev[dst]['measure'];measure.insert(list(measure).index(dstnode),dr);added.append((dst[:2],canonical(dr)));dirs.append(dict(part=dst[0],measure=dst[1],staff=staff,start=start,node=dr))
  changes.append(dict(unit=c['unit'],item=c['item'],token=token,source_part=src[0],source_measure=src[1],source_lyric_number=c.get('line_number'),source_evidence=c['state'],target_part=dst[0],target_measure=dst[1],target_staff=staff,onset=str(start),dynamics_already_present=bool(same)))
 if not changes:return original,dict(changes=[],covered=covered,pending=plan['pending'],all_other_score_fields_exact=True)
 after=copy.deepcopy(result)
 for (pid,mid),signature in added:
  measure=after.find(f"part[@id='{pid}']/measure[@number='{mid}']");matches=[d for d in measure.findall('direction') if canonical(d)==signature]
  if len(matches)!=1:raise ValueError('new source dynamic identity ambiguous')
  measure.remove(matches[0])
 if canonical(neutral)!=canonical(after):raise ValueError('role repair changed unrelated score fields')
 final_ev,_=_score_events(result)
 if {k:_music_signature(v) for k,v in oldev.items()}!={k:_music_signature(v) for k,v in final_ev.items()}:raise ValueError('role repair changed pitches, timing, voices or notes')
 return result,dict(changes=changes,covered=covered,pending=plan['pending'],all_other_score_fields_exact=True,all_note_pitches_durations_onsets_and_voices_exact=True)

if __name__=='__main__':
 import argparse
 from source_tempo_context import _score
 ap=argparse.ArgumentParser()
 for name in ('book','source','ledger','input','control','output','evidence'):ap.add_argument('--'+name,type=Path,required=True)
 a=ap.parse_args();a.evidence.mkdir(exist_ok=True)
 original=E.parse(a.input).getroot();plan=scan(a.book,a.source,a.ledger);(a.evidence/'source-role-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8');fixed,report=apply_roles(original,_score(a.control),plan,a.book,a.source,a.ledger)
 with a.output.open('xb') as stream:E.ElementTree(fixed).write(stream,encoding='utf-8',xml_declaration=True)
 report.update(source_book_sha256=plan['source_book_sha256'],head_ledger_sha256=plan['head_ledger_sha256'],source_images=plan['source_images'],plan_sha256=sha(a.evidence/'source-role-plan.json'),input_sha256=sha(a.input),output_sha256=sha(a.output))
 (a.evidence/'application.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');print('SOURCE_LYRIC_ROLE_REPAIR',len(report['changes']),len(report['covered']),len(report['pending']))
