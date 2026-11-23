from pathlib import Path
import os,json,hashlib,shutil,csv
from source_tempo_context import _score
from key_overlay import verify_key_ledger,merge_verified_keys

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def refine_source_keys(root,job,settings,device,parts_hint,env,cancel,runner,emit=print,source_job=None):
 job=Path(job);source=Path(source_job) if source_job is not None else job;base=Path(__file__).resolve().parent;report=dict(state='not_assessed',scope='Missing pitched-staff header keys with source-pixel multi-donor agreement; explicit existing keys are preserved',key_changes=[],pitch_alter_changes=[],pending=[])
 def finish(tree):
  (job/'source-key-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return tree,report
 try:
  native=source/'raw/input.omr';runtime=base/'key-runtime';scanner=base/'source_keys.py'
  if not native.is_file() or not (runtime/'org/audiveris/omr/score/SourceKeyRecovery331.class').is_file():report['reason']='source key runtime unavailable';return finish(root)
  engine=Path(settings['engine_root']);python=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe';emit('核对原图调号及跨行继承')
  evidence=job/'source-key-evidence';runner([str(python),str(scanner),str(source),str(evidence)],job/'source-keys-scan.log',env,cancel,timeout=300,emit=emit);scan=json.loads((evidence/'scan.json').read_text(encoding='utf8'));supported=[f for f in scan['findings'] if f['state']=='missing_header_key_supported'];report['pending']=[f for f in scan['findings'] if f['state']!='missing_header_key_supported'];report['source_headers_supported']=len(supported)
  if not supported:report['state']='needs_review' if report['pending'] else 'no_source_key_recovery_candidates';return finish(root)
  runner([str(python),str(scanner),str(source),str(evidence),'--validate'],job/'source-keys-validate.log',env,cancel,timeout=300,emit=emit)
  native_hash=sha(native)
  if native_hash!=scan['source_native_sha256']:raise ValueError('source native archive changed')
  cp=[str(runtime)];prefix=settings.get('engine_classpath_prefix')
  if prefix:
   prefix=Path(prefix);cp.append(str(prefix if prefix.is_absolute() else base/prefix))
  cp.append(str(engine/'Audiveris-Bandscore/lib/*'));java=engine/settings['java_relative']/'bin/java.exe'
  def export(mode):
   folder=job/'source-key-native'/mode;folder.mkdir(parents=True,exist_ok=True);shutil.copy2(native,folder/'input.omr');args=[str(java),'-Xmx1536m','-Djava.awt.headless=true','--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED','-cp',os.pathsep.join(cp),'org.audiveris.omr.score.SourceKeyRecovery331',str(folder/'input.omr'),native_hash,str(source),str(evidence/'plan.tsv'),str(folder),mode,parts_hint];runner(args,job/f'source-keys-{mode}.log',env,cancel,timeout=300,emit=emit)
   if sha(native)!=native_hash:raise ValueError('source native archive changed')
   return folder
  control=export('control');refined=export('apply')
  if (control/'key-ledger.tsv').read_bytes()!=(refined/'key-ledger.tsv').read_bytes():raise ValueError('source key export scope changed')
  scopes=verify_key_ledger(refined/'key-ledger.tsv',scan,source);corrected,details=merge_verified_keys(root,_score(control/'result.mxl'),_score(refined/'result.mxl'),scopes);report.update(state='partially_restored' if report['pending'] else 'restored',source_native_sha256=native_hash,**details);emit(f'调号复核：修正{len(details["key_changes"])}处调号及{len(details["pitch_alter_changes"])}个受影响升降音')
  return finish(corrected)
 except InterruptedError:raise
 except Exception as exc:report.update(state='unresolved',reason=str(exc));emit('调号复核未通过完整校验，已保留此前结果并记录原因');return finish(root)
