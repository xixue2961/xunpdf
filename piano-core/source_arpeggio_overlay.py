"""Add only new source-proven arpeggiate notations after context restoration."""
from pathlib import Path
import copy,json,os,xml.etree.ElementTree as E,zipfile

def canon(n):return n.tag,tuple(sorted(n.attrib.items())),(n.text or '').strip(),tuple(canon(c) for c in n)
def cooked(root):
 import xunpdf
 from musicxml_compat import normalize_chord_beams
 from musicxml_ties import normalize_tie_chain_order
 n=xunpdf.normalize_export(copy.deepcopy(root));normalize_chord_beams(n);normalize_tie_chain_order(n);return n

def verify_native_arps(before,after):
 if len(before.findall('part'))!=len(after.findall('part')) or canon(before.find('part-list'))!=canon(after.find('part-list')):raise ValueError('arpeggio export changed part mapping')
 stripped=copy.deepcopy(after);changes=[]
 for a,b,s in zip(before.findall('part'),after.findall('part'),stripped.findall('part')):
  if a.get('id')!=b.get('id') or len(a.findall('measure'))!=len(b.findall('measure')):raise ValueError('arpeggio source address changed')
  for ma,mb,ms in zip(a.findall('measure'),b.findall('measure'),s.findall('measure')):
   if ma.get('number')!=mb.get('number'):raise ValueError('arpeggio source bar identity changed')
   if canon(ma)==canon(mb):continue
   if len(ma.findall('note'))!=len(mb.findall('note')):raise ValueError('arpeggio export changed note count')
   for idx,(na,nb,ns) in enumerate(zip(ma.findall('note'),mb.findall('note'),ms.findall('note'))):
    if canon(na)==canon(nb):continue
    arps=ns.findall('notations/arpeggiate')
    if na.find('notations/arpeggiate') is not None or len(arps)!=1:raise ValueError('arpeggio recovery changed an existing mark')
    mark=copy.deepcopy(arps[0]);number=mark.get('number')
    if number is None or int(number)!=int(ns.findtext('staff','1')):raise ValueError('independent arpeggio number does not match source staff')
    ns.find('notations').remove(arps[0])
    if len(ns.find('notations'))==0:ns.remove(ns.find('notations'))
    changes.append(dict(part=a.get('id'),measure=ma.get('number'),note_index=idx,mark=mark))
   if canon(ma)!=canon(ms):raise ValueError('arpeggio native export changed other musical fields')
 return changes

def merge_source_arps(original,before,after):
 changes=verify_native_arps(before,after);old=cooked(before);new=cooked(after);base=cooked(original);fixed=copy.deepcopy(original);pending=[];applied=[];already=[];accepted=set()
 for change in changes:
  scope=change['part'],change['measure']
  if scope in accepted:continue
  pid,mid=scope;find=lambda t:t.find(f"part[@id='{pid}']/measure[@number='{mid}']")
  if find(base) is None:raise ValueError('source arpeggio target missing')
  if canon(find(base))==canon(find(new)):already.append(scope);accepted.add(scope);continue
  if canon(find(base))!=canon(find(old)):pending.append(dict(part=pid,measure=mid,reason='post-context target differs from complete native control'));continue
  accepted.add(scope)
  rawnotes=find(fixed).findall('note');normalized=find(base).findall('note')
  if len(rawnotes)!=len(normalized):raise ValueError('normalization changed arpeggio note addresses')
  for c in [v for v in changes if (v['part'],v['measure'])==scope]:
   n=rawnotes[c['note_index']];nc=normalized[c['note_index']]
   for field in ['pitch/step','pitch/alter','pitch/octave','voice','staff','type']:
    if n.findtext(field)!=nc.findtext(field):raise ValueError('arpeggio normalization changed musical note order')
   if n.find('notations/arpeggiate') is not None:raise ValueError('arpeggio target already marked')
   notation=n.find('notations')
   if notation is None:
    notation=E.Element('notations');idx=next((i for i,e in enumerate(n) if e.tag in ['lyric','play','listen']),len(n));n.insert(idx,notation)
   notation.append(copy.deepcopy(c['mark']));applied.append(dict(part=pid,measure=mid,note_index=c['note_index'],number=c['mark'].get('number')))
 return fixed,dict(applied=applied,pending=pending,already_present=already,native_only_added_arps=True)

def recover_source_arps(root,source,native,whole_scan,out,app,settings,device,env,cancel,runner,emit=print):
 import xunpdf
 from runtime_resources import check_memory
 from bounded_process import run_bounded
 pkg=Path(__file__).resolve().parent;out=Path(out);out.mkdir(parents=True,exist_ok=True);engine=Path(settings['engine_root']);python=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe';java=engine/settings['java_relative']/'bin/java.exe';evidence=out/'evidence';emit('核对钢琴独立琶音波线')
 def run(args,name):return run_bounded(runner,list(map(str,args)),out/(name+'.log'),env,cancel,timeout=300,emit=emit)
 run([python,pkg/'source_arpeggios.py','--job',source,'--heads',whole_scan,'--out',evidence],'scan');findings=json.loads((evidence/'source-arpeggios.json').read_text(encoding='utf8'))['findings'];lines=[];skipped=[]
 with zipfile.ZipFile(native) as z:
  for f in findings:
   rt=E.fromstring(z.read(f"sheet#{f['unit']}/sheet#{f['unit']}.xml"));sy=next(s for s in rt.findall('page/system') if s.get('id')==f['system']);x,y,w,h=f['complete_source_bounds'];overlap=[]
   for n in sy.findall('sig/inters/arpeggiato'):
    b=n.find('bounds');bx,by,bw,bh=[int(b.get(k)) for k in ['x','y','w','h']]
    if min(x+w,bx+bw)>max(x,bx) and min(y+h,by+bh)>max(y,by):overlap.append(n.get('id'))
   if overlap:skipped.append(dict(unit=f['unit'],system=f['system'],staff=f['staff'],existing_arpeggios=overlap));continue
   lines.append('\t'.join(map(str,[f['unit'],f['system'],f['native_measure'],f['staff'],*f['complete_source_bounds'],f['donor_unit'],f['donor'],f['source_sha256']])))
 (out/'plan.tsv').write_text('\n'.join(lines)+('\n' if lines else ''),encoding='utf8')
 if not lines:
  report=dict(state='no_missing_source_arpeggios',applied=[],pending=[],skipped_existing=skipped);(out/'source-arpeggio-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return root,report
 prefix=Path(settings.get('engine_classpath_prefix','engine-classes'));prefix=prefix if prefix.is_absolute() else Path(app)/prefix;basecp=[str(pkg/'runtime'),str(prefix),str(engine/'Audiveris-Bandscore/lib/*')]
 def jrun(cls,argv,name,arp=False):
  check_memory(out,name);cp=([str(pkg/'arpeggio-runtime')] if arp else [])+basecp;return run([java,'-Xmx1536m','-Djava.awt.headless=true','-XX:ActiveProcessorCount=4','--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED','-cp',os.pathsep.join(cp),'org.audiveris.omr.score.'+cls,*argv],name)
 control=out/'control';refined=out/'native';jrun('PianoExport357',[native,source,control],'control');jrun('SourceArpeggio347',[native,source,out/'plan.tsv',refined],'native',arp=True);before=xunpdf.read_score(control/'result.mxl');after=xunpdf.read_score(refined/'result.mxl');changes=verify_native_arps(before,after)
 if len(changes)!=len(lines):raise ValueError('source arpeggio plan was not completely exported')
 fixed,details=merge_source_arps(root,before,after);report=dict(state='partially_restored' if details['pending'] else 'restored',source_candidates=len(findings),skipped_existing=skipped,**details);(out/'source-arpeggio-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');emit('已恢复'+str(len(details['applied']))+'处原图琶音标记');return fixed,report
