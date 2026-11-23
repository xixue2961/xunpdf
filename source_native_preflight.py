"""Preserve native symbols, validate source before loading, then resume LINKS/RHYTHMS."""
from pathlib import Path
import os,sys,json,csv,shutil,hashlib,zipfile,xml.etree.ElementTree as E
from source_meter_context import inventory
from runtime_resources import check_memory,MemoryBudgetError

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def steps(path):
 with zipfile.ZipFile(path) as z:
  book=E.fromstring(z.read('book.xml'))
  return [s.findtext('steps','').split() for s in book.findall('sheet')]
def preserved_head_subset(before,after,retired):
 if before.keys()-after.keys()!=retired or after.keys()-before.keys() or any(v!=before[k] for k,v in after.items()):raise ValueError('unproved source head change')

def carry_native_source(book,xml,source,target,cancel):
 book,xml,source,target=map(Path,(book,xml,source,target));book_sha,xml_sha=sha(book),sha(xml)
 (target/'raw').mkdir(parents=True,exist_ok=False)
 shutil.copy2(book,target/'raw/input.omr');shutil.copy2(xml,target/'raw/input.mxl')
 for image in source.glob('unit-*.png'):
  if cancel.is_set():raise InterruptedError('已取消修订源谱交接')
  try:os.link(image,target/image.name)
  except OSError:shutil.copy2(image,target/image.name)
 if (source/'source-map.json').is_file():shutil.copy2(source/'source-map.json',target/'source-map.json')
 if sha(target/'raw/input.omr')!=book_sha or sha(target/'raw/input.mxl')!=xml_sha or sha(book)!=book_sha or sha(xml)!=xml_sha:raise ValueError('native source carry changed its paired book/XML')
 carried_steps=steps(target/'raw/input.omr')
 if not carried_steps or any(not step or step[-1]!='PAGE' for step in carried_steps):raise ValueError('native notation carry lost completed processing steps')
 return target


def complete_native_source(job,settings,device,env,cancel,runner,initial_args,emit=print):
 job=Path(job);app=Path(__file__).resolve().parent;evidence=job/'native-preflight';evidence.mkdir(exist_ok=True)
 report=dict(state='preparing',retired_void_heads=0,early_added_times=0,early_retired_time_heads=0,early_restored_barlines=0,pending=[],original_native_book=str(job/'raw/input.omr'),scope='source-backed preparation before native symbol reload/linking; no original archive replacement')
 def save(): (job/'source-native-preflight.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
 save()
 try:
  if cancel.is_set():raise InterruptedError('已取消原图前置复核')
  import xunpdf
  pkg=app/'piano-core'
  if str(pkg) not in sys.path:sys.path.insert(0,str(pkg))
  from bounded_process import run_bounded
  engine=Path(settings['engine_root']);python=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe';original=job/'raw/input.omr';original_hash=sha(original);original_steps=steps(original)
  if not original_steps or any(not s or s[-1]!='SYMBOLS' for s in original_steps):raise ValueError('native source checkpoint must end at SYMBOLS')
  check_memory(job,'source-head-preflight');emit('先核对原图符头，保住和弦附点，再计算节奏')
  run_bounded(runner,[python,app/'meter-core/prepare_native_head_context.py',job,evidence],job/'source-native-heads.log',env,cancel,timeout=300,emit=emit)
  head_result=json.loads((evidence/'head-result.json').read_text(encoding='utf8'));head_plan=json.loads((evidence/'head-plan.json').read_text(encoding='utf8'));head_source=Path(head_result['source_view']);head_book=head_source/'raw/input.omr';head_retired={(int(q['unit']),q['system'],q['head']) for q in head_plan['candidates']}
  if sha(original)!=original_hash or head_plan['book_sha256']!=original_hash:raise ValueError('original source changed during head proof')
  preserved_head_subset(inventory(original,cancel),inventory(head_book,cancel),head_retired)
  if steps(head_book)!=original_steps:raise ValueError('source head preparation changed native processing steps')
  report.update(retired_void_heads=len(head_retired),pending=list(head_plan['pending']))
  meter=evidence/'meter-plan';scanner=app/'meter-core/prepare_meter_context.py'
  for extra,label in [([], 'scan'),(['--validate'],'validate')]:
   check_memory(job,'early-meter-'+label);run_bounded(runner,[python,scanner,head_source,meter,*extra],job/('source-early-meter-'+label+'.log'),env,cancel,timeout=300,emit=emit)
  plan=json.loads((meter/'combined-plan.json').read_text(encoding='utf8'));source_hash=sha(head_book)
  if source_hash!=plan['times']['book_sha256']:raise ValueError('early source meter proof changed')
  check_memory(job,'early-meter-native');native=evidence/'native';native.mkdir(exist_ok=True);prefix=Path(settings.get('engine_classpath_prefix','engine-classes'));prefix=prefix if prefix.is_absolute() else app/prefix
  cp=os.pathsep.join(map(str,[app/'meter-runtime',app/'piano-core/runtime',prefix,engine/'Audiveris-Bandscore/lib/*']));java=engine/settings['java_relative']/'bin/java.exe'
  args=[java,'-Xmx1536m','-XX:ActiveProcessorCount=4','-Djava.awt.headless=true','--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED','-cp',cp,'org.audiveris.omr.score.SourceTimesEarly383',head_book,head_source,meter/'times.tsv',native,source_hash,meter/'barlines.tsv',meter/'conflicts.tsv']
  run_bounded(runner,args,job/'source-early-meter-native.log',env,cancel,timeout=300,emit=emit)
  summary={k:int(v) for k,v in csv.reader((native/'meter-recovery.tsv').open(encoding='utf8'),delimiter='\t')}
  if set(summary)!={'added_times','retained_times','restored_barlines','retired_time_heads'}:raise ValueError('early meter summary incomplete')
  conflicts=list(csv.DictReader((native/'source-time-conflicts.tsv').open(encoding='utf8'),delimiter='\t'));retired={(int(row['unit']),row['system'],head) for row in conflicts if row['status']=='retired' for head in row['heads'].split(',')}
  corrected=native/'refined.omr';preserved_head_subset(inventory(head_book,cancel),inventory(corrected,cancel),retired)
  if len(retired)!=summary['retired_time_heads'] or steps(corrected)!=original_steps:raise ValueError('early native proof mismatch')
  disagreements_path=native/'source-time-disagreements.tsv'
  with disagreements_path.open(encoding='utf8',newline='') as stream:
   disagreements=list(csv.DictReader(stream,delimiter='\t'))
  corrected_times=[row for row in disagreements if row['state']=='corrected']
  report['early_corrected_times']=corrected_times
  report['pending']+=[dict(stage='early_meter_conflict',**row) for row in disagreements if row['state']=='unresolved']
  changed=bool(summary['added_times'] or summary['restored_barlines'] or summary['retired_time_heads'] or corrected_times)
  selected=corrected if changed else head_book
  report.update(early_added_times=summary['added_times'],early_retained_times=summary['retained_times'],early_retired_time_heads=summary['retired_time_heads'],early_restored_barlines=summary['restored_barlines'])
  report['pending']+=list(plan['times']['pending'])+list(plan['conflict_pending'])+[row for row in conflicts if row['status']!='retired']
  for unit,digest in plan['times']['source_images'].items():
   if sha(job/f'unit-{int(unit):03d}.png')!=digest:raise ValueError('original raster changed during preparation')
  if sha(original)!=original_hash or sha(head_book)!=source_hash:raise ValueError('native source archive changed during preparation')
  split_report = dict(state='not_applicable', changes=[], pending=[])
  split_dir = evidence/'source-barline-splits'
  plan_file = evidence/'missing-barlines.tsv'
  selected_hash = sha(selected)
  try:
   check_memory(job,'missing-barlines')
   run_bounded(runner,[python,app/'meter-core/source_missing_barlines.py',selected,job,plan_file],
               job/'source-missing-barlines.log',env,cancel,timeout=180,emit=emit)
   if plan_file.stat().st_size:
    check_memory(job,'split-printed-barlines')
    args = [java,'-Xmx1536m','-XX:ActiveProcessorCount=4','-Djava.awt.headless=true',
            '--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED',
            '-cp',cp,'org.audiveris.omr.score.SourceSplitBarlines',selected,job,plan_file,split_dir,selected_hash]
    run_bounded(runner,args,job/'source-split-barlines.log',env,cancel,timeout=300,emit=emit)
    with (split_dir/'splits.tsv').open(encoding='utf8',newline='') as ledger:
     for row in csv.DictReader(ledger,delimiter='\t'):
      split_report['changes' if row['state']=='split' else 'pending'].append(row)
    if split_report['changes']:
     split_book = split_dir/'input.omr'
     preserved_head_subset(inventory(selected,cancel),inventory(split_book,cancel),set())
     if steps(split_book)!=original_steps or sha(selected)!=selected_hash:
      raise ValueError('Split archive changed the native stage or its input')
     selected=split_book
     split_report['state']='partially_restored' if split_report['pending'] else 'restored'
     emit(f"已按原图恢复 {len(split_report['changes'])} 处小节线，随后重新计算节奏和歌词位置")
    else:
     split_report['state']='needs_review'
  except InterruptedError:
   raise
  except Exception as exc:
   split_report=dict(state='unresolved',changes=[],pending=[dict(reason=str(exc))])
  report['source_barline_splits']=split_report
  report['pending'] += [dict(stage='source_barline_split',**row) for row in split_report['pending']]
  common_report=dict(state='not_applicable',changes=[],pending=[])
  common_dir=evidence/'source-inline-common-time'
  common_plan=evidence/'inline-common-time.tsv'
  selected_hash=sha(selected)
  try:
   check_memory(job,'inline-common-time')
   run_bounded(runner,[python,app/'meter-core/source_inline_common_time.py',selected,job,common_plan],
               job/'source-inline-common-time.log',env,cancel,timeout=180,emit=emit)
   if common_plan.stat().st_size:
    check_memory(job,'restore-inline-common-time')
    args=[java,'-Xmx1536m','-XX:ActiveProcessorCount=4','-Djava.awt.headless=true',
          '--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED',
          '-cp',cp,'org.audiveris.omr.score.SourceInlineCommonTime',selected,job,common_plan,common_dir,selected_hash]
    run_bounded(runner,args,job/'source-restore-inline-common-time.log',env,cancel,timeout=300,emit=emit)
    with (common_dir/'common-time.tsv').open(encoding='utf8',newline='') as ledger:
     for row in csv.DictReader(ledger,delimiter='\t'):
      common_report['changes' if row['state']=='restored' else 'pending'].append(row)
    if common_report['changes']:
     common_book=common_dir/'input.omr'
     preserved_head_subset(inventory(selected,cancel),inventory(common_book,cancel),set())
     if steps(common_book)!=original_steps or sha(selected)!=selected_hash:
      raise ValueError('Common-time archive changed the native stage or its input')
     selected=common_book
     common_report['state']='partially_restored' if common_report['pending'] else 'restored'
     emit(f"已按原图恢复 {len(common_report['changes'])} 处 4/4 拍号")
    else:
     common_report['state']='needs_review'
  except InterruptedError:
   raise
  except Exception as exc:
   common_report=dict(state='unresolved',changes=[],pending=[dict(reason=str(exc))])
  report['source_inline_common_time']=common_report
  report['pending'] += [dict(stage='source_inline_common_time',**row) for row in common_report['pending']]
  artifact_report=dict(state='not_applicable',changes=[])
  if any(row['reason'].split('false_beam_links=')[-1].isdigit() and
         int(row['reason'].split('false_beam_links=')[-1])>0 for row in split_report['changes']):
   cleaned=evidence/'source-piano-graph-cleaned.omr'
   proof=evidence/'source-piano-graph-artifacts.json'
   try:
    scanner=app/'source_piano_graph_artifacts.py'
    run_bounded(runner,[python,scanner,original,selected,job,cleaned,proof],
                job/'source-piano-graph-artifacts.log',env,cancel,timeout=180,emit=emit)
    changes=json.loads(proof.read_text('utf8'))
    retired={(int(row['unit']),row['system'],h) for row in changes for h in row['heads']}
    if retired:
     preserved_head_subset(inventory(selected,cancel),inventory(cleaned,cancel),retired)
     if steps(cleaned)!=original_steps or sha(original)!=original_hash:
      raise ValueError('Gray-head recovery changed the native stage or original archive')
     selected=cleaned
     artifact_report=dict(state='restored',changes=changes)
     emit(f"已按原图清除 {len(retired)} 枚与小节线误接的水印假符头")
   except InterruptedError:
    raise
   except Exception as exc:
    artifact_report=dict(state='unresolved',changes=[],reason=str(exc))
    report['pending'].append(dict(stage='source_piano_graph_artifacts',reason=str(exc)))
  report['source_piano_graph_artifacts']=artifact_report
  view=job/'native-completed-source';(view/'raw').mkdir(parents=True);completed=view/'raw/input.omr';shutil.copy2(selected,completed)
  for image in job.glob('unit-*.png'):
   if cancel.is_set():raise InterruptedError('已取消原图前置复核')
   try:os.link(image,view/image.name)
   except OSError:shutil.copy2(image,view/image.name)
  if (job/'source-map.json').is_file():shutil.copy2(job/'source-map.json',view/'source-map.json')
  report.update(state='resuming_native',source_view=str(view),original_native_sha256=original_hash,prepared_native_sha256=sha(selected),all_unretired_source_head_geometry_and_pitch_preserved=True);save()
  resume=list(initial_args[:initial_args.index('--')]);resume[resume.index('-step')+1]='PAGE';resume[resume.index('-output')+1]=view/'raw';resume+=['-export','--',completed]
  check_memory(job,'resume-native-rhythms');emit('原图前置复核完成，继续同一整谱的声部与节奏分析')
  run_bounded(runner,resume,job/'engine.log',env,cancel,timeout=1800,emit=emit)
  log=(job/'engine.log').read_text(encoding='utf8',errors='replace')
  if 'PaddleOCR server error' in log or 'PaddleOCR recognition failed' in log or 'Too large image' in log:raise ValueError('native log contains OCR failure')
  files=list((view/'raw').glob('*.mxl'))
  if len(files)!=1 or any(not s or s[-1]!='PAGE' for s in steps(completed)):raise ValueError('native resume did not complete one full score')
  if sha(original)!=original_hash:raise ValueError('original SYMBOLS archive changed during resume')
  # Apply source-supported piano notation to the native graph before any context/lyric projection.
  from source_native_rhythm_backend import recover_native_rhythm
  repaired_book,repaired_xml,rhythm_report= recover_native_rhythm(job,completed,files[0],settings,device,env,cancel,runner,emit)
  from source_native_notation import refine_native_notation
  repaired_book,repaired_xml,notation_report=refine_native_notation(job,repaired_book,repaired_xml,settings,device,env,cancel,runner,emit)
  report['native_notation']=notation_report
  report['pending']+=notation_report.get('pending',[])
  if notation_report['state']=='unresolved_original_preserved':report['pending'].append(dict(stage='native_notation',reason=notation_report['reason']))
  report['native_rhythm']=rhythm_report
  report['pending']+=rhythm_report.get('pending',[])
  selected_xml=files[0]
  if repaired_book!=completed:
   carried=carry_native_source(repaired_book,repaired_xml,job,job/'native-rhythm-source',cancel)
   view=carried;completed=carried/'raw/input.omr';selected_xml=carried/'raw/input.mxl'
  report.update(state='partially_restored' if report['pending'] else 'completed',source_view=str(view),completed_native_sha256=sha(completed),original_native_preserved=True);save();return selected_xml,view,report
 except (InterruptedError,MemoryBudgetError):
  report['state']='stopped_preserved';save();raise
 except Exception as exc:
  report.update(state='failed_preserved',reason=str(exc));save();raise RuntimeError('原图前置复核或节奏续算未完成；原谱和中间结果已保留。详情：'+str(job/'source-native-preflight.json')+'；'+str(exc)) from exc



def complete_upstream_source(job, env, cancel, runner, initial_args, emit):
    """Continue the saved SYMBOLS book with upstream Bandscore alone."""
    original = job / "raw/input.omr"
    original_hash = sha(original)
    source_view = job / "native-completed-source"
    output = source_view / "raw"
    output.mkdir(parents=True)
    completed = output / "input.omr"
    shutil.copyfile(original, completed)
    for image in job.glob("unit-*.png"):
        if cancel.is_set():
            raise InterruptedError("已取消识谱")
        try:
            os.link(image, source_view / image.name)
        except OSError:
            shutil.copyfile(image, source_view / image.name)
    shutil.copyfile(job / "source-map.json", source_view / "source-map.json")

    args = list(initial_args[:initial_args.index("--")])
    args[args.index("-step") + 1] = "PAGE"
    args[args.index("-output") + 1] = output
    args.extend(["-export", "--", completed])
    check_memory(job, "resume-upstream-rhythms")
    emit("使用 Audiveris-Bandscore 继续声部与节奏识别")
    runner(args, job / "engine.log", env, cancel, timeout=1800, emit=emit)
    files = list(output.glob("*.mxl"))
    processed = steps(completed)
    if len(files) != 1 or not processed or any(not row or row[-1] != "PAGE" for row in processed):
        raise RuntimeError("Audiveris-Bandscore 没有生成完整的乐谱")
    if sha(original) != original_hash:
        raise RuntimeError("符号识别断点意外发生变化")
    log = (job / "engine.log").read_text(encoding="utf-8", errors="replace")
    if any(error in log for error in ("PaddleOCR server error", "PaddleOCR recognition failed", "Too large image")):
        raise RuntimeError("原生续跑日志包含 OCR 或图像错误")
    report = {
        "state": "upstream_completed",
        "source_view": str(source_view),
        "original_native_preserved": True,
        "retired_void_heads": 0,
        "early_added_times": 0,
        "early_retired_time_heads": 0,
        "pending": [{"stage": "native_preflight", "reason": "optional Java extensions unavailable"}],
    }
    (job / "source-native-preflight.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
    )
    return files[0], source_view, report
