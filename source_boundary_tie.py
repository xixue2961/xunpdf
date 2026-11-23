"""Transfer only source-bound, same-pitch cross-system tie endpoints."""
from pathlib import Path
import copy,zipfile,xml.etree.ElementTree as E
from fractions import Fraction as F
from source_curved_rest_overlay import canonical,sha,addresses
from tuplet_overlay import times

def strip_ties(n):
 n=copy.deepcopy(n)
 for tie in list(n.findall('tie')):n.remove(tie)
 for notation in list(n.findall('notations')):
  for tied in list(notation.findall('tied')):notation.remove(tied)
  if not len(notation) and not notation.attrib:n.remove(notation)
 return n

def neutral(n,div):
 n=copy.deepcopy(n)
 for d in n.findall('duration'):d.text=str(F(d.text)/div)
 for lyric in n.findall('lyric'):lyric.attrib.pop('default-y',None)
 return canonical(n)

def source_meter(q,book,meterplan):
 with zipfile.ZipFile(book) as z:sy=E.fromstring(z.read(f"sheet#{q['unit']}/sheet#{q['unit']}.xml")).find(f"page/system[@id='{q['system']}']")
 owners=[(part,i) for part in sy.findall('part') for i,m in enumerate(part.findall('measure')) if q['anchor']['chord'] in m.findtext('head-chords','').split()]
 if len(owners)!=1:raise ValueError('source chord owner is ambiguous')
 part,index=owners[0];stacks=sy.findall('stack');order={s.get('id'):i for i,s in enumerate(stacks) if s.get('special')!='CAUTIONARY'}
 relevant=[v for v in meterplan['rows'] if v['unit']==q['unit'] and v['system']==q['system'] and v['staff']==q['staff'] and v['score']>=.8 and v['stack'] in order and order[v['stack']]<=index]
 if not relevant:raise ValueError('source printed meter witness missing')
 latest=max(order[v['stack']] for v in relevant);own=[v for v in relevant if order[v['stack']]==latest]
 if len(own)!=1:raise ValueError('source printed meter ambiguous')
 own=own[0];peers=[v for v in meterplan['rows'] if v['unit']==q['unit'] and v['system']==q['system'] and v['stack']==own['stack'] and v['time']==own['time'] and v['score']>=.8]
 if len({v['staff'] for v in peers})<2:raise ValueError('independent printed meter staff missing')
 beats,unit=map(int,own['time'].split('/'));duration=F(beats*4,unit)
 if F(stacks[index].get('expected'))*4!=duration:raise ValueError('native expected meter disagrees with source')
 return duration

def repair(caller,control,refined,proof,cr,rr,meterplan):
 if sha(proof['book'])!=proof['book_sha256'] or meterplan['source_images']!=proof['source_images']:raise ValueError('source graph/meter provenance changed')
 for u,h in proof['source_images'].items():
  if sha(Path(proof['images'])/f'unit-{int(u):03d}.png')!=h:raise ValueError('source image changed')
 old,od=addresses(control);new,nd=addresses(refined);ms,ds=addresses(caller);out=copy.deepcopy(caller);dest,_=addresses(out);expected={};report=[];inserted=[]
 for pair in proof['pairs']:
  selected=[]
  for side,kind in (('before','start'),('after','stop')):
   q=pair[side];a=q['anchor'];k=(pair['part'],a['measure']);matches=[]
   for rows in (cr,rr):
    hit=[v for v in rows if v['unit']==str(q['unit']) and v['system']==q['system'] and v['head']==a['head'] and v['part']==pair['part'] and v['measure']==a['measure'] and v['repeat_copy']=='false']
    if len(hit)!=1:raise ValueError('source tie endpoint has no unique physical ledger')
    row=hit[0]
    if row['implicit']!='false' or row['native_note_class']!='HeadInter' or row['glyph']!=a['head_glyph'] or row['source_sha256']!=q['source_sha256'] or [int(row[v]) for v in ('x','y','w','h')]!=a['head_bounds']:raise ValueError('source tie head identity changed')
    matches.append(row)
   if matches[0]!=matches[1]:raise ValueError('tie repair moved physical head')
   idx=int(matches[0]['note_index']);before=old[k].findall('note')[idx];after=new[k].findall('note')[idx];target=ms[k].findall('note')[idx]
   if expected.setdefault((k,idx),kind)!=kind:raise ValueError('source endpoint collision')
   if before.find('tie') is not None or before.find('notations/tied') is not None:raise ValueError('existing tie chain requires combined repair')
   if [v.get('type') for v in after.findall('tie')]!=[kind] or [v.get('type') for v in after.findall('notations/tied')]!=[kind]:raise ValueError('unexpected native tie definition')
   if neutral(before,od[k])!=neutral(strip_ties(after),nd[k]) or neutral(strip_ties(target),ds[k])!=neutral(before,od[k]):raise ValueError('caller/native tie note changed')
   if target.findtext('pitch/step')!=a['step'] or int(target.findtext('pitch/octave'))!=a['octave']:raise ValueError('caller note differs from source pitch')
   t=times(ms[k],ds[k])[idx];span=source_meter(q,proof['book'],meterplan)
   if side=='before' and t[0]+t[1]!=span or side=='after' and t[0]!=0:raise ValueError('source tie notes are not temporally adjacent')
   if max(v[0]+v[1] for v in times(ms[k],ds[k]))!=span:raise ValueError('caller measure does not close at source meter')
   selected.append((k,idx,target,t))
   fixed=dest[k].findall('note')[idx]
   if neutral(target,ds[k])==neutral(after,nd[k]):state='already_present'
   elif canonical(target)==canonical(strip_ties(target)):
    for tie in after.findall('tie'):fixed.insert(list(fixed).index(fixed.find('duration'))+1,copy.deepcopy(tie))
    if after.findall('notations/tied'):
     notation=fixed.find('notations')
     if notation is None:
      notation=E.Element('notations');i=next((i for i,v in enumerate(fixed) if v.tag in ('lyric','play','listen')),len(fixed));fixed.insert(i,notation)
     for tied in after.findall('notations/tied'):notation.append(copy.deepcopy(tied))
    state='inserted';inserted.append((k,idx))
   else:raise ValueError('caller conflicting tie preserved')
   report.append(dict(part=k[0],measure=k[1],note_index=idx,physical_head=a['head'],kind=kind,onset=str(t[0]),duration=str(t[1]),source_meter_quarters=str(span),state=state))
  a,b=selected
  if a[2].findtext('voice','1')!=b[2].findtext('voice','1') or a[2].findtext('staff','1')!=b[2].findtext('staff','1') or canonical(a[2].find('pitch'))!=canonical(b[2].find('pitch')):raise ValueError('cross-system tie voice/staff/sounding pitch differs')
 # Native export must differ only at the two proven tie endpoints, including metadata.
 restored=copy.deepcopy(refined);rm,_=addresses(restored)
 for (k,idx),kind in expected.items():
  note=rm[k].findall('note')[idx];parent=rm[k];pos=list(parent).index(note);parent.remove(note);parent.insert(pos,copy.deepcopy(old[k].findall('note')[idx]))
 if canonical(restored)!=canonical(control):raise ValueError('unrelated native export content changed')
 restored=copy.deepcopy(out);rm,_=addresses(restored)
 for k,idx in inserted:
  n=rm[k].findall('note')[idx];p=rm[k];pos=list(p).index(n);p.remove(n);p.insert(pos,copy.deepcopy(ms[k].findall('note')[idx]))
 if canonical(restored)!=canonical(caller):raise ValueError('unrelated caller content changed')
 return out,dict(changes=report,all_other_caller_content_preserved=True,production_installed=False)
