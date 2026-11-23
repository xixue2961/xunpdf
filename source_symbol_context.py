"""Resolve proven cross-system Chinese direction glyphs misrecognized as pedal releases."""
from pathlib import Path
import os,json,csv,hashlib,shutil
from source_tempo_context import _score
from text_pedal_overlay import verify_text_pedal_evidence,merge_verified_text_pedals,_rows

def _sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def refine_source_symbols(root,job,settings,device,parts_hint,env,cancel,runner,emit=print,source_job=None):
 job=Path(job);source=Path(source_job) if source_job is not None else job;base=Path(__file__).resolve().parent
 report=dict(state='not_assessed',scope='Cross-system multi-Han printed direction glyphs misrecognized as PEDAL_UP_MARK only',removed=[])
 def finish(tree):
  (job/'source-symbol-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return tree,report
 try:
  native=source/'raw/input.omr';runtime=base/'text-symbol-runtime';target=runtime/'org/audiveris/omr/score/SourceTextPedals327.class'
  if not native.is_file() or not target.is_file():report['reason']='native book or text-symbol runtime unavailable';return finish(root)
  engine=Path(settings['engine_root']);java=engine/settings['java_relative']/'bin/java.exe';cp=[str(runtime)];prefix=settings.get('engine_classpath_prefix')
  if prefix:
   prefix=Path(prefix);cp.append(str(prefix if prefix.is_absolute() else base/prefix))
  cp.append(str(engine/'Audiveris-Bandscore/lib/*'));native_hash=_sha(native);report['source_native_sha256']=native_hash
  emit('核对跨行表情文字与踏板符号的墨迹冲突')
  def run(mode):
   folder=job/'source-text-symbol-native'/mode;folder.mkdir(parents=True,exist_ok=True);shutil.copy2(native,folder/'input.omr');args=[str(java),'-Xmx1536m','-Djava.awt.headless=true','--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED','-cp',os.pathsep.join(cp),'org.audiveris.omr.score.SourceTextPedals327',str(folder/'input.omr'),native_hash,str(source),str(folder),mode,parts_hint]
   runner(args,job/f'source-text-symbol-{mode}.log',env,cancel,timeout=300,emit=emit)
   if _sha(native)!=native_hash:raise ValueError('source native archive changed during symbol verification')
   return folder
  scan_folder=run('scan');proofs=verify_text_pedal_evidence(source,scan_folder/'source-text-pedals.tsv');report['source_conflicts']=list(proofs.values())
  if not proofs:report['state']='no_source_text_pedal_conflicts';return finish(root)
  control_folder=run('control');refined_folder=run('apply')
  key=lambda row:(int(row['unit']),int(row['pedal_system']),int(row['pedal']))
  scan_rows=sorted(_rows(scan_folder/'source-text-pedals.tsv'),key=key)
  for folder in (control_folder,refined_folder):
   if sorted(_rows(folder/'source-text-pedals.tsv'),key=key)!=scan_rows:raise ValueError('source conflict evidence changed during native replay')
  corrected,details=merge_verified_text_pedals(root,_score(control_folder/'result.mxl'),_score(refined_folder/'result.mxl'),control_folder/'text-pedal-ledger.tsv',proofs)
  report.update(state='restored',**details);emit(f'已根据原图消除{len(details["removed"])}处文字误识踏板，保留原表情文字')
  return finish(corrected)
 except InterruptedError:raise
 except Exception as exc:
  report.update(state='unresolved',reason=str(exc));emit('文字与符号复核未通过完整校验，已保留此前结果并记录原因');return finish(root)
