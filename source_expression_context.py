"""Source graphic ownership and existing unlinked grand-staff wedge recovery."""
from pathlib import Path
import json,sys,hashlib,copy,xml.etree.ElementTree as E

def refine_source_expression(root,job,settings,device,parts_hint,env,cancel,runner,emit=print,source_job=None):
 job=Path(job);source=Path(source_job) if source_job is not None else job;base=Path(__file__).resolve().parent;report=dict(state='not_assessed',scope='source piano articulation graphic ownership and unlinked held-bass wedge only',pending=[])
 def finish(tree):
  (job/'source-expression-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return tree,report
 if not any(int(x.text)>1 for x in root.findall('part/measure/attributes/staves')):report['state']='no_grandstaff';return finish(root)
 try:
  from source_expression_overlay import apply_verified_expression
  pkg=base/'piano-core'
  if str(pkg) not in sys.path:sys.path.insert(0,str(pkg))
  from bounded_process import run_bounded
  piano=json.loads((job/'source-piano-context.json').read_text(encoding='utf8'));meta=piano.get('native_recovery',{});native=Path(meta['native_book']);ledger=Path(meta['native_note_ledger']);evidence=job/'source-expression-evidence';evidence.mkdir(exist_ok=True);score=evidence/'input.musicxml';E.ElementTree(copy.deepcopy(root)).write(score,encoding='utf-8',xml_declaration=True);book_hash=hashlib.sha256(native.read_bytes()).hexdigest();engine=Path(settings['engine_root']);python=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe';meters=job/'source-tuplet-evidence/meter/source-meters.json';tuplet_scan=job/'source-tuplet-evidence/source-tuplets.json';tuplet_context=job/'source-tuplet-context.json'
  if not all(path.is_file() for path in [native,ledger,meters,tuplet_scan,tuplet_context]):raise ValueError('verified source expression inputs unavailable')
  emit('核对演奏记号的原图墨迹和渐强渐弱线关联')
  args=[python,base/'source_expression_scan.py','--score',score,'--book',native,'--job',source,'--ledger',ledger,'--meters',meters,'--tuplet-scan',tuplet_scan,'--tuplet-context',tuplet_context,'--out',evidence]
  run_bounded(runner,list(map(str,args)),job/'source-expression-scan.log',env,cancel,timeout=300,emit=emit);plan=json.loads((evidence/'source-expression-plan.json').read_text(encoding='utf8'))
  if hashlib.sha256(native.read_bytes()).hexdigest()!=book_hash or plan['source_book_sha256']!=book_hash or plan['input_xml_sha256']!=hashlib.sha256(score.read_bytes()).hexdigest():raise ValueError('source expression inputs changed during verification')
  fixed,details=apply_verified_expression(root,plan,source);report.update(**details,pending=plan['pending'],source_book_sha256=book_hash,evidence_directory=str(evidence));changed=bool(details['articulation_removals'] or details['wedge_additions']);report['state']=('partially_restored' if plan['pending'] else 'restored') if changed else ('needs_review' if plan['pending'] else 'no_source_expression_changes');emit('演奏记号复核：去除'+str(len(details['articulation_removals']))+'处原图文字/水印误识，恢复'+str(len(details['wedge_additions']))+'处渐强渐弱线');return finish(fixed)
 except InterruptedError:raise
 except Exception as exc:
  report.update(state='unresolved',reason=str(exc));emit('演奏记号复核未通过完整证据校验，保留此前内容');return finish(root)
