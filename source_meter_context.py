"""Restore printed meter in a preserved native source view before other contexts."""
from pathlib import Path
import os,sys,json,csv,shutil,hashlib,zipfile,re,xml.etree.ElementTree as E
from runtime_resources import check_memory,MemoryBudgetError

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def inventory(path,cancel):
 result={}
 with zipfile.ZipFile(path) as z:
  for name in z.namelist():
   if cancel.is_set():raise InterruptedError('拍号复核已取消')
   if not re.fullmatch(r'sheet#\d+/sheet#\d+\.xml',name):continue
   unit=int(name.split('/')[0][6:]);sheet=E.fromstring(z.read(name))
   for system in sheet.findall('page/system'):
    for head in system.findall('sig/inters/head'):
     key=unit,system.get('id'),head.get('id');bounds=head.find('bounds')
     if key in result or bounds is None:raise ValueError('ambiguous native source head')
     result[key]=(head.get('staff'),head.get('shape'),head.get('pitch'),head.get('glyph'),tuple(sorted(bounds.attrib.items())))
 return result

def refine_source_meter(root,job,settings,device,env,cancel,runner,emit=print,source_job=None):
 job=Path(job);source=Path(source_job) if source_job is not None else job;app=Path(__file__).resolve().parent;book=source/'raw/input.omr';report=dict(state='not_assessed',added_times=0,retained_times=0,restored_barlines=0,retired_time_heads=0,pending=[],scope='printed source-consensus meters before rhythm/voice recovery; original book and raster retained')
 def finish(tree,source):
  (job/'source-meter-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return tree,source,report
 try:
  import xunpdf
  from tempo_overlay import canonical
  pkg=app/'piano-core'
  if str(pkg) not in sys.path:sys.path.insert(0,str(pkg))
  from bounded_process import run_bounded
  if not book.is_file():raise ValueError('source native book unavailable')
  runtime=app/'meter-runtime';scanner=app/'meter-core/prepare_meter_context.py'
  if not scanner.is_file() or not (runtime/'org/audiveris/omr/score/SourceTimes379.class').is_file():raise ValueError('source meter runtime incomplete')
  engine=Path(settings['engine_root']);python=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe';evidence=job/'source-meter-evidence';evidence.mkdir(exist_ok=True);source_hash=sha(book)
  emit('先按原图核对拍号变化和小节线，再重算内部节奏')
  for extra,name in [([], 'scan'),(['--validate'],'validate')]:run_bounded(runner,[python,scanner,source,evidence,*extra],job/f'source-meter-{name}.log',env,cancel,timeout=300,emit=emit)
  plan=json.loads((evidence/'combined-plan.json').read_text(encoding='utf8'))
  if source_hash!=plan['times']['book_sha256'] or sha(book)!=source_hash:raise ValueError('source meter book changed')
  pending=list(plan['times']['pending'])+list(plan['conflict_pending']);native=evidence/'native';native.mkdir(exist_ok=True)
  prefix=Path(settings.get('engine_classpath_prefix','engine-classes'));prefix=prefix if prefix.is_absolute() else app/prefix
  cp=os.pathsep.join(map(str,[runtime,pkg/'runtime',prefix,engine/'Audiveris-Bandscore/lib/*']));java=engine/settings['java_relative']/'bin/java.exe';check_memory(job,'source-meter-native')
  args=[java,'-Xmx1536m','-XX:ActiveProcessorCount=4','-Djava.awt.headless=true','--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED','-cp',cp,'org.audiveris.omr.score.SourceTimes379',book,source,evidence/'times.tsv',native,source_hash,evidence/'barlines.tsv',evidence/'conflicts.tsv']
  run_bounded(runner,args,job/'source-meter-native.log',env,cancel,timeout=300,emit=emit)
  summary={k:int(v) for k,v in csv.reader((native/'meter-recovery.tsv').open(encoding='utf8'),delimiter='\t')}
  if set(summary)!={'added_times','retained_times','restored_barlines','retired_time_heads'}:raise ValueError('native meter summary incomplete')
  conflicts=list(csv.DictReader((native/'source-time-conflicts.tsv').open(encoding='utf8'),delimiter='\t'));pending.extend(row for row in conflicts if row['status']!='retired')
  retired={(int(row['unit']),row['system'],head) for row in conflicts if row['status']=='retired' for head in row['heads'].split(',')};before=inventory(book,cancel);after=inventory(native/'refined.omr',cancel)
  if before.keys()-after.keys()!=retired or after.keys()-before.keys() or any(value!=before[key] for key,value in after.items()):raise ValueError('meter recovery changed unproved source head geometry or pitch')
  if len(retired)!=summary['retired_time_heads']:raise ValueError('retired source head count differs')
  for unit,digest in plan['times']['source_images'].items():
   if cancel.is_set():raise InterruptedError('拍号复核已取消')
   if sha(source/f'unit-{int(unit):03d}.png')!=digest:raise ValueError('source raster changed after meter repair')
  if sha(book)!=source_hash:raise ValueError('original native book changed')
  report.update(**summary,pending=pending,source_book_sha256=source_hash,evidence_directory=str(evidence),all_remaining_source_head_geometry_and_pitch_preserved=True)
  changed=bool(summary['added_times'] or summary['restored_barlines'] or summary['retired_time_heads'])
  if not changed:
   report['state']='needs_review' if pending else ('native_meters_match_source' if plan['times']['rows'] else 'no_supported_source_meters');return finish(root,source)
  corrected=xunpdf.read_score(native/'result.mxl')
  if len(corrected.findall('part'))!=len(root.findall('part')) or canonical(corrected.find('part-list'))!=canonical(root.find('part-list')):raise ValueError('meter recovery changed logical part identities')
  view=evidence/'source-view';(view/'raw').mkdir(parents=True,exist_ok=True);target=view/'raw/input.omr'
  if target.exists():raise ValueError('source meter view already exists; keep previous evidence')
  shutil.copy2(native/'refined.omr',target)
  for image in sorted(source.glob('unit-*.png')):
   if cancel.is_set():raise InterruptedError('拍号复核已取消')
   try:os.link(image,view/image.name)
   except OSError:shutil.copy2(image,view/image.name)
  if (source/'source-map.json').is_file():shutil.copy2(source/'source-map.json',view/'source-map.json')
  report.update(state='partially_restored' if pending else 'restored',source_view=str(view),corrected_book_sha256=sha(target),original_native_book_preserved=True)
  emit('原图拍号复核：恢复'+str(summary['added_times'])+'处拍号，保留'+str(len(pending))+'处待核；原识别档案已保留')
  return finish(corrected,view)
 except (InterruptedError,MemoryBudgetError):raise
 except Exception as exc:
  report.update(state='unresolved',reason=str(exc));emit('原图拍号复核未通过完整校验，保留原识别结果并记录原因');return finish(root,source)
