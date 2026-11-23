"""Find weak automatic caesuras whose source ink belongs to complete native curves."""
from pathlib import Path
import zipfile,xml.etree.ElementTree as E,json,hashlib,math,functools
import cv2,numpy as np

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def box(n):return [int(n.find('bounds').get(k)) for k in ['x','y','w','h']]
def glyph_mask(g):
 r=g.find('run-table');w,h=int(r.get('width')),int(r.get('height'));mask=np.zeros((h,w),bool)
 for i,row in enumerate(r.findall('runs')):
  pos=0
  for j,length in enumerate(map(int,(row.text or '').split())):
   if j%2==0:
    if r.get('orientation')=='VERTICAL':mask[pos:pos+length,i]=True
    else:mask[i,pos:pos+length]=True
   pos+=length
 return mask

def curve_points(host):
 if host.tag=='slur':
  c=host.find('curve');p={k:float(v) for k,v in c.attrib.items()};t=np.linspace(0,1,1025);return [np.column_stack(((1-t)**3*p['x1']+3*(1-t)**2*t*p['ctrlx1']+3*(1-t)*t*t*p['ctrlx2']+t**3*p['x2'],(1-t)**3*p['y1']+3*(1-t)**2*t*p['ctrly1']+3*(1-t)*t*t*p['ctrly2']+t**3*p['y2']))]
 out=[]
 for k in ['l1','l2']:
  a,b=host.find(k);out.append(np.column_stack((np.linspace(float(a.get('x')),float(b.get('x')),1025),np.linspace(float(a.get('y')),float(b.get('y')),1025))))
 return out

def trace_support(im,points,radius,space):
 results=[]
 for line in points:
  count=max(16,min(512,math.ceil(np.linalg.norm(np.diff(line,axis=0),axis=1).sum()/(.5*space))));indices=np.linspace(0,len(line)-1,count).round().astype(int);thresholds=[]
  for threshold in [160,208]:
   states=[]
   for xx,yy in line[indices]:
    left=max(0,math.floor(xx-radius));right=min(im.shape[1],math.ceil(xx+radius)+1);top=max(0,math.floor(yy-radius));bottom=min(im.shape[0],math.ceil(yy+radius)+1);ys,xs=np.mgrid[top:bottom,left:right];mask=(xs-xx)**2+(ys-yy)**2<=radius**2;states.append(bool(((im[top:bottom,left:right]<threshold)&mask).any()))
   gap=run=0
   for value in states:run=0 if value else run+1;gap=max(gap,run)
   thresholds.append(dict(threshold=threshold,samples=count,coverage=sum(states)/count,max_gap=gap))
  results.append(thresholds)
 return results

def scan(book,images,out):
 book,images,out=map(Path,(book,images,out));out.mkdir();proposals=[];pending=[];inventory=[];rows=[]
 @functools.lru_cache(maxsize=2)
 def image(u):return cv2.imdecode(np.fromfile(images/f'unit-{u:03d}.png',np.uint8),0)
 with zipfile.ZipFile(book) as archive:
  for name in archive.namelist():
   if not(name.startswith('sheet#') and name.endswith('.xml')):continue
   u=int(name.split('/')[0][6:]);sheet=E.fromstring(archive.read(name));glyphs={g.get('id'):g for g in sheet.findall('glyph-index/glyph')}
   for sy in sheet.findall('page/system'):
    nodes={n.get('id'):n for n in sy.find('sig/inters')};rels=list(sy.find('sig/relations'));members={}
    for rel in rels:
     if rel.find('containment') is not None and nodes[rel.get('source')].tag=='head-chord':members[rel.get('target')]=rel.get('source')
    owner={};grand=set();staffs={}
    for part in sy.findall('part'):
     for st in part.findall('staff'):
      staffs[st.get('id')]=st
      if len(part.findall('staff'))==2:grand.add(st.get('id'))
     for m in part.findall('measure'):
      for c in m.findtext('head-chords','').split():owner[c]=(part.get('id'),m.get('id'))
    hosts=[]
    for host in nodes.values():
     if host.tag not in ['slur','wedge'] or host.find('bounds') is None or float(host.get('ctx-grade',host.get('grade','0')))<.7:continue
     ends={}
     for rel in rels:
      relation=rel.find('slur-head' if host.tag=='slur' else 'chord-wedge')
      if relation is None:continue
      if host.tag=='slur' and rel.get('source')==host.get('id'):cid=members.get(rel.get('target'))
      elif host.tag=='wedge' and rel.get('target')==host.get('id'):cid=rel.get('source')
      else:continue
      if cid in owner:ends.setdefault(relation.get('side'),[]).append(cid)
     if set(ends)!=set(['LEFT','RIGHT']) or any(len(v)!=1 for v in ends.values()):continue
     a,b=ends['LEFT'][0],ends['RIGHT'][0]
     if a==b or owner[a]!=owner[b] or box(nodes[a])[0]>box(nodes[b])[0]:continue
     hosts.append((host,a,b))
    for pause in sy.findall('sig/inters/caesura'):
     links=[rel for rel in rels if rel.get('target')==pause.get('id') and rel.find('chord-pause') is not None];rec=dict(unit=u,system=sy.get('id'),pause=pause.get('id'),bounds=box(pause));inventory.append(rec)
     if pause.get('manual')=='true' or float(pause.get('grade','1'))>=.35 or pause.get('glyph') not in glyphs or len(links)!=1:continue
     chord=links[0].get('source');sid=nodes[chord].get('staff')
     if sid not in grand or chord not in owner:continue
     if any(rel not in links and (rel.get('source')==pause.get('id') or rel.get('target')==pause.get('id')) for rel in rels):continue
     st=staffs[sid];ys=[float(p.get('y')) for p in st.findall('lines/line/point')];space=(max(ys)-min(ys))/4;radius=.35*space;x,y,bw,bh=box(pause);im=image(u);patch=im[y:y+bh,x:x+bw];g=glyphs[pause.get('glyph')];mask=glyph_mask(g);gx,gy=int(g.get('left')),int(g.get('top'));own=im[gy:gy+mask.shape[0],gx:gx+mask.shape[1]]
     if not mask.any():continue
     precision=[float((own[mask]<t).mean()) for t in [160,208]]
     if any(v>=.95 for v in precision):continue
     matches=[]
     for host,a,b in hosts:
      hx,hy,hw,hh=box(host)
      if owner[a]!=owner[chord] or not(hx<=x and x+bw<=hx+hw and hy<=y and y+bh<=hy+hh) or hw<max(6*space,3*bw):continue
      points=curve_points(host);allpoints=np.concatenate(points);coverage=[]
      for threshold in [160,208]:
       yy,xx=np.where(patch<threshold)
       if len(xx):
        d=np.min((xx[:,None]+x-allpoints[:,0])**2+(yy[:,None]+y-allpoints[:,1])**2,axis=1);covered=float((d<=radius**2).mean())
       else:covered=1.
       coverage.append(dict(threshold=threshold,source_ink_pixels=len(xx),curve_owned_fraction=covered))
      if coverage[-1]['source_ink_pixels']<8 or any(v['curve_owned_fraction']<.98 for v in coverage):continue
      support=trace_support(im,points,radius,space)
      if any(v['coverage']<.95 or v['max_gap']>1 for group in support for v in group):continue
      matches.append(dict(host=host.get('id'),host_type=host.tag,host_bounds=box(host),left_chord=a,right_chord=b,source_ink=coverage,source_host_trace=support))
     if len(matches)!=1:
      if matches:pending.append(dict(rec,reason='multiple_source_curve_owners'))
      continue
     q=dict(rec,staff=sid,chord=chord,glyph=pause.get('glyph'),glyph_bounds=[gx,gy,*mask.shape[::-1]],source_glyph_foreground_precision160=precision[0],source_glyph_foreground_precision208=precision[1],space=space,source_sha256=sha(images/f'unit-{u:03d}.png'),**matches[0]);proposals.append(q);rows.append('\t'.join(map(str,[u,sy.get('id'),sid,pause.get('id'),pause.get('glyph'),chord,matches[0]['host_type'],matches[0]['host'],matches[0]['left_chord'],matches[0]['right_chord'],x,y,bw,bh,q['source_sha256']])))
 report=dict(source_book=str(book),source_book_sha256=sha(book),proposals=proposals,pending=pending,caesura_inventory=inventory,rule='Weak automatic piano caesura; its own glyph source ink contradicted at both95% checks; >=98% original crop ink belongs to one complete long slur/wedge with independently95%-supported source trace; no note edits')
 (out/'source-curve-pause-plan.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');(out/'plan.tsv').write_text('\n'.join(rows)+('\n' if rows else ''),encoding='utf8');print('SOURCE_CURVE_PAUSES',len(proposals),'OF',len(inventory),'PENDING',len(pending));return report
if __name__=='__main__':
 import sys
 scan(*sys.argv[1:])
