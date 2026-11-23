"""Offline piano native recovery; leaves the caller's source book and PNGs unchanged.
Arpeggios are deferred until header/tempo/key/tuplet context processing has finished.
"""
from pathlib import Path
import argparse,sys,json,os,threading,hashlib,shutil,csv,xml.etree.ElementTree as E

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def canon(n):return n.tag,tuple(sorted(n.attrib.items())),(n.text or '').strip(),tuple(canon(c) for c in n)
def recover(args):
 pkg=Path(__file__).resolve().parent;job=args.job.resolve();book=args.book.resolve();out=args.out.resolve();out.mkdir(parents=True,exist_ok=True);app=args.app_root.resolve();sys.path.insert(0,str(app));import xunpdf
 from runtime_resources import wait_for_released_memory
 from bounded_process import run_bounded
 settings=xunpdf.read_settings();engine=Path(settings['engine_root']);python_cv=engine/('ocr-gpu-env' if args.device=='gpu' else 'ocr-env')/'Scripts/python.exe';java=engine/settings['java_relative']/'bin/java.exe';prefix=Path(settings.get('engine_classpath_prefix','engine-classes'));prefix=prefix if prefix.is_absolute() else app/prefix;cp=os.pathsep.join([str(pkg/'runtime'),str(prefix),str(engine/'Audiveris-Bandscore/lib/*')]);env=xunpdf.environment(settings);env['PYTHONUTF8']='1';env['PYTHONDONTWRITEBYTECODE']='1';cancel=threading.Event();initial_hash=sha(book);stages=[]
 def run(command,label):
  if shutil.disk_usage(out).free<2*1024**3:raise RuntimeError('识谱缓存所在磁盘余量不足2GB，已停止新增缓存，原谱和旧结果保留。')
  if Path(command[0]).name.lower()=='java.exe':wait_for_released_memory(out,cancel,emit=print,phase=label)
  run_bounded(xunpdf.run_process,list(map(str,command)),out/(label+'.log'),env,cancel,timeout=300,emit=print)
 def jrun(cls,argv,label):run([java,'-Xmx1536m','-Djava.awt.headless=true','-XX:ActiveProcessorCount=4','--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED','-cp',cp,'org.audiveris.omr.score.'+cls,*argv],label)
 if not python_cv.is_file() or not java.is_file():raise RuntimeError('本地Java或图像分析运行环境不完整')
 meters=args.meters.resolve() if args.meters else out/'meter/source-meters.json'
 if not args.meters:run([python_cv,app/'source_tuplet_meters.py',job,out/'meter'],'source-meter')
 # Export the actual supplied book, including on repeated runs; never reuse unrelated job XML.
 current=book;control=out/'control';jrun('PianoExport357',[current,job,control],'source-control');xml=control/'result.mxl';before=xunpdf.read_score(xml)
 inventory=out/'piano-inventory.tsv';jrun('PianoInventory357',[current,job,inventory],'source-inventory')
 partition=out/'partition';run([sys.executable,pkg/'source_partitions.py','--inventory',inventory,'--meters',meters,'--job',job,'--out',partition],'source-partition-scan');plan=json.loads((partition/'partition-proposals.json').read_text(encoding='utf8'))
 if plan['proposals']:
  dest=out/'partition-native';jrun('SourcePartitions357',[current,job,partition/'rests.tsv',partition/'lanes.tsv',dest],'source-partition-native');current=dest/'refined.omr';xml=dest/'result.mxl'
 stages.append(dict(stage='source_partitions',proposals=len(plan['proposals']),pending=plan['pending']))
 bootstrap=out/'bootstrap-whole';run([python_cv,pkg/'source_bootstrap_wholes.py','--job',job,'--book',current,'--meters',meters,'--out',bootstrap],'source-bootstrap-scan');proposal=json.loads((bootstrap/'source-bootstrap-proposals.json').read_text(encoding='utf8'))
 for unit in sorted({v['unit'] for v in proposal['proposals']}):jrun('WholeVariantProbe',[job/f'unit-{unit:03d}.png',bootstrap/f'classify-unit-{unit:03d}.tsv',bootstrap/f'classification-unit-{unit:03d}.tsv'],f'source-bootstrap-classify-{unit}')
 run([python_cv,pkg/'plan_bootstrap_wholes.py','--job',job,'--book',current,'--proposals',bootstrap/'source-bootstrap-proposals.json','--out',bootstrap],'source-bootstrap-plan');bp=json.loads((bootstrap/'bootstrap-plan-validation.json').read_text(encoding='utf8'))
 if bp['selected']:
  dest=out/'bootstrap-native';jrun('SourceBootstrap364',[current,job,bootstrap/'bootstrap-plan.tsv',dest],'source-bootstrap-native');current=dest/'refined.omr';xml=dest/'result.mxl'
 stages.append(dict(stage='source_donorless_whole_chords',new_source_heads=bp['new_source_heads'],pending=bp['pending']))
 whole=out/'whole-evidence';run([python_cv,pkg/'source_wholes.py','--job',job,'--book',current,'--out',whole],'source-whole-scan');run([sys.executable,pkg/'plan_wholes.py','--book',current,'--scan',whole/'source-wholes.json','--meters',meters,'--job',job,'--out',whole],'source-whole-plan');plan=json.loads((whole/'whole-plan-validation.json').read_text(encoding='utf8'))
 if plan['selected']:
  dest=out/'whole-native';jrun('SourceWholes357',[current,job,whole/'wholes.tsv',dest],'source-whole-native');current=dest/'refined.omr';xml=dest/'result.mxl'
 stages.append(dict(stage='source_whole_chords',new_source_heads=plan['new_source_heads'],pending=plan['pending']))
 half=out/'half-evidence';run([python_cv,pkg/'source_half_chords.py','--job',job,'--book',current,'--out',half],'source-half-scan');plan=json.loads((half/'half-chord-candidates.json').read_text(encoding='utf8'));headlines=[];stemlines=[]
 for f in plan['findings']:
  for h in f['heads']:headlines.append('\t'.join(map(str,[f['unit'],f['system'],f['native_measure'],f['staff'],*h['bounds'],h['pitch_position'],h['donor_unit'],h['donor_head'],f['source_sha256']])))
  stemlines.append('\t'.join(map(str,[f['unit'],f['system'],f['native_measure'],f['staff'],*f['stem_bounds'],*f['stem_median'],f['stem_donor_unit'],f['stem_donor_id'],f['source_sha256']])))
 (half/'heads.tsv').write_text('\n'.join(headlines)+('\n' if headlines else ''),encoding='utf8');(half/'stems.tsv').write_text('\n'.join(stemlines)+('\n' if stemlines else ''),encoding='utf8')
 if plan['findings']:
  dest=out/'half-native';jrun('SourceHalf356',[current,job,half/'heads.tsv',half/'stems.tsv',dest],'source-half-native');current=dest/'refined.omr';xml=dest/'result.mxl'
 stages.append(dict(stage='source_half_chords',new_source_heads=sum(len(f['heads']) for f in plan['findings'])))
 after=xunpdf.read_score(xml)
 if len(before.findall('part'))!=len(after.findall('part')) or canon(before.find('part-list'))!=canon(after.find('part-list')):raise ValueError('原生恢复意外改变了声部映射，结果未接受')
 if sha(book)!=initial_hash:raise ValueError('原始识谱档案被意外改动')
 # Native notes already exported before recovery must all remain addressable by their source IDs.
 def keys(p):
  rr=list(csv.DictReader(Path(p).open(encoding='utf8'),delimiter='\t'));return {(v['unit'],v['system'],v['head'],v['repeat_copy']) for v in rr if v['head']!='0'}
 oldids=keys(control/'note-ledger.tsv');newids=keys(xml.parent/'note-ledger.tsv')
 if not oldids<=newids:raise ValueError('原生恢复丢失既有源音符，结果未接受')
 report=dict(state='source_piano_native_recovered' if current!=book else 'no_source_piano_changes',source_job=str(job),source_book=str(book),source_book_sha256=initial_hash,native_book=str(current),native_xml=str(xml),native_note_ledger=str(xml.parent/'note-ledger.tsv'),native_head_ledger=str(xml.parent/'head-ledger.tsv'),original_native_note_ids_preserved=len(oldids),part_count=len(after.findall('part')),pitches_before=len(before.findall('.//pitch')),pitches_after=len(after.findall('.//pitch')),stages=stages,arpeggios_deferred=True,source_context_processing_required=True,production_deployed=False,whole_score_accuracy_verified=False)
 (out/'source-piano-native.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');print('PIANO_NATIVE_CORE357_OK',json.dumps(report,ensure_ascii=True));return report
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--job',type=Path,required=True);ap.add_argument('--book',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);ap.add_argument('--app-root',type=Path,default=Path(__file__).resolve().parent.parent);ap.add_argument('--meters',type=Path);ap.add_argument('--device',choices=['cpu','gpu'],default='gpu');a=ap.parse_args();recover(a)
