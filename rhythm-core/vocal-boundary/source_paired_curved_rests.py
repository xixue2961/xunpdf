"""Pair source curved rest misclassifications at adjacent equal-pitch system boundaries."""
from pathlib import Path
import sys,json,csv,zipfile,hashlib,xml.etree.ElementTree as E
import numpy as np
from PIL import Image
from source_rest_cap import inspect

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def bounds(n):return [int(n.find('bounds').get(k)) for k in ('x','y','w','h')]
def select_pairs(records,font):
 if len(font)<6 or len({(v['unit'],v['system']) for v in font})<2:return []
 def compatible(a,b):
  return a['anchor']['role']=='end' and b['anchor']['role']=='start' and b['part']==a['part'] and b['system_order']==a['system_order']+1 and int(b['anchor']['measure'])==int(a['anchor']['measure'])+1 and tuple(b['anchor'][k] for k in ('step','octave','key_fifths'))==tuple(a['anchor'][k] for k in ('step','octave','key_fifths'))
 pairs=[]
 for a in records:
  matches=[b for b in records if compatible(a,b)]
  if len(matches)!=1:continue
  b=matches[0]
  if sum(compatible(q,b) for q in records)!=1:continue
  pairs.append(dict(part=a['part'],before=a,after=b))
 return pairs

def scan(book,images,ledger,out):
 book,images,ledger,out=map(Path,(book,images,ledger,out));out.mkdir();rows=list(csv.DictReader(ledger.open(encoding='utf8'),delimiter='\t'));records=[];cache={};font=[];source_hashes={};system_order=0
 with zipfile.ZipFile(book) as z:
  names=sorted((n for n in z.namelist() if n.startswith('sheet#') and n.endswith('.xml')),key=lambda n:int(n.split('/')[0][6:]))
  for name in names:
   unit=int(name.split('/')[0][6:]);root=E.fromstring(z.read(name));source=images/f'unit-{unit:03d}.png';cache[unit]=np.array(Image.open(source).convert('L'));source_hashes[str(unit)]=sha(source)
   for sy in root.findall('page/system'):
    order=system_order;system_order+=1;nodes={n.get('id'):n for n in sy.find('sig/inters')};rels=list(sy.findall('sig/relations/relation'));stacks=sy.findall('stack');owners={};partlogical={}
    for part in sy.findall('part'):
     measures=part.findall('measure')
     if len(measures)!=len(stacks):continue
     logical={v['part'] for v in rows if v['unit']==str(unit) and v['system']==sy.get('id') and v['native_part']==part.get('id') and v['repeat_copy']=='false'}
     if len(logical)!=1:continue
     partlogical[part.get('id')]=next(iter(logical))
     for index,m in enumerate(measures):
      for chord in (m.findtext('head-chords','')+' '+m.findtext('rest-chords','')).split():owners[chord]=(part,m,stacks[index],index)
    chordmembers={}
    for rel in rels:
     if rel.find('containment') is not None and rel.get('source') in owners:chordmembers.setdefault(rel.get('source'),[]).append(rel.get('target'))
    nodechord={nid:cid for cid,members in chordmembers.items() for nid in members}
    globalstack={}
    for row in rows:
     if row['unit']!=str(unit) or row['system']!=sy.get('id') or row['repeat_copy']!='false' or row['implicit']=='true' or row['chord'] not in owners:continue
     owner=owners[row['chord']];index=owner[3]
     if owner[2].get('special')=='CAUTIONARY':continue
     globalstack.setdefault(index,set()).add(row['measure'])
    for rest in sy.findall('sig/inters/rest'):
     if rest.get('shape')!='HALF_REST' or rest.get('id') not in nodechord:continue
     cid=nodechord[rest.get('id')];part,m,stack,index=owners[cid]
     if len(part.findall('staff'))!=1:continue
     staff=part.find('staff');sid=staff.get('id');ys=[float(v.find('point').get('y')) for v in staff.findall('lines/line')];space=(ys[-1]-ys[0])/4;b=bounds(rest);baseline=min(ys,key=lambda v:abs(v-(b[1]+b[3])));profile=inspect(cache[unit],b,space,baseline)
     # A font reference is a solid, flat, central staff-positioned rest in the same single-staff source font.
     if abs(float(rest.get('pitch','99')))<=1.5 and float(rest.get('grade','0'))>=.7 and b[3]>=.3*space:
      x,y,w,h=b;patch=cache[unit][y:y+h,x:x+w];tops=[]
      for threshold in (160,208):
       mask=patch<threshold;top=[np.flatnonzero(mask[:,xx]).min() for xx in range(1,w-1) if mask[:,xx].any()];tops.append(float(np.std(top)) if top else 999)
      if max(tops)<=.08*space and float((patch<208).mean())>=.6:font.append(dict(unit=unit,system=sy.get('id'),rest=rest.get('id'),bounds=b,grade=rest.get('grade')))
     if not profile['accepted'] or stack.get('special')=='CAUTIONARY':continue
     logical=partlogical[part.get('id')];headchords={k for k,o in owners.items() if o[0] is part and o[2].get('special')!='CAUTIONARY' and nodes[k].tag=='head-chord'};heads=[nodes[n] for k in headchords for n in chordmembers.get(k,[]) if nodes[n].tag=='head' and float(nodes[n].get('grade','0'))>=.5 and nodes[n].get('staff')==sid]
     if not heads:continue
     centers=[bounds(n)[0]+bounds(n)[2]/2 for n in heads];extremes={'start':min(centers),'end':max(centers)};anchors=[]
     for role,edge in extremes.items():
      for head in heads:
       hb=bounds(head);hx=hb[0]+hb[2]/2;hy=hb[1]+hb[3]/2;rx=b[0]+b[2]/2;dy=hy-b[1]
       if abs(hx-edge)>.5*space or not .6*space<=dy<=2.5*space:continue
       dx=hx-rx
       if not(.2*space<=dx<=2*space if role=='start' else -2*space<=dx<=-.2*space):continue
       owner=owners[nodechord[head.get('id')]];bars=globalstack.get(owner[3],set())
       if len(bars)!=1:continue
       try:bar=int(next(iter(bars)))
       except ValueError:continue
       # No real rest may interrupt the alleged cross-system connection.
       if role=='start' and any(nodes[n].tag=='rest' and n!=rest.get('id') and bounds(nodes[n])[0]<hb[0] for k in owners if owners[k][0] is part for n in chordmembers.get(k,[])):continue
       clefs=[v for v in sy.findall('sig/inters/clef') if v.get('staff')==sid and bounds(v)[0]<=hx];clefs.sort(key=lambda v:bounds(v)[0]);clef=clefs[-1] if clefs else None
       if clef is None:continue
       shape=clef.get('shape');middle={'G_CLEF':34,'G_CLEF_8VB':27,'F_CLEF':22}.get(shape)
       if middle is None:continue
       pitch=round(float(head.get('pitch')));diatonic=middle-pitch;octave,step=divmod(diatonic,7);keys=[v for v in sy.findall('sig/inters/key') if v.get('staff')==sid and v.find('bounds') is not None and bounds(v)[0]<=hx];keys.sort(key=lambda v:bounds(v)[0]);fifths=keys[-1].get('fifths') if keys else None
       if fifths is None or any(r.get('target')==head.get('id') and any('alter' in c.tag or 'accidental' in c.tag for c in r) for r in rels):continue
       anchors.append(dict(role=role,head=head.get('id'),head_glyph=head.get('glyph'),head_bounds=hb,chord=nodechord[head.get('id')],measure=str(bar),step='CDEFGAB'[step],octave=octave,key_fifths=int(fifths),clef=clef.get('id'),clef_shape=shape))
     if len(anchors)!=1:continue
     records.append(dict(unit=unit,system=sy.get('id'),system_order=order,staff=sid,part=logical,rest=rest.get('id'),rest_chord=cid,rest_glyph=rest.get('glyph'),rest_bounds=b,source_sha256=source_hashes[str(unit)],profile=profile,anchor=anchors[0]))
 pairs=select_pairs(records,font)
 report=dict(scope='Same-book rectangular-rest font references plus stable curved contours, lower-note-ink protection, and adjacent equal-pitch physical system boundaries. No fixed score/measure/head IDs. Proposed false-rest declassification, no final XML acceptance.',book=str(book),book_sha256=sha(book),images=str(images),source_images=source_hashes,ledger_sha256=sha(ledger),font_references=font,curved_boundary_records=records,pairs=pairs,production_installed=False);(out/'paired-curved-rest-plan.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');lines=[]
 for pair in pairs:
  for q in (pair['before'],pair['after']):lines.append('\t'.join(map(str,[q['unit'],q['system'],q['rest'],q['rest_glyph'],*q['rest_bounds']])))
 (out/'retire-plan.tsv').write_text('\n'.join(lines)+'\n',encoding='utf8');return report
if __name__=='__main__':
 result=scan(*sys.argv[1:]);print(json.dumps(dict(font_references=len(result['font_references']),boundary_records=len(result['curved_boundary_records']),pairs=[dict(part=p['part'],before=p['before']['anchor'],after=p['after']['anchor']) for p in result['pairs']]),ensure_ascii=False))
