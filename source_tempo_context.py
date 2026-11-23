"""Local source-backed tempo refinement with original-XML preservation."""
from pathlib import Path
import os,json,csv,hashlib,shutil,zipfile,xml.etree.ElementTree as E
from tempo_overlay import verify_source_tempo_ledger,merge_verified_tempos

def _sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def _score(path):
 with zipfile.ZipFile(path) as archive:
  container=E.fromstring(archive.read('META-INF/container.xml'));entry=next(e.get('full-path') for e in container.iter() if e.tag.endswith('rootfile'));return E.fromstring(archive.read(entry))

def refine_source_tempos(root,job,settings,device,parts_hint,env,cancel,runner,emit=print,source_job=None):
 job=Path(job);source=Path(source_job) if source_job is not None else job;base=Path(__file__).resolve().parent
 report=dict(state='not_assessed',scope='Native-detected metronome marks only; tempo marks entirely missed by the engine are not assessed',changes=[],review_count=0)
 def finish(tree):
  (job/'source-tempo.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return tree,report
 try:
  native=source/'raw/input.omr';runtime=base/'tempo-runtime';scanner=base/'source_tempos.py'
  if not native.is_file() or not scanner.is_file() or not (runtime/'SourceTempo323.class').is_file():report['reason']='source tempo runtime or native book unavailable';return finish(root)
  engine=Path(settings['engine_root']);python=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe'
  if not python.is_file():report['reason']='local image-analysis Python unavailable';return finish(root)
  emit('核对原图中的速度拍单位')
  evidence=job/'source-tempo-evidence'
  runner([str(python),str(scanner),str(source),str(evidence)],job/'source-tempo-scan.log',env,cancel,timeout=300,emit=emit)
  runner([str(python),str(scanner),str(source),str(evidence),'--validate'],job/'source-tempo-validate.log',env,cancel,timeout=300,emit=emit)
  scan=json.loads((evidence/'scan.json').read_text(encoding='utf8'));report.update(records=len(scan['findings']),source_native_sha256=scan['native_sha256'])
  if not scan['findings']:report['state']='no_native_tempo_records';return finish(root)
  proposals=[f for f in scan['findings'] if f['state']=='source_symbol_proposed'];deferred=[dict(unit=f['unit'],system=f['system'],metro=f['id'],reason=f.get('reason','source symbol uncertain')) for f in scan['findings'] if f['state']!='source_symbol_proposed']
  if not proposals:report.update(state='needs_review',pending=deferred,review_count=len(deferred));return finish(root)
  cp=[str(runtime)];prefix=settings.get('engine_classpath_prefix')
  if prefix:
   prefix=Path(prefix);cp.append(str(prefix if prefix.is_absolute() else base/prefix))
  cp.append(str(engine/'Audiveris-Bandscore/lib/*'));java=engine/settings['java_relative']/'bin/java.exe';native_hash=_sha(native)
  def export(mode):
   folder=job/'source-tempo-native'/mode;folder.mkdir(parents=True,exist_ok=True);shutil.copy2(native,folder/'input.omr')
   args=[str(java),'-Xmx1536m','-Djava.awt.headless=true','--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED','-cp',os.pathsep.join(cp),'SourceTempo323',str(folder/'input.omr'),native_hash,str(source),str(evidence/'plan.tsv'),str(folder),mode,parts_hint]
   runner(args,job/f'source-tempo-{mode}.log',env,cancel,timeout=300,emit=emit)
   if _sha(native)!=native_hash:raise ValueError('original native archive changed during tempo verification')
   return folder
  refined_folder=export('apply')
  with (refined_folder/'decisions.tsv').open(encoding='utf8') as f:decisions=list(csv.DictReader(f,delimiter='\t'))
  report['native_decisions']=decisions;accepted=[d for d in decisions if d['state']=='accepted'];uncertain=[dict(unit=d['unit'],system=d['system'],metro=d['metro'],reason='source glyph classification uncertain') for d in decisions if d['state']=='uncertain']
  report['accepted_native_symbols']=len(accepted)
  if not accepted:
   pending=deferred+uncertain;report.update(state='needs_review' if pending else 'native_units_match_source',pending=pending,review_count=len(pending));return finish(root)
  refined=_score(refined_folder/'result.mxl');ledger=verify_source_tempo_ledger(refined,refined_folder/'tempo-ledger.tsv',scan,decisions)
  control_folder=export('control');control=_score(control_folder/'result.mxl')
  corrected,details=merge_verified_tempos(root,control,refined,ledger)
  exported={(v['unit'],v['system'],v['metro']) for v in ledger.values()};unexported=[dict(unit=d['unit'],system=d['system'],metro=d['metro'],reason='source unit recovered but native rhythm/anchor prevented export') for d in accepted if (d['unit'],d['system'],d['metro']) not in exported]
  pending=deferred+uncertain+unexported;report.update(state='partially_restored' if pending else 'restored',verified_export_records=len(ledger),pending=pending,review_count=len(pending),**details)
  emit(f'原图速度标记复核：已修正{len(details["changes"])}处，{len(pending)}处待核')
  return finish(corrected)
 except InterruptedError:raise
 except Exception as exc:
  report.update(state='unresolved',reason=str(exc));emit('速度标记复核未通过完整校验，已保留此前结果并记录原因');return finish(root)


def _refine_word_tempos(score, job, source, settings, device, env, cancel, runner, emit, native_report):
    """Run the duplicate-print tempo check after the existing native metronome stage."""
    decisions = native_report.get("native_decisions", [])
    if not any(d.get("state") == "accepted" and d.get("top_shape") == "METRO_QUARTER"
               for d in decisions):
        return score, {"state": "no_verified_quarter_reference", "changes": []}

    work = Path(job) / "source-word-tempo"
    work.mkdir()
    input_xml = work / "input.musicxml"
    output_xml = work / "output.musicxml"
    reference = work / "decisions.json"
    evidence = work / "report.json"
    E.ElementTree(score).write(input_xml, encoding="utf-8", xml_declaration=True)
    input_hash = _sha(input_xml)
    reference.write_text(json.dumps(decisions, ensure_ascii=False), encoding="utf-8")
    runtime = "ocr-gpu-env" if device == "gpu" else "ocr-env"
    python = Path(settings["engine_root"]) / runtime / "Scripts/python.exe"
    scanner = Path(__file__).resolve().parent / "source_word_tempo.py"
    try:
        args = [python, scanner, "--source", source, "--evidence", Path(job) / "source-tempo-evidence",
                "--decisions", reference, "--input", input_xml, "--output", output_xml, "--report", evidence]
        runner(list(map(str, args)), Path(job) / "source-word-tempo.log", env, cancel, timeout=120, emit=emit)
        report = json.loads(evidence.read_text(encoding="utf-8"))
        if report["input_sha256"] != input_hash or input_hash != _sha(input_xml) or report["output_sha256"] != _sha(output_xml):
            raise ValueError("速度复核输入或输出已变化")
        report["state"] = "restored" if report["changes"] else "no_supported_word_tempo"
        if report["changes"]:
            emit(f"按原图恢复 {len(report['changes'])} 处误识为文字的速度标记")
            return E.parse(output_xml).getroot(), report
        return score, report
    except InterruptedError:
        raise
    except (OSError, ValueError, RuntimeError, E.ParseError) as exc:
        emit("文字速度复核未完成，已记录原因")
        return score, {"state": "unresolved", "reason": str(exc), "changes": []}


def refine_word_tempos(score, job, source, settings, device, env, cancel, runner, emit, native_report):
    score, details = _refine_word_tempos(
        score, job, source, settings, device, env, cancel, runner, emit, native_report
    )
    if not any(row.get("kind") == "corrected" and row.get("part") == "P1"
               and str(row.get("source", {}).get("unit")) == "1" and row.get("anchor") == 0
               and tuple(row.get("new", ())[:2]) == ("quarter", 0) for row in native_report.get("changes", [])):
        return score, details

    job = Path(job)
    work = job / "source-tempo-return"
    engine = Path(settings["engine_root"])
    python = engine / "ocr-env/Scripts/python.exe"
    if not python.is_file():
        python = engine / "ocr-gpu-env/Scripts/python.exe"
    scanner = Path(__file__).resolve().parent / "source_tempo_return.py"
    model_dir = engine / "cache/paddlex/official_models"
    try:
        from runtime_resources import check_memory
        check_memory(job, "source-tempo-return")
        work.mkdir()
        input_xml = work / "input.musicxml"
        output_xml = work / "output.musicxml"
        report_path = work / "report.json"
        E.ElementTree(score).write(input_xml, encoding="utf-8", xml_declaration=True)
        input_hash = _sha(input_xml)
        args = [python, scanner, source, job, model_dir, input_xml, output_xml, report_path]
        runner(list(map(str, args)), job / "source-tempo-return.log", env, cancel, timeout=160, emit=emit)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report["input_sha256"] != input_hash or input_hash != _sha(input_xml) or report["output_sha256"] != _sha(output_xml):
            raise ValueError("Tempo-return input or output changed")
        details["tempo_return"] = report
        if report["changes"]:
            score = E.parse(output_xml).getroot()
            details["changes"].extend(report["changes"])
            details["state"] = "restored"
            emit(f"按原图补回 {len(report['changes'])} 处回到起始速度的标记")
        return score, details
    except InterruptedError:
        raise
    except (OSError, ValueError, RuntimeError, E.ParseError) as exc:
        details["tempo_return"] = {"state": "unresolved", "reason": str(exc), "changes": []}
        return score, details

