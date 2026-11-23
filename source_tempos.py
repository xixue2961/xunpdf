"""Source-pixel beat-unit proposals; native tempo records are not ground truth."""
from pathlib import Path
import sys,json,hashlib,zipfile,xml.etree.ElementTree as E
import cv2,numpy as np

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def extract_symbol(im,b):
 x,y,w,h=b;size=max(h,12);height,width=im.shape
 x0=max(0,round(x-2*size));y0=max(0,round(y-.8*size));x1=min(width,round(x+w+.3*size));y1=min(height,round(y+h+.3*size))
 patch=im[y0:y1,x0:x1]
 _,_,stats,_=cv2.connectedComponentsWithStats((patch<160).astype('uint8'))
 bars=[(int(xx+x0),int(yy+y0),int(ww),int(hh)) for xx,yy,ww,hh,area in stats[1:] if ww>=max(4,.2*size) and ww/hh>=3 and hh<=max(2,.2*size)]
 pairs=[(a,c) for a in bars for c in bars if max(1.5*a[3],.2*a[2])<=c[1]-a[1]<=max(4*a[3],.9*a[2]) and abs(a[0]-c[0])<=max(1,.05*a[2]) and abs(a[2]-c[2])<=max(1,.05*a[2])]
 if len(pairs)!=1:return None,'ambiguous_or_missing_source_equals'
 (ex,ey,ew,eh),second=pairs[0];sx=max(0,round(ex-4*ew));sy=max(0,round(ey-3*ew));patch=im[sy:min(height,round(ey+1.5*ew)),sx:ex-1]
 _,labels,stats,_=cv2.connectedComponentsWithStats((patch<180).astype('uint8'));candidates=[]
 for index,(xx,yy,ww,hh,area) in enumerate(stats[1:],1):
  gap=ex-(sx+xx+ww)
  if hh>=1.2*ew and ww>=.3*ew and hh>=1.3*ww and 0<=gap<=2.5*ew:candidates.append((gap,index))
 if not candidates:return None,'no_complete_tall_beat_symbol'
 candidates.sort();index=candidates[0][1];xx,yy,ww,hh,area=map(int,stats[index]);selected=[index]
 # Every eligible lower-right component is retained, including augmentation dots.
 for j,(dx,dy,dw,dh,ar) in enumerate(stats[1:],1):
  if j==index:continue
  if dx>=xx+ww and yy+.55*hh<=dy+dh/2<=yy+1.15*hh:
   if ar>=3 and dw<=.5*hh and dh<=.5*hh:selected.append(j)
 mx=min(int(stats[j,0]) for j in selected);my=min(int(stats[j,1]) for j in selected);right=max(int(stats[j,0]+stats[j,2]) for j in selected);bottom=max(int(stats[j,1]+stats[j,3]) for j in selected)
 if xx<=0 or yy<=0 or xx+ww>=patch.shape[1] or yy+hh>=patch.shape[0]:return None,'source_symbol_touches_search_boundary'
 mask=np.isin(labels,selected).astype('uint8');image=cv2.copyMakeBorder(255*(1-mask[my:bottom,mx:right]),2,2,2,2,cv2.BORDER_CONSTANT,value=255)
 return dict(image=image,bounds=[sx+mx,sy+my,right-mx,bottom-my],equals=[list(pairs[0][0]),list(second)],components=len(selected)),None

def scan(job,out):
 job=Path(job).resolve();out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True);native=job/'raw/input.omr';report=dict(version=323,source_job=str(job),native_sha256=sha(native),findings=[],scope='Only native MetronomeInter records; source marks missed entirely by the engine remain unassessed');plans=[];cache={}
 with zipfile.ZipFile(native) as archive:
  for file in archive.namelist():
   if not file.endswith('.xml') or not file.startswith('sheet#'):continue
   unit=int(file.split('/')[0].split('#')[1]);root=E.fromstring(archive.read(file));source=job/f'unit-{unit:03d}.png'
   for system in root.findall('.//page/system'):
    nodes={n.get('id'):n for n in system.iter() if n.get('shape')};relations=list(system.iter('relation'))
    for metro in system.iter('metronome'):
     if metro.get('shape')!='METRONOME':continue
     b=metro.find('bounds');box=[int(b.get(k)) for k in ['x','y','w','h']];members=sorted([e.get('target') for e in relations if e.get('source')==metro.get('id') and e.find('containment') is not None],key=int)
     finding=dict(unit=unit,system=int(system.get('id')),id=int(metro.get('id')),staff=int(metro.get('staff')),native_bounds=box,members=members,existing_beat_units=[dict(id=ident,shape=nodes[ident].get('shape')) for ident in members if ident in nodes and nodes[ident].tag=='beat-unit'])
     ordered=sorted((nodes[i] for i in members if i in nodes and nodes[i].tag=='word'),key=lambda n:int(n.find('bounds').get('x')))
     finding['source_word_values']=[n.get('value','') for n in ordered]
     if not source.is_file():finding.update(state='deferred',reason='source_image_missing');report['findings'].append(finding);continue
     if unit not in cache:cache[unit]=(cv2.imdecode(np.frombuffer(source.read_bytes(),np.uint8),0),sha(source))
     im,digest=cache[unit];symbol,reason=extract_symbol(im,box)
     if symbol is None:finding.update(state='deferred',reason=reason);report['findings'].append(finding);continue
     crop=out/f'u{unit}-s{system.get("id")}-m{metro.get("id")}.png';crop.write_bytes(cv2.imencode('.png',symbol.pop('image'))[1].tobytes());finding.update(state='source_symbol_proposed',**symbol,source_sha256=digest,crop_sha256=sha(crop),crop=str(crop));report['findings'].append(finding)
     fields=[unit,system.get('id'),metro.get('id'),metro.get('staff'),*box,','.join(members),*symbol['bounds'],str(crop),sha(crop),str(source),digest];plans.append('\t'.join(map(str,fields)))
 (out/'plan.tsv').write_text('\n'.join(plans),encoding='utf8');(out/'scan.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return report
def validate_plan(job,out):
 """Reconstruct evidence from authoritative PNG/native inputs; do not trust cached crop hashes alone."""
 job=Path(job).resolve();out=Path(out).resolve();metadata=json.loads((out/'scan.json').read_text(encoding='utf8'));native=job/'raw/input.omr'
 if metadata['source_job']!=str(job) or sha(native)!=metadata['native_sha256']:raise ValueError('native archive provenance changed')
 rows=[];targets=set();sheets={};images={}
 with zipfile.ZipFile(native) as archive:
  for line in (out/'plan.tsv').read_text(encoding='utf8').splitlines():
   f=line.split('\t')
   if len(f)!=17:raise ValueError('plan width changed')
   unit,system,ident,staff=map(int,f[:4]);key=(unit,system,ident)
   if key in targets:raise ValueError('duplicate native target')
   targets.add(key)
   if unit not in sheets:sheets[unit]=E.fromstring(archive.read(f'sheet#{unit}/sheet#{unit}.xml'))
   systems=[s for s in sheets[unit].findall('.//page/system') if s.get('id')==str(system)]
   if len(systems)!=1:raise ValueError('native system identity changed')
   sys=systems[0];nodes=[n for n in sys.iter('metronome') if n.get('id')==str(ident) and n.get('shape')=='METRONOME']
   if len(nodes)!=1:raise ValueError('native target not a unique metronome')
   node=nodes[0];box=[int(node.find('bounds').get(k)) for k in ['x','y','w','h']]
   members=sorted([e.get('target') for e in sys.iter('relation') if e.get('source')==str(ident) and e.find('containment') is not None],key=int)
   if int(node.get('staff'))!=staff or box!=list(map(int,f[4:8])) or ','.join(members)!=f[8]:raise ValueError('native target identity changed')
   all_nodes={n.get('id'):n for n in sys.iter() if n.get('shape')}
   words=sorted((all_nodes[i] for i in members if i in all_nodes and all_nodes[i].tag=='word'),key=lambda n:int(n.find('bounds').get('x')))
   recorded=[v for v in metadata['findings'] if (v['unit'],v['system'],v['id'])==key]
   if len(recorded)!=1 or recorded[0].get('source_word_values')!=[n.get('value','') for n in words]:raise ValueError('native tempo text evidence changed')
   crop=Path(f[13]).resolve();source=job/f'unit-{unit:03d}.png'
   if crop.parent!=out or Path(f[15]).resolve()!=source:raise ValueError('source/crop path provenance changed')
   if sha(crop)!=f[14] or sha(source)!=f[16]:raise ValueError('source/crop SHA changed')
   if unit not in images:images[unit]=cv2.imdecode(np.frombuffer(source.read_bytes(),np.uint8),0)
   symbol,reason=extract_symbol(images[unit],box)
   if symbol is None:raise ValueError('source symbol no longer supported: '+reason)
   if symbol['bounds']!=list(map(int,f[9:13])):raise ValueError('source symbol bounds changed')
   expected=cv2.imencode('.png',symbol['image'])[1].tobytes()
   if hashlib.sha256(expected).hexdigest()!=f[14]:raise ValueError('source symbol content changed')
   rows.append(f)
 return dict(validated_rows=len(rows),native_sha256=metadata['native_sha256'])

if __name__=='__main__':
 if len(sys.argv)>3 and sys.argv[3]=='--validate':print(json.dumps(validate_plan(sys.argv[1],sys.argv[2])))
 else:
  report=scan(sys.argv[1],sys.argv[2]);print(json.dumps(dict(records=len(report['findings']),proposed=sum(x['state']=='source_symbol_proposed' for x in report['findings'])),ensure_ascii=False))
