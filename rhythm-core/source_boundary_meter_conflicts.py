"""Find note chords occupying independently repeated printed time-signature ink."""
from pathlib import Path
import zipfile,json,hashlib,xml.etree.ElementTree as E,collections,math
import cv2,numpy as np
from fractions import Fraction

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def box(n):return [int(n.find('bounds').get(k)) for k in ('x','y','w','h')]
def lines(st,x):
 out=[]
 for line in st.findall('lines/line'):
  pts=line.findall('point');a,b=pts[0],pts[-1];ax,ay,bx,by=[float(v) for v in [a.get('x'),a.get('y'),b.get('x'),b.get('y')]];y=ay if bx==ax else ay+(by-ay)*(x-ax)/(bx-ax);out.append((y,float(line.get('thickness','1'))))
 return out
def metrics(im,st,tb,refst,rb):
 x,y,w,h=tb;rx,ry,rw,rh=rb
 if (w,h)!=(rw,rh):return None
 a=im[y:y+h,x:x+w];b=im[ry:ry+h,rx:rx+w]
 if a.shape!=(h,w) or b.shape!=a.shape:return None
 mask=np.ones((h,w),bool)
 for staff,left,top in [(st,x,y),(refst,rx,ry)]:
  for ly,th in lines(staff,left):
   row=math.floor(ly-top+.5);pad=math.ceil(th/2);mask[max(0,row-pad):min(h,row+pad+1)]=False
 # Up to five known staff-line bands, no mask inferred from the candidate glyph.
 if mask.mean()<.45:return None
 values=[]
 for threshold in [160,208]:
  aa=(a<threshold)[mask];bb=(b<threshold)[mask]
  if aa.std()<.05 or bb.std()<.05:return None
  hit=np.logical_and(aa,bb).sum();v=dict(threshold=threshold,ncc=float(np.corrcoef(aa.astype(float),bb.astype(float))[0,1]),precision=float(hit/max(1,aa.sum())),recall=float(hit/max(1,bb.sum())))
  if v['ncc']<.95 or min(v['precision'],v['recall'])<.98:return None
  values.append(v)
 return dict(mask_fraction=float(mask.mean()),thresholds=values,quality=min(min(v[k] for k in ['ncc','precision','recall']) for v in values))
def source_time_stack(system, nodes, staff, chord, ref, refs, interline):
    """Allow a system-opening time after clef/key only with a local prefix witness."""
    left, _, width, _ = box(ref)
    stacks = system.findall('stack')
    ordinary = next((s for s in stacks if -.25 * interline <= left - float(s.get('left')) <= 1.5 * interline), None)
    if ordinary is not None or not stacks:
        return ordinary
    first = stacks[0]
    if not float(first.get('left')) + 1.5 * interline < left < float(first.get('right')) - 3.5 * interline:
        return None
    owner = next(p for p in system.findall('part') if staff in p.findall('staff'))
    measure = owner.find("measure[@id='{}']".format(first.get('id')))
    if measure is None or chord.get('id') not in measure.findtext('head-chords', '').split():
        return None
    if first.get('expected') != str(Fraction(int(ref.get('num')), int(ref.get('den')))):
        return None
    staff_id = staff.get('id')
    prefix = [nodes[node_id] for node_id in measure.findtext('clefs', '').split() + measure.findtext('keys', '').split()
              if node_id in nodes and nodes[node_id].get('staff') == staff_id]
    if not any(node.tag == 'clef' for node in prefix):
        return None
    if any(box(node)[0] + box(node)[2] > left for node in prefix if node.find('bounds') is not None and box(node)[0] < left):
        return None
    time_refs = [candidate for candidate in refs if candidate.get('num') == ref.get('num')
                 and candidate.get('den') == ref.get('den') and abs(box(candidate)[0] - left) <= .5 * interline]
    if len({candidate.get('staff') for candidate in time_refs}) < 3:
        return None
    own_chords = [nodes[key] for key in measure.findtext('head-chords', '').split()
                  if key != chord.get('id') and nodes[key].get('staff') == staff_id]
    if not own_chords or min(box(candidate)[0] for candidate in own_chords) < left + width + .4 * interline:
        return None
    return first

def scan(book,source,out):
 book,source,out=map(Path,(book,source,out));out.mkdir();findings=[];pending=[];plans=[]
 with zipfile.ZipFile(book) as z:
  for name in z.namelist():
   if not(name.startswith('sheet#') and name.endswith('.xml')):continue
   u=int(name.split('/')[0][6:]);im=cv2.imdecode(np.fromfile(source/f'unit-{u:03d}.png',np.uint8),0);digest=sha(source/f'unit-{u:03d}.png')
   for sy in E.fromstring(z.read(name)).findall('page/system'):
    staves={s.get('id'):s for s in sy.findall('part/staff')};nodes={n.get('id'):n for n in sy.find('sig/inters')};members=collections.defaultdict(list)
    for rel in sy.find('sig/relations'):
     if rel.find('containment') is not None:members[rel.get('source')].append(rel.get('target'))
    refs=[n for n in nodes.values() if n.tag=='time-custom' and int(n.get('num','0')) in range(2,13) and n.get('den') in ['2','4','8'] and float(n.get('grade','0'))>=.8]
    for chord in sy.findall('sig/inters/head-chord'):
     sid=chord.get('staff');st=staves.get(sid);heads=[nodes[i] for i in members[chord.get('id')]]
     if st is None or not heads or any(h.tag!='head' or h.get('shape')!='NOTEHEAD_VOID' or h.get('staff')!=sid for h in heads):continue
     hb=[box(h) for h in heads];left=min(b[0] for b in hb);right=max(b[0]+b[2] for b in hb);top=min(b[1] for b in hb);bottom=max(b[1]+b[3] for b in hb);g=lines(st,left);il=(g[-1][0]-g[0][0])/4;clusters=collections.defaultdict(list)
     for ref in refs:
      rb=box(ref)
      if ref.get('staff')==sid or abs(rb[0]-left)>.7*il:continue
      if not (g[0][0]-.35*il<=top and bottom<=g[-1][0]+.35*il and right-left<=2.2*il):continue
      refst=staves[ref.get('staff')];rg=lines(refst,rb[0]);ril=(rg[-1][0]-rg[0][0])/4
      if abs(il/ril-1)>.05:continue
      peers=[v for v in refs if v.get('num')==ref.get('num') and v.get('den')==ref.get('den') and abs(box(v)[0]-rb[0])<.5*il]
      if len({v.get('staff') for v in peers})<2:continue
      stack=source_time_stack(sy,nodes,st,chord,ref,refs,il)
      if stack is None:continue
      if any(n.get('staff')==sid and abs(box(n)[0]-rb[0])<il for n in refs):continue
      for dx in range(-3,4):
       for dy in range(-3,4):
        tb=[rb[0]+dx,math.floor(g[0][0]+.5)+dy,rb[2],rb[3]];m=metrics(im,st,tb,refst,rb)
        if m is not None:clusters[(stack.get('id'),ref.get('num'),ref.get('den'),tuple(tb))].append(dict(native_id=ref.get('id'),staff=ref.get('staff'),bounds=rb,**m))
     qualified=[]
     for (mid,num,den,tb),matches in clusters.items():
      perstaff={}
      for m in matches:
       if m['staff'] not in perstaff or m['quality']>perstaff[m['staff']]['quality']:perstaff[m['staff']]=m
      if len(perstaff)>=2:qualified.append(dict(unit=u,system=sy.get('id'),stack=mid,staff=sid,numerator=int(num),denominator=int(den),time_bounds=list(tb),chord=chord.get('id'),heads=[h.get('id') for h in heads],head_bounds=hb,source_sha256=digest,references=sorted(perstaff.values(),key=lambda v:-v['quality'])))
     identities={(v['stack'],v['numerator'],v['denominator']) for v in qualified}
     if len(identities)>1:pending.append(dict(unit=u,system=sy.get('id'),chord=chord.get('id'),reason='conflicting_source_time_identities'));continue
     if not qualified:continue
     q=max(qualified,key=lambda v:min(x['quality'] for x in v['references'][:2]));findings.append(q);row=[q[k] for k in ['unit','system','stack','staff','numerator','denominator']]+q['time_bounds']+[q['chord'],','.join(q['heads']),digest]
     for ref in q['references'][:2]:row.extend([ref['staff'],ref['native_id'],*ref['bounds']])
     plans.append('\t'.join(map(str,row)))
 report=dict(source_book=str(book),source_book_sha256=sha(book),findings=findings,pending=pending,rule='Two other same-column native source time fonts; no resizing; only known staff-line rows excluded; remaining>=45%; dual160/208 NCC>=.95 precision/recall>=.98; no duration-sum meter inference')
 (out/'plan.tsv').write_text('\n'.join(plans)+('\n' if plans else ''),encoding='utf8');(out/'source-time-heads.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return report
if __name__=='__main__':
 import sys
 r=scan(*sys.argv[1:]);print('SOURCE_TIME_HEAD_CONFLICTS',len(r['findings']),len(r['pending']))
