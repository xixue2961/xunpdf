"""Source-backed header-clef recovery. Original XML is the output base.
Only verified octave values and missing G8vb clefs are transferred from native re-export.
"""
from pathlib import Path
import xml.etree.ElementTree as E
import copy,json,hashlib,shutil,os


def _canonical(e):
 return (e.tag,tuple(sorted(e.attrib.items())),(e.text or '').strip(),tuple(_canonical(c) for c in e))

def _without_octave(note):
 n=copy.deepcopy(note)
 for path in ('pitch/octave','rest/display-octave'):
  child=n.find(path)
  if child is not None:n.find(path.split('/')[0]).remove(child)
 return _canonical(n)

def _clef_signature(clef):
 return (clef.get('number','1'),clef.findtext('sign'),clef.findtext('line'),clef.findtext('clef-octave-change','0'))

def merge_verified_clef_export(original,refined):
 """Transfer no directions, lyrics, rhythm, instrument data, or layout from re-export."""
 result=copy.deepcopy(original);old_parts=original.findall('part');new_parts=refined.findall('part');parts=result.findall('part');changes=[];added=[]
 if len(old_parts)!=len(new_parts):raise ValueError('native re-export changed part count')
 for old,new,dest in zip(old_parts,new_parts,parts):
  if old.get('id')!=new.get('id'):raise ValueError('native re-export changed part identity')
  om=old.findall('measure');nm=new.findall('measure');dm=dest.findall('measure')
  if len(om)!=len(nm):raise ValueError('native re-export changed measure count')
  for a,b,c in zip(om,nm,dm):
   if a.get('number')!=b.get('number'):raise ValueError('native re-export changed measure identity')
   aa=[e for e in a if e.tag in ('note','backup','forward')];bb=[e for e in b if e.tag in ('note','backup','forward')];cc=[e for e in c if e.tag in ('note','backup','forward')]
   if len(aa)!=len(bb):raise ValueError('native re-export changed note/rhythm cursor count')
   for x,y,z in zip(aa,bb,cc):
    if x.tag!=y.tag:raise ValueError('native re-export changed rhythm cursor order')
    if x.tag!='note':
     if _canonical(x)!=_canonical(y):raise ValueError('native re-export changed a rhythm cursor')
     continue
    if _without_octave(x)!=_without_octave(y):raise ValueError('native re-export changed non-octave note content')
    for path in ('pitch/octave','rest/display-octave'):
     ov=x.findtext(path);nv=y.findtext(path)
     if (ov is None)!=(nv is None):raise ValueError('native re-export has incomplete pitch context')
     if ov is None:continue
     delta=int(nv)-int(ov)
     if delta not in (0,-1):raise ValueError('unexpected octave change for recovered G8vb clef')
     if delta:
      z.find(path).text=nv;changes.append({'part':old.get('id'),'measure':a.get('number'),'kind':path,'old':ov,'new':nv})
   original_clefs=[_clef_signature(e) for e in a.findall('attributes/clef')]
   for attr in b.findall('attributes'):
    for clef in attr.findall('clef'):
     signature=_clef_signature(clef)
     if signature in original_clefs:continue
     if signature[1:]!=('G','2','-1'):raise ValueError('native re-export introduced unsupported clef')
     if any(t[0]==signature[0] for t in original_clefs):raise ValueError('native re-export conflicts with existing measure clef')
     # Only zero-time header additions are supported; a mid-measure clef is not moved.
     if any(e.tag in ('note','backup','forward') for e in list(b)[:list(b).index(attr)]):raise ValueError('native re-export introduced a mid-measure clef')
     attrs=c.find('attributes')
     if attrs is None:
      attrs=E.Element('attributes');index=next((i for i,e in enumerate(c) if e.tag!='print'),len(c));c.insert(index,attrs)
     index=next((i for i,e in enumerate(attrs) if e.tag in ('staff-details','transpose','directive','measure-style')),len(attrs));attrs.insert(index,copy.deepcopy(clef));original_clefs.append(signature);added.append({'part':old.get('id'),'measure':a.get('number'),'signature':signature})
 return result,{'octave_changes':changes,'clefs_added':added,'all_original_directions_lyrics_rhythm_and_layout_preserved':True}


def refine_source_clefs(root,job,settings,device,parts_hint,env,cancel,runner,emit=print,source_job=None):
 job=Path(job);source=Path(source_job) if source_job is not None else job;base=Path(__file__).resolve().parent;report={'state':'not_assessed','scope':'missing G_CLEF_8VB headers with same-book printed source evidence','candidates':0,'report':'source-context.json'}
 def finish(tree):
  (job/'source-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8');return tree,report
 try:
  native=source/'raw/input.omr';scanner=base/'source_clefs.py';classes=base/'context-runtime'
  if not native.is_file() or not scanner.is_file() or not (classes/'ClefExport307.class').is_file():
   report['reason']='source context runtime or original native book unavailable';return finish(root)
  engine=Path(settings['engine_root']);python=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe'
  if not python.is_file():report['reason']='local image analysis Python unavailable';return finish(root)
  emit('核对原图中的八度高音谱号')
  runner([str(python),str(scanner),str(source),str(job/'source-clefs.json')],job/'source-clefs-scan.log',env,cancel,timeout=300,emit=emit)
  scan=json.loads((job/'source-clefs.json').read_text('utf8'));findings=[f for f in scan['findings'] if f['status']=='missing_clef_candidate'];report['templates']=scan['templates'];report['candidates']=len(findings);report['bootstrap_body_templates']=scan.get('bootstrap_body_templates',0);report['bootstrap_pending']=scan.get('bootstrap_pending',[])
  if not findings:
   report['state']='no_missing_clef_candidates' if scan['templates'] else 'no_source_templates';return finish(root)
  bootstrap=any(f.get('evidence_mode')=='plain_g_body_and_repeated_printed_eight' for f in findings)
  if bootstrap and (not all((classes/name).is_file() for name in ('ClefExport365.class','ClefExport365$Region.class')) or not all(f.get('evidence_mode')=='plain_g_body_and_repeated_printed_eight' for f in findings)):raise ValueError('source clef bootstrap runtime or mode unavailable')
  plan=[]
  for f in findings:
   t=f['template']
   if Path(t['job']).resolve()!=source.resolve() or f['shape']!='G_CLEF_8VB' or f['footer_holes']<2 or f['match_score']<.84 or f['footer_score']<.8:raise ValueError('source clef evidence incomplete')
   image=source/f'unit-{f["unit"]:03d}.png'
   if hashlib.sha256(image.read_bytes()).hexdigest()!=f['source_sha256']:raise ValueError('source image changed during verification')
   values=[f['unit'],f['system'],f['staff'],*f['source_bounds'],t['unit'],t['system'],t['id'],f['shape']]
   if bootstrap:
    peer=f['peer'];g=f['octave_eight_geometry']
    if t.get('native_shape')!='G_CLEF' or f['match_score']<.95 or f['footer_score']<.95 or g['left_waist_inset']<.05*f['space'] or g['right_waist_inset']<.05*f['space']:raise ValueError('printed eight geometry proof incomplete')
    if hashlib.sha256((source/f"unit-{peer['unit']:03d}.png").read_bytes()).hexdigest()!=peer['source_sha256'] or hashlib.sha256((source/f"unit-{t['unit']:03d}.png").read_bytes()).hexdigest()!=t['source_sha256']:raise ValueError('peer or donor source pixels changed')
    values.extend([*f['body_bounds'],*f['footer_bounds'],f['source_sha256'],peer['unit'],*peer['footer_bounds'],peer['source_sha256'],f['match_score'],f['footer_score'],t['source_sha256'],f['evidence_mode']])
   plan.append('\t'.join(map(str,values)))
  (job/'source-clef-plan.tsv').write_text('\n'.join(plan)+'\n','utf8');folder=job/'source-context-native';folder.mkdir(exist_ok=True);original_hash=hashlib.sha256(native.read_bytes()).hexdigest();shutil.copy2(native,folder/'input.omr')
  classpath=[str(classes)];prefix=settings.get('engine_classpath_prefix')
  if prefix:
   p=Path(prefix);classpath.append(str(p if p.is_absolute() else base/p))
  classpath.append(str(engine/'Audiveris-Bandscore/lib/*'));java=engine/settings['java_relative']/'bin/java.exe'
  args=[str(java),'-Xmx1536m','-Djava.awt.headless=true','--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED','-cp',os.pathsep.join(classpath),'ClefExport365' if bootstrap else 'ClefExport307',str(folder/'input.omr'),parts_hint,str(job/'source-clef-plan.tsv'),str(job/'source-clef-native.tsv')]
  if bootstrap:args.append(str(source))
  runner(args,job/'source-clefs-native.log',env,cancel,timeout=300,emit=emit)
  if hashlib.sha256(native.read_bytes()).hexdigest()!=original_hash:raise ValueError('original native book changed')
  import zipfile
  with zipfile.ZipFile(folder/'input.mxl') as archive:
   container=E.fromstring(archive.read('META-INF/container.xml'));entry=next(e.get('full-path') for e in container.iter() if e.tag.endswith('rootfile'));refined=E.fromstring(archive.read(entry))
  corrected,details=merge_verified_clef_export(root,refined);report.update(state='restored',source_native_sha256=original_hash,native_clefs_restored=len(findings),**details);emit(f'已根据原图补全{len(findings)}处八度高音谱号');return finish(corrected)
 except InterruptedError:raise
 except Exception as exc:
  report.update(state='unresolved',reason=str(exc));emit('谱号复核存在未解决项，已记录在核对报告中');return finish(root)
