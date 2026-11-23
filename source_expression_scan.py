from pathlib import Path
from fractions import Fraction as F
import argparse,json,zipfile,xml.etree.ElementTree as E,csv,hashlib,functools,re,collections
import cv2,numpy as np
from source_expression_overlay import events,node_hash
ap=argparse.ArgumentParser()
for key in ['score','book','job','ledger','meters','tuplet_scan','tuplet_context','out']:ap.add_argument('--'+key.replace('_','-'),type=Path,required=True)
a=ap.parse_args();a.out.mkdir(exist_ok=True);root=E.parse(a.score).getroot();ev,mi=events(root);grand={p.get('id') for p in root.findall('part') if any(int(x.text)>1 for x in p.findall('measure/attributes/staves'))};rows=list(csv.DictReader(a.ledger.open(encoding='utf8'),delimiter='\t'));bychord=collections.defaultdict(list)
for row in rows:
 if row['part'] in grand:bychord[int(row['unit']),row['system'],row['chord']].append(row)
meters=json.loads(a.meters.read_text(encoding='utf8'));hashes={int(x['unit']):x['image_sha256'] for x in meters['source_images']}
for u,d in hashes.items():
 if hashlib.sha256((a.job/f'unit-{u:03d}.png').read_bytes()).hexdigest()!=d:raise ValueError('source meter image provenance changed')
state={};prev=None
for record in sorted(meters['results'],key=lambda r:(int(r['unit']),int(r['system']))):
 state[int(record['unit']),record['system']]=(prev,record['source_columns'])
 for col in sorted(record['source_columns'],key=lambda c:c['x']):
  if col['kind']=='courtesy':continue
  prev=dict(unit=record['unit'],system=record['system'],column=col) if not col['conflicting_time_at_same_column'] and len({v['staff'] for v in col['votes']})>=2 else None
def printed_meter(u,s,x):
 previous,cols=state.get((u,s),(None,[]))
 if any(c['conflicting_time_at_same_column'] and c['x']<=x for c in cols):return None
 usable=[c for c in cols if c['kind']!='courtesy' and not c['conflicting_time_at_same_column'] and c['x']<=x and len({v['staff'] for v in c['votes']})>=2];proof=dict(unit=u,system=s,column=usable[-1]) if usable else previous
 if proof is None:return None
 num,den=proof['column']['time'].split('/');return F(int(num)*4,int(den)),proof
@functools.lru_cache(maxsize=2)
def image(u):return cv2.imdecode(np.fromfile(a.job/f'unit-{u:03d}.png',np.uint8),0)
def box(n):return [int(n.find('bounds').get(k)) for k in ['x','y','w','h']]
def geom(st):
 ys=[float(n.get('y')) for n in st.findall('lines/line/point')];return min(ys),max(ys),(max(ys)-min(ys))/4
context=json.loads(a.tuplet_context.read_text(encoding='utf8'));digits=json.loads(a.tuplet_scan.read_text(encoding='utf8'));accepted={(int(v['unit']),v['system'],v['digit']) for v in context.get('native_decisions',[]) if v['state']=='accepted'};digit_regions=[]
for f in digits.get('findings',[]):
 if (int(f['unit']),f['system'],f['native_digit_id']) in accepted and f.get('state')=='source_supported_candidate' and f['source_sha256']==hashes[int(f['unit'])]:digit_regions.append(f)
artics=[];wedges=[];pending=[];inventory=[]
with zipfile.ZipFile(a.book) as archive:
 for name in archive.namelist():
  match=re.fullmatch(r'sheet#([1-9][0-9]*)/sheet#\1\.xml',name)
  if not match:continue
  u=int(match[1]);rt=E.fromstring(archive.read(name))
  for sy in rt.findall('page/system'):
   sid=sy.get('id');nodes={n.get('id'):n for n in sy.find('sig/inters')};rels=list(sy.find('sig/relations'));staffs={n.get('id'):n for n in sy.findall('part/staff')};words=[n for n in nodes.values() if n.tag=='word' and re.fullmatch(r'(?:cresc|decresc|dim|rit|rall)\.?',n.get('value','').strip(),re.I)]
   for n in sy.findall('sig/inters/articulation'):
    if n.get('shape') not in ['STACCATO','STACCATISSIMO']:continue
    links=[rel for rel in rels if rel.get('target')==n.get('id') and rel.find('chord-articulation') is not None]
    if len(links)!=1:continue
    cid=links[0].get('source');mapped=bychord.get((u,sid,cid),[])
    if not mapped:continue
    st=staffs[nodes[cid].get('staff')];space=geom(st)[2];x,y,w,h=box(n);patch=image(u)[y:y+h,x:x+w];kind=n.get('shape').lower();reason=None;proof=None
    if patch.size and int(patch.min())>=190 and int((patch<160).sum())==0:reason='no_source_black_articulation_ink';proof=dict(bounds=[x,y,w,h],minimum_gray=int(patch.min()),dark_pixels=0)
    for word in words if reason is None else []:
     xx,yy,ww,hh=box(word);pad=round(.25*space)
     if not(xx-pad<=x and x+w<=xx+ww+pad and yy-pad<=y and y+h<=yy+hh+pad):continue
     left=max(0,xx-pad);top=max(0,yy-pad);roi=image(u)[top:yy+hh+pad,left:xx+ww+pad];_,labels,stats,_=cv2.connectedComponentsWithStats((roi<160).astype(np.uint8),8);labs=set(int(v) for v in labels[y-top:y-top+h,x-left:x-left+w].ravel())-{0};valid=[]
     for lab in labs:
      sx,syy,sw,sh,area=map(int,stats[lab]);inside=int((labels[yy-top:yy-top+hh,xx-left:xx-left+ww]==lab).sum())
      if inside>=4 and sw>=w+2 and sh>=.6*hh:valid.append(lab)
     if valid:
      dark=(patch<160);target=labels[y-top:y-top+h,x-left:x-left+w];covered=np.logical_and(dark,np.isin(target,valid)).sum()/max(1,dark.sum())
      if covered>=.9:reason='source_articulation_is_connected_music_word_ink';proof=dict(word_id=word.get('id'),word=word.get('value'),word_bounds=[xx,yy,ww,hh],symbol_bounds=[x,y,w,h],connected_ink_fraction=float(covered));break
    for f in digit_regions if reason is None else []:
     if int(f['unit'])!=u or f['system']!=sid or cid not in f['chords']:continue
     dx,dy,dw,dh=f['source_digit_bounds']
     if not(dx<=x and x+w<=dx+dw and dy<=y and y+h<=dy+dh):continue
     actual=[]
     for c in f['chords']:
      for row in bychord.get((u,sid,c),[]):
       info=ev.get((row['part'],row['measure'],int(row['note_index'])))
       if info and info['node'].find('time-modification') is not None:actual.append(info['node'].findtext('time-modification/actual-notes'))
     if len(actual)==int(f['digit']) and all(value==f['digit'] for value in actual):reason='source_articulation_is_verified_tuplet_digit_ink';proof=dict(digit=f['digit'],digit_id=f['native_digit_id'],digit_bounds=f['source_digit_bounds'],source_group_chords=f['chords'],source_sha256=f['source_sha256']);break
    inventory.append(dict(unit=u,system=sid,native_id=n.get('id'),shape=n.get('shape'),source_reason=reason))
    if reason is None:continue
    candidates=[]
    for row in mapped:
     if row['source_sha256']!=hashes[u]:raise ValueError('articulation ledger source mismatch')
     key=row['part'],row['measure'],int(row['note_index']);info=ev.get(key)
     if info is None:continue
     nn=info['node'];symbol=nn.findall('notations/articulations/'+kind)
     if len(symbol)==1:candidates.append((row,nn))
    if len(candidates)!=1:continue
    row,nn=candidates[0];hx,hy,hw,hh=box(nodes[row['head']]);head_dark=float((image(u)[hy:hy+hh,hx:hx+hw]<160).mean())
    if head_dark<.4:continue
    artics.append(dict(unit=u,system=sid,native_id=n.get('id'),part=row['part'],measure=row['measure'],note_index=int(row['note_index']),kind=kind,note_sha256=node_hash(nn),source_sha256=hashes[u],reason=reason,evidence=proof,head_dark_fraction=head_dark))
   for wedge in sy.findall('sig/inters/wedge'):
    if any((rel.get('source')==wedge.get('id') or rel.get('target')==wedge.get('id')) and rel.find('chord-wedge') is not None for rel in rels):continue
    points=[[(float(q.get('x')),float(q.get('y'))) for q in wedge.findall(f'{tag}/*')] for tag in ['l1','l2']]
    if any(len(line)!=2 for line in points):continue
    l1,l2=[sorted(line) for line in points];left=(l1[0][0]+l2[0][0])/2;right=(l1[1][0]+l2[1][0])/2;low=min(q[1] for line in points for q in line);high=max(q[1] for line in points for q in line);spread0=abs(l1[0][1]-l2[0][1]);spread1=abs(l1[1][1]-l2[1][1]);direction='diminuendo' if spread0>spread1 else 'crescendo';shape='DIMINUENDO' if direction=='diminuendo' else 'CRESCENDO'
    if wedge.get('shape')!=shape:continue
    for part in sy.findall('part'):
     staves=part.findall('staff')
     if len(staves)!=2:continue
     staff=staves[1];_,bottom,space=geom(staff)
     if not(bottom+.6*space<=low and high<=bottom+6*space and right-left>=6*space and min(spread0,spread1)<=.25*space and .5*space<=max(spread0,spread1)<=3*space):continue
     stacks=sy.findall('stack')
     for measure,stack in zip(part.findall('measure'),stacks):
      if not(float(stack.get('left'))<=left<right<=float(stack.get('right')) and float(stack.get('right'))-right<=1.25*space):continue
      chords=[nodes[c] for c in measure.findtext('head-chords','').split() if nodes[c].get('staff')==staff.get('id')]
      if len(chords)!=1:continue
      cid=chords[0].get('id');mapped=bychord.get((u,sid,cid),[])
      if not mapped or any(row['shape']!='WHOLE_NOTE' for row in mapped):continue
      keys={(row['part'],row['measure']) for row in mapped}
      if len(keys)!=1:continue
      key=next(iter(keys));info=mi[key];printed=printed_meter(u,sid,left)
      if printed is None or printed[0]!=info['meter'] or info['end_cursor']!=info['meter']:continue
      indices=[int(row['note_index']) for row in mapped];staffnum=mapped[0]['staff_number'];notes=[(k,v) for k,v in ev.items() if k[:2]==key and v['node'].findtext('staff','1')==staffnum]
      if {k[2] for k,v in notes}!=set(indices) or any(v['start']!=0 or v['duration']!=printed[0] for k,v in notes):continue
      head_left=sum(float(row['x']) for row in mapped)/len(mapped)
      if abs(head_left-left)>.5*space:continue
      coverage=[];line_offsets=[]
      for line in [l1,l2]:
       (x0,y0),(x1,y1)=line;xs=np.arange(round(x0)+2,round(x1)-1);base=y0+(y1-y0)*(xs-x0)/(x1-x0);options=[]
       for offset in sorted(range(-round(.2*space),round(.2*space)+1),key=lambda n:abs(n)):
        ys=np.rint(base+offset).astype(int);hits=np.zeros(len(xs),dtype=bool)
        for dy in [-1,0,1]:
         for dx in [-1,0,1]:hits|=image(u)[ys+dy,xs+dx]<160
        options.append((float(hits.mean()),offset))
       value,offset=max(options,key=lambda v:v[0]);coverage.append(value);line_offsets.append(offset)
      if min(coverage)<.95:continue
      existing=[dr for dr in info['node'].findall('direction') if dr.findtext('staff','1')==staffnum and dr.find('direction-type/wedge') is not None]
      if existing:
       if any(dr.find('direction-type/wedge').get('type')==direction for dr in existing):continue
       pending.append(dict(unit=u,system=sid,native_id=wedge.get('id'),reason='existing_wedge_conflict'));continue
      wedges.append(dict(unit=u,system=sid,native_id=wedge.get('id'),part=key[0],measure=key[1],staff=staffnum,kind=direction,measure_sha256=node_hash(info['node']),source_sha256=hashes[u],line_coverage=coverage,line_pixel_offsets=line_offsets,lines=points,source_meter=printed[1],expected=str(printed[0]),source_bounds=box(wedge),spread=round(max(spread0,spread1)/space*10,2),default_y=round(-40-((low+high)/2-bottom)/space*10,2)))
report=dict(algorithm='source-graphic-ownership-and-unlinked-wedge-v1',input_xml_sha256=hashlib.sha256(a.score.read_bytes()).hexdigest(),source_book_sha256=hashlib.sha256(a.book.read_bytes()).hexdigest(),source_images=meters['source_images'],articulation_inventory=inventory,articulation_removals=artics,wedge_additions=wedges,pending=pending,whole_score_accuracy_verified=False);(a.out/'source-expression-plan.json').write_text(json.dumps(report,indent=2),encoding='utf8');print('SOURCE_EXPRESSION_SCAN',len(artics),'false articulation marks',len(wedges),'unlinked source wedges',len(pending),'pending')
