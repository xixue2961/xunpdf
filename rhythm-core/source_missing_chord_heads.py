"""Source-backed missing chord-head evidence; no score/bar/note constants.
Use actual offline local segmentation, independent native peers, source head-font pixels,
and continuous printed stem ink. A missing head must join one uniquely owned source stem.
Does not copy HOMR's transcription, pitch spelling, meter, or note grouping.
"""
from pathlib import Path
import zipfile,xml.etree.ElementTree as E,json,hashlib,math
from collections import Counter
import cv2,numpy as np

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def box(n):return [int(n.find('bounds').get(k)) for k in ['x','y','w','h']]
def center(b):return (b[0]+b[2]/2,b[1]+b[3]/2)
def staff_info(st):
 ys=[float(p.get('y')) for p in st.findall('lines/line/point')];return min(ys),max(ys),(max(ys)-min(ys))/4

def load_witness(mapping_file):
 mapping_file=Path(mapping_file);folder=mapping_file.parent;mapping=json.loads(mapping_file.read_text(encoding='utf8'));source=Path(mapping['source']);assert sha(source)==mapping['source_sha256'],'source image changed';inputs=list(folder.glob('*.segmentation-shapes.json'));assert len(inputs)==1,'ambiguous segmentation outputs';shapes_path=inputs[0];stem=shapes_path.name.removesuffix('.segmentation-shapes.json');input_image=folder/(stem+'.png');shapes=json.loads(shapes_path.read_text(encoding='utf8'));components_path=folder/(stem+'.components.json');components=json.loads(components_path.read_text(encoding='utf8'));assert sha(input_image)==mapping['input_sha256']==shapes['source_sha256']==components['input_sha256'],'segmentation input changed';assert sha(shapes_path)==components['shapes_sha256'],'segmentation geometry changed';assert sha(folder/(stem+'.notehead-mask.png'))==components['mask_sha256'],'component mask changed';assert json.loads((folder/(stem+'.offline.json')).read_text())==dict(network_blocked=True,attempts=[]),'offline source proof absent';assert json.loads((folder/(stem+'.runtime.json')).read_text())['external_module_files']=={},'external module provenance';profiles=json.loads((folder/(stem+'.providers.json')).read_text());assert sum(sum(v['nodes'].values()) for v in profiles)>0,'no actual local model computation';assert all(sha(m['path'])==m['sha256'] for m in components['models']),'local source models changed';ox,oy,x1,y1=mapping['crop'];im=cv2.imdecode(np.frombuffer(source.read_bytes(),np.uint8),0);cropped=cv2.imdecode(np.frombuffer(input_image.read_bytes(),np.uint8),0);assert np.array_equal(im[oy:y1,ox:x1],cropped),'witness is not unchanged original pixels';notes=[]
 for idx,n in enumerate(shapes['notes']):
  m=next(v for v in components['head_membership'] if v['note_index']==idx);comp=next((v for v in components['components'] if v['component']==m['component']),None);notes.append(dict(index=idx,center=[n['center'][0]+ox,n['center'][1]+oy],position=n['staff_position'],box=[n['box'][0]+ox,n['box'][1]+oy,n['box'][2]+ox,n['box'][3]+oy],component=m['component'],component_head_count=len(comp['note_indices']) if comp else 0))
 return mapping,im,notes,dict(mapping_file=str(mapping_file),mapping_sha256=sha(mapping_file),shapes_file=str(shapes_path),shapes_sha256=sha(shapes_path),components_file=str(components_path),components_sha256=sha(components_path),input_sha256=sha(input_image),models=components['models'],providers=profiles)

def printed_lines(im,left,top,width,height,space):
 margin=round(2*space);x0=max(0,left-margin);y0=max(0,top-2);x1=min(im.shape[1],left+width+margin);y1=min(im.shape[0],top+height+2);region=(im[y0:y1,x0:x1]<180).astype('uint8');horizontal=cv2.morphologyEx(region,cv2.MORPH_OPEN,np.ones((1,round(1.8*space)),np.uint8));return horizontal[top-y0:top-y0+height,left-x0:left-x0+width].astype(bool)

def compare(patch,reference,mask):
 scores=[]
 if patch.shape!=reference.shape or mask.mean()<.5:return None
 for threshold in [160,208]:
  aa=(patch<threshold)[mask];bb=(reference<threshold)[mask]
  if aa.std()<.05 or bb.std()<.05:return None
  hit=np.logical_and(aa,bb).sum();scores.append(dict(threshold=threshold,ncc=float(np.corrcoef(aa.astype(float),bb.astype(float))[0,1]),precision=float(hit/max(1,aa.sum())),recall=float(hit/max(1,bb.sum()))))
 return dict(quality=min(min(v['ncc'],v['precision'],v['recall']) for v in scores),scores=scores,visible_mask_fraction=float(mask.mean()))

def scan(book,mapping_file,out):
 book=Path(book);out=Path(out);out.mkdir(exist_ok=False);mapping,im,nn,witness=load_witness(mapping_file);unit=int(Path(mapping['source']).stem.split('-')[-1]);rect=mapping['crop'];source_hash=mapping['source_sha256'];z=zipfile.ZipFile(book);root=E.fromstring(z.read(f'sheet#{unit}/sheet#{unit}.xml'));staff_candidates=[]
 for sy in root.findall('page/system'):
  for st in sy.findall('part/staff'):
   top,bottom,space=staff_info(st);points=st.findall('lines/line/point');left=min(float(p.get('x')) for p in points);right=max(float(p.get('x')) for p in points)
   if rect[1]<=top and bottom<=rect[3] and max(0,min(rect[2],right)-max(rect[0],left))>=.9*(right-left):staff_candidates.append((sy,st))
 assert len(staff_candidates)==1,'crop must contain one uniquely mapped original native staff';sy,st=staff_candidates[0];top,bottom,space=staff_info(st);sid=sy.get('id');staff=st.get('id');nodes={n.get('id'):n for n in sy.find('sig/inters')};rels=list(sy.find('sig/relations'));cs={q.get('source'):nodes[q.get('target')] for q in rels if q.find('chord-stem') is not None};headchord={q.get('target'):nodes[q.get('source')] for q in rels if q.find('containment') is not None and nodes[q.get('source')].tag=='head-chord'};heads=[n for n in nodes.values() if n.tag=='head' and n.get('staff')==staff];matches={};offsets=[]
 for h in heads:
  hx,hy=center(box(h));near=sorted((math.dist((hx,hy),n['center']),n['index'],n) for n in nn)
  if near and near[0][0]<.35*space and (len(near)<2 or near[1][0]>.6*space):matches[h.get('id')]=near[0][2];offsets.append(round(float(h.get('pitch')))+near[0][2]['position'])
 assert len(offsets)>=8,'insufficient independent native/source head quorum';pitch_offset,count=Counter(offsets).most_common(1)[0];assert count>=.95*len(offsets),'independent source diatonic grid conflict';bank=[]
 for h in heads:
  cid=headchord.get(h.get('id'));stem=cs.get(cid.get('id')) if cid is not None else None
  if h.get('id') not in matches or h.get('shape')!='NOTEHEAD_BLACK' or float(h.get('ctx-grade',h.get('grade','0')))<.9 or stem is None:continue
  b=box(h);cx,cy=center(b);sx=sum(float(p.get('x')) for p in stem.findall('median/*'))/2
  if not (1.1*space<=b[2]<=1.7*space and .8*space<=b[3]<=1.3*space):continue
  bank.append(dict(head=h.get('id'),chord=cid.get('id'),glyph=h.get('glyph'),bounds=b,stem_x=sx,pixels=im[b[1]:b[1]+b[3],b[0]:b[0]+b[2]].copy(),native_pitch=float(h.get('pitch')),NN_index=matches[h.get('id')]['index']))
 findings=[];pending=[]
 for n in nn:
  if min((math.dist(center(box(h)),n['center']) for h in heads),default=0)<=.7*space:continue
  rec=dict(unit=unit,system=sid,staff=staff,source_sha256=source_hash,witness=n);cx,cy=n['center'];grid=(cy-(top+bottom)/2)*2/space;expected_pitch=pitch_offset-n['position']
  if n['component_head_count']!=1 or abs(grid-expected_pitch)>.25:pending.append(dict(rec,reason='source_component_or_pitch_grid_uncertain'));continue
  choices=[]
  for cid,stem in cs.items():
   chord=nodes[cid]
   if chord.get('staff')!=staff:continue
   members=[h for h in heads if headchord.get(h.get('id')) is chord]
   if len(members)<2 or any(h.get('shape')!='NOTEHEAD_BLACK' for h in members):continue
   if sum(h.get('id') in matches for h in members)<2:continue
   mx=float(np.median([center(box(h))[0] for h in members]));ys=[center(box(h))[1] for h in members]
   if abs(mx-cx)>.35*space or not (min(ys)-2.5*space<=cy<min(ys)-.65*space or max(ys)+.65*space<cy<=max(ys)+2.5*space):continue
   ends=[(float(p.get('x')),float(p.get('y'))) for p in stem.findall('median/*')];a,b=sorted(ends,key=lambda v:v[1]);above=cy<min(ys);old_head=min(members,key=lambda h:center(box(h))[1]) if above else max(members,key=lambda h:center(box(h))[1]);hc=center(box(old_head));oldend=a if above else b;dy=oldend[1]-hc[1]
   if abs(dy)>.65*space:continue
   newy=cy+dy;newx=a[0]+(newy-a[1])*(b[0]-a[0])/(b[1]-a[1]);scores=[]
   yy=np.arange(math.ceil(min(oldend[1],newy)),math.floor(max(oldend[1],newy))+1);xx=np.rint(a[0]+(yy-a[1])*(b[0]-a[0])/(b[1]-a[1])).astype(int)
   if len(yy)<.5*space:continue
   for threshold in (160,208):
    coverage=np.array([bool((im[y,max(0,x-1):x+2]<threshold).any()) for x,y in zip(xx,yy)]);maxgap=max((len(v) for v in ''.join('1' if x else '0' for x in coverage).split('1')),default=0);scores.append(dict(threshold=threshold,rows=len(yy),covered_rows=int(coverage.sum()),coverage=float(coverage.mean()),max_white_gap=maxgap))
   if any(v['coverage']<.98 or v['max_white_gap']>1 for v in scores):continue
   choices.append(dict(chord=cid,stem=stem.get('id'),old_stem_bounds=box(stem),old_median=[*a,*b],old_extreme_head=old_head.get('id'),stem_side='left' if oldend[0]<hc[0] else 'right',end='top' if above else 'bottom',new_endpoint=[newx,newy],source_stem_ink=scores,member_heads=[h.get('id') for h in members],font_matches=[]))
  if len(choices)!=1:pending.append(dict(rec,reason='no_unique_source_ink_connected_chord',choices=len(choices)));continue
  choice=choices[0]
  for ref in bank:
   if ref['chord']==choice['chord'] or round(ref['native_pitch'])%2!=expected_pitch%2:continue
   ref_side='left' if ref['stem_x']<center(ref['bounds'])[0] else 'right'
   if ref_side!=choice['stem_side']:continue
   rw,rh=ref['bounds'][2:];px=round(cx-rw/2);py=round(cy-rh/2);best=None
   for dx in range(-2,3):
    for dy in range(-2,3):
     left,yy0=px+dx,py+dy;patch=im[yy0:yy0+rh,left:left+rw]
     if patch.shape!=ref['pixels'].shape:continue
     # Mask only actual long horizontal ink, not imaginary ledger lines on the staff grid.
     target_lines=printed_lines(im,left,yy0,rw,rh,space);reference_lines=printed_lines(im,ref['bounds'][0],ref['bounds'][1],rw,rh,space)
     mask=~(target_lines|reference_lines)
     target_sx=choice['old_median'][0];tx=round(target_sx-left);rx=round(ref['stem_x']-ref['bounds'][0])
     for x in [tx,rx]:mask[:,max(0,x-2):max(0,min(rw,x+3))]=False
     quality=compare(patch,ref['pixels'],mask)
     if quality and (best is None or quality['quality']>best['quality']):best=dict(**quality,head_bounds=[left,yy0,rw,rh],reference={k:v for k,v in ref.items() if k!='pixels'})
   if best is not None:choice['font_matches'].append(best)
  choice['font_matches'].sort(key=lambda v:v['quality'],reverse=True);supported=[v for v in choice['font_matches'] if v['quality']>=.95]
  rec.update(choice,expected_native_pitch=expected_pitch,font_matches=choice['font_matches'][:8],supported_font_matches=supported)
  if len({v['reference']['chord'] for v in supported})<2:pending.append(dict(rec,reason='independent_source_black_head_font_insufficient'));continue
  # Require independent reference agreement on the exact physical head bounds.
  bound_counts=Counter(tuple(v['head_bounds']) for v in supported);bounds,num=bound_counts.most_common(1)[0];agree=[v for v in supported if tuple(v['head_bounds'])==bounds]
  if len({v['reference']['chord'] for v in agree})<2:pending.append(dict(rec,reason='font_references_disagree_on_source_head_box'));continue
  rec.update(state='source_supported_head_candidate',head_bounds=list(bounds),supporting_references=agree);findings.append(rec)
 report=dict(algorithm='original-pixels-plus-independent-local-head-component-and-native-stem-v1',scope='Source evidence candidates only; no note/meter/voice copying from HOMR',source_book=str(book),source_book_sha256=sha(book),source_image=str(mapping['source']),source_image_sha256=source_hash,witness=witness,unit=unit,system=sid,staff=staff,independent_existing_head_matches=len(matches),native_NN_diatonic_offset=pitch_offset,source_font_reference_count=len(bank),findings=findings,pending=pending)
 (out/'source-head-evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');print('SOURCE_HEAD_EVIDENCE',len(matches),'matched native peers',len(bank),'font references',len(findings),'supported',len(pending),'pending');print('SUPPORTED',[(v['chord'],v['expected_native_pitch'],v['head_bounds'],len(v['supporting_references'])) for v in findings]);print('PENDING',[(v['witness']['index'],v['reason'],[(round(m['quality'],4),m['head_bounds']) for m in v.get('font_matches',[])[:2]]) for v in pending]);return report
if __name__=='__main__':
 import sys
 scan(*sys.argv[1:])