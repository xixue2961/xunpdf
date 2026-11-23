"""Offline source piano reconstruction after existing header/tempo/key/tuplet context.
The caller's accepted vocal parts remain the output base.
"""
from pathlib import Path
import sys,copy,json,hashlib,os,shutil,xml.etree.ElementTree as E
from runtime_resources import MemoryBudgetError, wait_for_released_memory

def refine_source_piano(root,job,settings,device,parts_hint,env,cancel,runner,emit=print,source_job=None):
 job=Path(job);source=Path(source_job) if source_job is not None else job;app=Path(__file__).resolve().parent;pkg=app/'piano-core';report=dict(state='not_assessed',pending=[],pitches_before=len(root.findall('.//pitch')),scope='source-backed grand-staff voice, whole and half chord recovery; no overall accuracy estimate')
 def finish(tree):
  report['pitches_after']=len(tree.findall('.//pitch'));(job/'source-piano-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return tree,report
 if not any(e.text=='2' for e in root.findall('part/measure/attributes/staves')):report['state']='no_grandstaff';return finish(root)
 try:
  if not (pkg/'recover_piano_native.py').is_file() or not (source/'raw/input.omr').is_file():raise ValueError('source piano native runtime unavailable')
  if str(pkg) not in sys.path:sys.path.insert(0,str(pkg))
  import xunpdf
  from bounded_process import run_bounded
  from source_piano_merge import merge_piano_parts,context_hash
  from source_arpeggio_overlay import recover_source_arps
  cache=settings.get('piano_core_cache_root');work=(Path(cache) if cache else job/'source-piano-core')/job.name if cache else job/'source-piano-core';work.mkdir(parents=True,exist_ok=True);report['evidence_directory']=str(work)
  def checked_runner(args,log,runenv,event,**kwargs):
   args=list(map(str,args))
   if Path(args[0]).name.lower()=='java.exe':
    wait_for_released_memory(work,event,emit=kwargs.get("emit"),phase=Path(log).stem)
    if '-XX:ActiveProcessorCount=4' not in args:args.insert(1,'-XX:ActiveProcessorCount=4')
   return run_bounded(runner,args,log,runenv,event,**kwargs)
  emit('根据原图重建钢琴声部、持续音和漏识和弦')
  args=[sys.executable,str(pkg/'recover_piano_native.py'),'--job',str(source),'--book',str(source/'raw/input.omr'),'--out',str(work/'native-core'),'--app-root',str(app),'--device',device]
  meters=job/'source-tuplet-evidence/meter/source-meters.json'
  if meters.is_file():args.extend(['--meters',str(meters)])
  checked_runner(args,job/'source-piano-core.log',env,cancel,timeout=1200,emit=emit)
  meta=json.loads((work/'native-core/source-piano-native.json').read_text(encoding='utf8'));report['native_recovery']=meta;fixed=root
  if meta['state']=='source_piano_native_recovered':
   view=work/'context-source';(view/'raw').mkdir(parents=True,exist_ok=True);shutil.copy2(meta['native_book'],view/'raw/input.omr')
   for path in source.glob('unit-*.png'):
    target=view/path.name
    if not target.exists():
     try:os.link(path,target)
     except OSError:shutil.copy2(path,target)
   ctx=work/'context';ctx.mkdir(exist_ok=True);native_hash=hashlib.sha256((view/'raw/input.omr').read_bytes()).hexdigest();restored=xunpdf.read_score(Path(meta['native_xml']));receipts=[]
   from source_context import refine_source_clefs
   from source_tempo_context import refine_source_tempos
   from source_symbol_context import refine_source_symbols
   from source_key_context import refine_source_keys
   from source_tuplet_context import refine_source_tuplets
   for label,function in [('clefs',refine_source_clefs),('tempo',refine_source_tempos),('symbols',refine_source_symbols),('keys',refine_source_keys),('tuplets',refine_source_tuplets)]:
    old_hash=context_hash(restored);restored,detail=function(restored,ctx,settings,device,parts_hint,env,cancel,checked_runner,emit=emit,source_job=view);receipts.append(dict(stage=label,input_sha256=old_hash,output_sha256=context_hash(restored),state=detail.get('state'),report=detail))
    if detail.get('state') in ['unresolved','not_assessed']:raise ValueError('corrected piano context did not verify: '+label)
   if hashlib.sha256((view/'raw/input.omr').read_bytes()).hexdigest()!=native_hash:raise ValueError('core context source book changed')
   report['context_receipts']=receipts
   before_native=xunpdf.read_score(work/'native-core/control/result.mxl')
   after_native=xunpdf.read_score(Path(meta['native_xml']))
   control_ledger=work/'native-core/control/note-ledger.tsv'
   after_ledger=Path(meta['native_note_ledger'])
   try:
    fixed,details=merge_piano_parts(root,restored,before_native,after_native,control_ledger,after_ledger,context_receipts=receipts)
   except ValueError as exc:
    from source_whole_rest_projection import project_whole_rests
    fixed,details=project_whole_rests(root,restored,before_native,after_native,control_ledger,after_ledger,
                                     work/'native-core/whole-evidence/wholes.tsv',source)
    details['complete_piano_projection_unavailable']=str(exc)
   report['merge']=details
  # Numbered independent arpeggios are emitted last; later native exports cannot discard their grouping.
  fixed,arps=recover_source_arps(fixed,source,Path(meta['native_book']),work/'native-core/whole-evidence/source-wholes.json',work/'late-arpeggios',app,settings,device,env,cancel,checked_runner,emit=emit);report['arpeggios']=arps;report['pending']=arps.get('pending',[])+[p for stage in meta['stages'] for p in stage.get('pending',[])];changed=meta['state']=='source_piano_native_recovered' or bool(arps.get('applied'));report['state']=('partially_restored' if report['pending'] else 'restored') if changed else ('needs_review' if report['pending'] else 'no_source_piano_changes');emit('钢琴原图恢复完成：补回'+str(len(fixed.findall('.//pitch'))-len(root.findall('.//pitch')))+'个实音；人声内容保留');return finish(fixed)
 except MemoryBudgetError as exc:
  report.update(state="interrupted_memory",reason=str(exc));finish(root);raise
 except InterruptedError:raise
 except Exception as exc:
  report.update(state='unresolved',reason=str(exc));emit('钢琴恢复未通过完整校验，已保留此前识别结果，原因见核对报告');return finish(root)
