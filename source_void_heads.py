"""Source-backed empty-head evidence before native archive loading.
Conservative candidate only: no pitch/time is inferred; native IDs are never hardcoded.
"""
from pathlib import Path
import hashlib,json,zipfile,xml.etree.ElementTree as E,collections
import numpy as np
from PIL import Image

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def remaining_ink(image,bounds,lines,stems,threshold):
 x,y,w,h=bounds
 if x<0 or y<0 or w<10 or h<8 or y+h>image.shape[0] or x+w>image.shape[1]:raise ValueError('unsupported head bounds')
 mask=(image[y:y+h,x:x+w]<threshold).copy();yy,xx=np.mgrid[y:y+h,x:x+w]
 for line in lines:
  pts=line['points'];xs,ys=zip(*pts)
  if len(pts)<2 or any(b<=a for a,b in zip(xs,xs[1:])):raise ValueError('invalid source staff geometry')
  mask[np.abs(yy-np.interp(xx,xs,ys))<=line['thickness']/2+.5]=False
 for stem in stems:
  ax,ay,bx,by=stem['median']
  if abs(by-ay)<.5:raise ValueError('degenerate source stem')
  xs=ax+(yy-ay)*(bx-ax)/(by-ay)
  mask[(np.abs(xx-xs)<=stem['width']/2+.75)&(yy>=min(ay,by)-.5*h)&(yy<=max(ay,by)+.5*h)]=False
 return dict(ink=int(mask.sum()),ratio=float(mask.mean()),columns=int(np.any(mask,axis=0).sum()),rows=int(np.any(mask,axis=1).sum()))

def faint_curve_support(image,bounds,lines,stems):
 # Horizontal staff and codec bands have nearly constant row brightness.
 # Protect faint curved ink by its horizontal contrast away from the stem halo.
 x,y,w,h=bounds;pixels=image[y:y+h,x:x+w];yy,xx=np.mgrid[y:y+h,x:x+w];allowed=np.ones((h,w),dtype=bool)
 for line in lines:
  lx,ly=zip(*line['points']);allowed[np.abs(yy-np.interp(xx,lx,ly))<=line['thickness']/2+.5]=False
 for stem in stems:
  ax,ay,bx,by=stem['median']
  if abs(by-ay)<.5:raise ValueError('degenerate source stem')
  xs=ax+(yy-ay)*(bx-ax)/(by-ay);allowed[np.abs(xx-xs)<=stem['width']/2+.75+.2*w]=False
 max_contrast=0;segments=0
 for pixels_row,allowed_row in zip(pixels,allowed):
  indices=np.flatnonzero(allowed_row)
  for run in np.split(indices,np.flatnonzero(np.diff(indices)>1)+1):
   if len(run)<4:continue
   values=pixels_row[run].astype('int16');segments+=1;max_contrast=max(max_contrast,int(values.max()-values.min()))
 return dict(max_row_contrast=max_contrast,independent_segments=segments,protect_head=segments==0 or max_contrast>=4)

def build(job):
 job=Path(job);book=job/'raw/input.omr';book_hash=sha(book);rows=[];images={};pending=[]
 with zipfile.ZipFile(book) as z:
  for name in z.namelist():
   if not name.startswith('sheet#') or not name.endswith('.xml'):continue
   unit=int(name.split('/')[0][6:]);imagepath=job/f'unit-{unit:03d}.png';images[str(unit)]=sha(imagepath);image=np.asarray(Image.open(imagepath).convert('L'));sheet=E.fromstring(z.read(name))
   for sy in sheet.findall('page/system'):
    inters={e.get('id'):e for e in sy.findall('sig/inters/*')};staffs={s.get('id'):s for s in sy.findall('part/staff')};relations=sy.findall('sig/relations/relation');stems_by_head=collections.defaultdict(list);members={};attached=collections.defaultdict(list)
    for rel in relations:
     for endpoint in (rel.get('source'),rel.get('target')):attached[endpoint].append(rel)
     if rel.find('head-stem') is not None:stems_by_head[rel.get('source')].append(rel.get('target'))
     if rel.find('containment') is not None and inters.get(rel.get('source')) is not None and inters[rel.get('source')].tag=='head-chord':members[rel.get('target')]=rel.get('source')
    for head in inters.values():
     if head.tag!='head' or head.get('shape')!='NOTEHEAD_VOID' or head.get('staff') not in staffs:continue
     try:
      bounds=[int(head.find('bounds').get(k)) for k in ('x','y','w','h')];lines=[dict(thickness=float(l.get('thickness')),points=[(float(q.get('x')),float(q.get('y'))) for q in l.findall('point')]) for l in staffs[head.get('staff')].findall('lines/line')]
      stems=[]
      for stemid in stems_by_head[head.get('id')]:
       stem=inters[stemid];a,b=stem.find('median/p1'),stem.find('median/p2');stems.append(dict(width=float(stem.get('width')),median=[float(q) for q in (a.get('x'),a.get('y'),b.get('x'),b.get('y'))]))
      metrics=[remaining_ink(image,bounds,lines,stems,t) for t in (96,160,208)];faint_guard=faint_curve_support(image,bounds,lines,stems)
      row=dict(unit=unit,system=sy.get('id'),head=head.get('id'),staff=head.get('staff'),chord=members.get(head.get('id')),grade=float(head.get('grade')),glyph=head.get('glyph'),bounds=bounds,stems=stems,threshold_metrics=metrics,faint_guard=faint_guard,relation_kinds=sorted({c.tag for rel in attached[head.get('id')] for c in rel}))
      rows.append(row)
     except (ValueError,TypeError,AttributeError) as exc:pending.append(dict(unit=unit,system=sy.get('id'),head=head.get('id'),reason=str(exc)))
 groups=collections.defaultdict(list)
 for row in rows:groups[row['unit'],row['system'],row['chord']].append(row)
 candidates=[]
 for row in rows:
  if not row['chord'] or not row['stems'] or row['grade']>=.25:continue
  w,h=row['bounds'][2:]
  if not all(m['ratio']<.025 and m['columns']<.2*w for m in row['threshold_metrics']):continue
  if row['faint_guard']['protect_head']:continue
  peers=[q for q in groups[row['unit'],row['system'],row['chord']] if q is not row and all(m['ratio']>.075 and m['columns']>=.6*q['bounds'][2] for m in q['threshold_metrics'])]
  if len(peers)<2:continue
  if set(row['relation_kinds'])-{'head-stem','head-head','containment','exclusion'}:pending.append(dict(unit=row['unit'],system=row['system'],head=row['head'],reason='unsupported semantic attachments',kinds=row['relation_kinds']));continue
  candidates.append(dict(**row,source_supported_peers=sorted(q['head'] for q in peers)))
 if sha(book)!=book_hash:raise ValueError('native archive changed during proof')
 for unit,digest in images.items():
  if sha(job/f'unit-{int(unit):03d}.png')!=digest:raise ValueError('source image changed during proof')
 return dict(scope='source preflight: source mask has no supported void head after staff/stem removal at three thresholds plus independent faint-curve protection, >=2 supported same-chord peers, strict relation filter; source archive and raster preserved',book_sha256=book_hash,source_images=images,head_count=len(rows),candidates=candidates,pending=pending)

def apply(job,plan,view):
 job=Path(job);view=Path(view)
 if build(job)!=plan:raise ValueError('source void-head proof changed or plan was altered')
 if view.exists():raise ValueError('preserve previous source view')
 if not plan['candidates']:return job
 (view/'raw').mkdir(parents=True);source=job/'raw/input.omr';target=view/'raw/input.omr';by_sheet=collections.defaultdict(list)
 for q in plan['candidates']:by_sheet[q['unit']].append(q)
 with zipfile.ZipFile(source) as src,zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED) as dst:
  for item in src.infolist():
   data=src.read(item.filename)
   if item.filename.startswith('sheet#') and item.filename.endswith('.xml'):
    unit=int(item.filename.split('/')[0][6:])
    if unit in by_sheet:
     tree=E.fromstring(data)
     for q in by_sheet[unit]:
      sy=next(v for v in tree.findall('page/system') if v.get('id')==q['system']);inters=sy.find('sig/inters');rels=sy.find('sig/relations');head=next(v for v in inters if v.tag=='head' and v.get('id')==q['head'])
      if head.get('glyph')!=q['glyph'] or head.get('shape')!='NOTEHEAD_VOID' or [int(head.find('bounds').get(k)) for k in ('x','y','w','h')]!=q['bounds']:raise ValueError('source head identity changed')
      if set(head.attrib)-{'pitch','shape','glyph','grade','ctx-grade','staff','id'}:raise ValueError('unhandled source head attributes')
      inters.remove(head)
      for rel in list(rels):
       if q['head'] in (rel.get('source'),rel.get('target')):
        if set(c.tag for c in rel)-{'head-stem','head-head','containment','exclusion'}:raise ValueError('unproved semantic attachment')
        rels.remove(rel)
      for notes in sy.findall('part/staff/notes'):
       if notes.text:notes.text=' '.join(v for v in notes.text.split() if v!=q['head'])
     data=E.tostring(tree,encoding='utf-8',xml_declaration=True)
   dst.writestr(item,data)
 import os,shutil
 for image in job.glob('unit-*.png'):os.link(image,view/image.name)
 if (job/'source-map.json').is_file():shutil.copy2(job/'source-map.json',view/'source-map.json')
 if sha(source)!=plan['book_sha256']:raise ValueError('original archive changed')
 for unit,digest in plan['source_images'].items():
  if sha(job/f'unit-{int(unit):03d}.png')!=digest:raise ValueError('source raster changed')
 (view/'source-void-head-proof.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8')
 return view

if __name__=='__main__':
 import sys
 out=Path(sys.argv[2]);plan=build(Path(sys.argv[1]));out.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8');print('SOURCE_VOID_HEAD_PLAN',len(plan['candidates']),'pending',len(plan['pending']));print([(q['unit'],q['system'],q['head']) for q in plan['candidates']])
