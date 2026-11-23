"""Bounded multi-view offline head segmentation, validated against native source anchors.
Every view consists solely of unchanged original pixels. No threshold is relaxed to obtain a note.
"""
from pathlib import Path
import json,hashlib,zipfile,xml.etree.ElementTree as E,math
from collections import Counter

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def geometry(st):
 points=st.findall('lines/line/point');ys=[float(p.get('y')) for p in points];xs=[float(p.get('x')) for p in points];return min(xs),min(ys),max(xs),max(ys),(max(ys)-min(ys))/4

def alignment_quality(nodes,relations,staff,space,shapes,crop):
 nn=[dict(center=[n['center'][0]+crop[0],n['center'][1]+crop[1]],position=n['staff_position']) for n in shapes['notes']];heads=[n for n in nodes.values() if n.tag=='head' and n.get('staff')==staff];matched={};offsets=[]
 for h in heads:
  b=h.find('bounds');center=(int(b.get('x'))+int(b.get('w'))/2,int(b.get('y'))+int(b.get('h'))/2);near=sorted((math.dist(center,n['center']),i,n) for i,n in enumerate(nn))
  if near and near[0][0]<.35*space and (len(near)<2 or near[1][0]>.6*space):matched[h.get('id')]=near[0][2];offsets.append(round(float(h.get('pitch')))+near[0][2]['position'])
 count=Counter(offsets);offset,n=count.most_common(1)[0] if count else (None,0);weak=[];dense=0
 for c in nodes.values():
  if c.tag!='head-chord' or c.get('staff')!=staff:continue
  members=[nodes[q.get('target')] for q in relations if q.get('source')==c.get('id') and q.find('containment') is not None]
  if len(members)<2 or not all(h.tag=='head' and h.get('shape')=='NOTEHEAD_BLACK' for h in members):continue
  dense+=1
  if sum(h.get('id') in matched for h in members)<2:weak.append(c.get('id'))
 return dict(qualified=len(offsets)>=8 and n>=.95*len(offsets),native_heads=len(heads),matched_native_heads=len(matched),diatonic_offset=offset,diatonic_agreement=n/max(1,len(offsets)),dense_chords=dense,weak_dense_chords=weak)

def build_head_witnesses(book,source,output,settings,device,env,cancel,run,emit=print,only_staves=None):
 from PIL import Image
 from runtime_resources import MemoryBudgetError,check_memory,wait_for_released_memory
 book=Path(book);source=Path(source);output=Path(output);output.mkdir(parents=True,exist_ok=False);app=Path(__file__).resolve().parent;configured=settings.get('source_head_runtime');runtime=Path(configured) if configured else app
 if not runtime.is_absolute():runtime=app/runtime
 python=runtime/'runtime/homr-python/python.exe';worker=runtime/'homr_worker.py';report=dict(state='preparing',runtime=str(runtime),requested_device=device,witnesses=[],attempts=[],pending=[],eligible_staves=0,skipped_staves=0,scope='Unchanged original-pixel local geometry, source-anchor-qualified multiple views; no transcription or whole-score head-completeness claim')
 def finish():
  (output/'witnesses.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return [Path(v['mapping']) for v in report['witnesses']],report
 if not python.is_file() or not worker.is_file():report.update(state='runtime_unavailable',pending=[dict(reason='local_offline_head_segmentation_runtime_unavailable')]);return finish()
 tasks=[]
 with zipfile.ZipFile(book) as z:
  for name in z.namelist():
   if not name.startswith('sheet#') or not name.endswith('.xml'):continue
   unit=int(name.split('/')[0][6:]);root=E.fromstring(z.read(name))
   for sy in root.findall('page/system'):
    nodes={n.get('id'):n for n in sy.find('sig/inters')};relations=list(sy.find('sig/relations'));all_staffs=sorted(sy.findall('part/staff'),key=lambda st:geometry(st)[1]);wanted=[st for p in sy.findall('part') if len(p.findall('staff'))==2 for st in p.findall('staff')]
    for st in wanted:
     if only_staves is not None and (unit,sy.get('id'),st.get('id')) not in only_staves:continue
     dense=[]
     for c in nodes.values():
      if c.tag!='head-chord' or c.get('staff')!=st.get('id'):continue
      members=[nodes[q.get('target')] for q in relations if q.get('source')==c.get('id') and q.find('containment') is not None]
      if len(members)>=2 and all(h.tag=='head' and h.get('shape')=='NOTEHEAD_BLACK' for h in members):dense.append(c)
     if not dense:report['skipped_staves']+=1;continue
     left,top,right,bottom,space=geometry(st);i=all_staffs.index(st);heads=[n for n in nodes.values() if n.tag=='head' and n.get('staff')==st.get('id')];hy=[int(h.find('bounds').get('y')) for h in heads];hb=[int(h.find('bounds').get('y'))+int(h.find('bounds').get('h')) for h in heads];variants=[]
     # Vary the sampling phase and surrounding white space rather than changing
     # source ink, confidence thresholds, or native ownership requirements.
     for above,below,leftpad,rightpad in [(10,6,.2,.2),(9.5,6,.2,.15),(9.75,5.75,.15,.2),(10,5.5,.15,.15)]:
      y0=min(top-above*space,min(hy,default=top)-2*space);y1=max(bottom+below*space,max(hb,default=bottom)+2*space)
      if i:y0=max(y0,geometry(all_staffs[i-1])[3]+.5*space)
      if i+1<len(all_staffs):y1=min(y1,geometry(all_staffs[i+1])[1]-.5*space)
      crop=[int(left)-round(leftpad*space),int(y0),int(right)+round(rightpad*space),int(y1)]
      if crop not in variants:variants.append(crop)
     tasks.append(dict(unit=unit,system=sy.get('id'),staff=st.get('id'),native_dense_chords=len(dense),variants=variants,nodes=nodes,relations=relations,space=space))
 report['eligible_staves']=len(tasks);report['state']='running';finish();cache=output/'cache';cache.mkdir();worker_hash=sha(worker)
 memory_deferred=False
 def defer_memory(error,task,variant):
  report['pending'].append(dict(unit=task['unit'],system=task['system'],staff=task['staff'],
      variant=variant,reason='local_head_memory_deferred',detail=str(error)))
 for seq,t in enumerate(tasks,1):
  good=[];attempted=set();image=source/f"unit-{t['unit']:03d}.png";digest=sha(image)
  for variant,rawbox in enumerate(t['variants'],1):
   if cancel.is_set():raise InterruptedError('已取消原图音头复核')
   try:
    check_memory(output,f'local-head-geometry-{seq}-{variant}')
   except MemoryBudgetError:
    try:
     wait_for_released_memory(output,cancel,emit,phase='head_witness_before_worker')
    except MemoryBudgetError as exc:
     defer_memory(exc,t,variant)
     memory_deferred=True
     break
   with Image.open(image) as im:
    box=[max(0,rawbox[0]),max(0,rawbox[1]),min(im.width,rawbox[2]),min(im.height,rawbox[3])]
    if tuple(box) in attempted:continue
    attempted.add(tuple(box));key=hashlib.sha256(json.dumps(dict(source=digest,crop=box,worker=worker_hash,device=device),sort_keys=True).encode()).hexdigest()[:24];folder=cache/key;folder.mkdir(exist_ok=False);cropped=folder/'staff-source.png';im.crop(box).save(cropped)
   mapping=folder/'source-map.json';mapping.write_text(json.dumps(dict(source=str(image.resolve()),source_sha256=digest,crop=box,input_sha256=sha(cropped)),indent=2),encoding='utf8');emit('核对钢琴原图音头 '+str(seq)+'/'+str(len(tasks))+'，视野'+str(variant));args=[python,'-I','-B',worker,'--geometry-only']
   if device=='cpu':args.append('--geometry-cpu')
   args.append(cropped);localenv=dict(env);localenv['XUN_OMR_TEMP']=env.get('TEMP',str(output/'temp'));entry=dict(unit=t['unit'],system=t['system'],staff=t['staff'],variant=variant,crop=box,mapping=str(mapping))
   try:
    run(args,folder/'worker.log',localenv,cancel,timeout=180,emit=emit);providers=json.loads(cropped.with_suffix('.providers.json').read_text(encoding='utf8'));cuda=sum(v['nodes'].get('CUDAExecutionProvider',0) for v in providers)
    if device=='gpu' and cuda<=0:raise ValueError('GPU requested but local head model did not execute on CUDA')
    if json.loads(cropped.with_suffix('.offline.json').read_text())!=dict(network_blocked=True,attempts=[]):raise ValueError('local geometry worker offline proof failed')
    if json.loads(cropped.with_suffix('.runtime.json').read_text())['external_module_files']:raise ValueError('local geometry worker loaded external modules')
    shapes=json.loads(cropped.with_suffix('.segmentation-shapes.json').read_text(encoding='utf8'));quality=alignment_quality(t['nodes'],t['relations'],t['staff'],t['space'],shapes,box);entry.update(actual_cuda_nodes=cuda,device=device,quality=quality,state='qualified' if quality['qualified'] else 'native_anchor_disagreement');report['attempts'].append(entry)
    if quality['qualified']:good.append(dict(**entry,mapping_sha256=sha(mapping)))
   except InterruptedError:raise
   except Exception as exc:entry.update(state='unresolved',detail=str(exc));report['attempts'].append(entry)
   finish()
   try:
    wait_for_released_memory(output,cancel,emit,phase='head_witness_after_worker')
   except MemoryBudgetError as exc:
    defer_memory(exc,t,variant)
    memory_deferred=True
    break
   if len(good)>=2 and any(not v['quality']['weak_dense_chords'] for v in good):break
  if good:
   good.sort(key=lambda v:(len(v['quality']['weak_dense_chords']),-v['quality']['matched_native_heads']));report['witnesses']+=good
   if all(v['quality']['weak_dense_chords'] for v in good):report['pending'].append(dict(unit=t['unit'],system=t['system'],staff=t['staff'],reason='some_native_dense_chords_still_lack_two_independent_head_anchors'))
  elif not memory_deferred:
   report['pending'].append(dict(unit=t['unit'],system=t['system'],staff=t['staff'],reason='local_head_geometry_source_anchor_quorum_unresolved'))
  if memory_deferred:
   report['memory_deferred_staves']=[dict(unit=u['unit'],system=u['system'],staff=u['staff']) for u in tasks[seq:]]
   report['state']='partially_verified' if report['witnesses'] else 'memory_deferred'
   if emit:
    emit('钢琴音头复核暂受内存限制，已通过原图验证的视野会继续使用')
   return finish()
  finish()
 report['state']='partially_verified' if report['pending'] else 'verified_local_geometry';return finish()