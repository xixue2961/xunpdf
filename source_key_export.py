"""Transfer only the verified exporter fix for spurious key-void events.
Explicit native key events, caller music, lyrics and expression are preserved.
"""
from pathlib import Path
import csv,copy,hashlib,zipfile,xml.etree.ElementTree as E
from key_overlay import without_key_alter,key_contexts,key_alter
from source_expression_overlay import canonical
from source_lyric_roles import _score_events

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def key_nodes(m):return [k for a in m.findall('attributes') for k in a.findall('key')]
def key_signature(m):return [canonical(k) for k in key_nodes(m)]
def base_music(event):
 n=event['node'];p=n.find('pitch')
 return (event['start'],event['duration'],None if p is None else (p.findtext('step'),p.findtext('octave')),n.findtext('voice','1'),n.findtext('staff','1'),n.find('chord') is not None,n.find('grace') is not None,canonical(n.find('accidental')) if n.find('accidental') is not None else None)

def merge_verified_export_keys(original,control,refined,book,source,control_ledger,refined_ledger,expected_native_sha):
 book=Path(book);source=Path(source)
 if sha(book)!=expected_native_sha:raise ValueError('source book changed')
 if Path(control_ledger).read_bytes()!=Path(refined_ledger).read_bytes():raise ValueError('native physical note export map changed')
 if without_key_alter(control)!=without_key_alter(refined):raise ValueError('native key fix changed unrelated content')
 oldctx=key_contexts(control);newctx=key_contexts(refined);callerctx=key_contexts(original);affected={k for k,v in oldctx.items() if newctx.get(k)!=v}
 rows=list(csv.DictReader(Path(control_ledger).open(encoding='utf8'),delimiter='\t'));bymeasure={};byaddr={}
 for row in rows:
  if row['repeat_copy']!='false':continue
  bymeasure.setdefault((row['part'],row['measure']),[]).append(row);addr=(row['part'],row['measure'],int(row['note_index']))
  if addr in byaddr:raise ValueError('ambiguous native note address')
  byaddr[addr]=row
 source_units={};image_hashes={}
 changed_headers={(p.get('id'),m.get('number')) for p,q in zip(control.findall('part'),refined.findall('part')) for m,n in zip(p.findall('measure'),q.findall('measure')) if key_signature(m)!=key_signature(n)}
 with zipfile.ZipFile(book) as z:
  for key in affected:
   pid,mid,staffnum=key
   if (pid,mid) not in changed_headers:continue  # Later empty measures inherit the proven header context.
   relevant=[v for v in bymeasure.get((pid,mid),[]) if int(v['staff_number'])==staffnum]
   if not relevant:raise ValueError('key context has no native source anchor')
   for row in relevant:
    unit=int(row['unit']);sid=row['system'];staff=row['staff']
    if unit not in source_units:source_units[unit]=E.fromstring(z.read(f'sheet#{unit}/sheet#{unit}.xml'))
    sy=next(s for s in source_units[unit].findall('page/system') if s.get('id')==sid);st=next(s for s in sy.findall('part/staff') if s.get('id')==staff)
    if st.find('header') is not None and st.find('header').get('key') is not None:raise ValueError('existing native header key must not be treated as absent')
    if any(n.tag=='key' and n.get('staff')==staff for n in sy.find('sig/inters')):raise ValueError('explicit native key event preserved')
    if unit not in image_hashes:image_hashes[unit]=sha(source/f'unit-{unit:03d}.png')
    if image_hashes[unit]!=row['source_sha256']:raise ValueError('source key pixels changed')
 result=copy.deepcopy(original);ctrl_ev,_=_score_events(control);new_ev,_=_score_events(refined);caller_ev,_=_score_events(original);dest_ev,_=_score_events(result);changes=[];covered=[]
 for p,q,base,dest in zip(control.findall('part'),refined.findall('part'),original.findall('part'),result.findall('part')):
  if len({p.get('id'),q.get('id'),base.get('id'),dest.get('id')})!=1 or len({len(t.findall('measure')) for t in (p,q,base,dest)})!=1:raise ValueError('key part/measure identity changed')
  for a,b,c,d in zip(p.findall('measure'),q.findall('measure'),base.findall('measure'),dest.findall('measure')):
   pid=p.get('id');mid=a.get('number')
   if len({mid,b.get('number'),c.get('number'),d.get('number')})!=1:raise ValueError('key measure identity changed')
   if key_signature(a)!=key_signature(b):
    oldkeys=key_nodes(a);newkeys=key_nodes(b)
    if len(oldkeys)!=1 or newkeys or oldkeys[0].findtext('fifths')!='0' or oldkeys[0].find('cancel') is not None:raise ValueError('not a proven key-void removal')
    passed_music=False
    for child in a:
     if child.tag in ('note','backup','forward'):passed_music=True
     if passed_music and child.find('key') is not None:raise ValueError('mid-measure key change preserved')
    scopes=[k for k in affected if k[:2]==(pid,mid)]
    if not scopes or any(newctx[k]==0 for k in scopes):raise ValueError('key-void removal lacks inherited nonzero context')
    if key_signature(c)==key_signature(a):
     for attr in list(d.findall('attributes')):
      for k in list(attr.findall('key')):attr.remove(k)
      if len(attr)==0:d.remove(attr)
     changes.append(dict(kind='remove_spurious_key_void',part=pid,measure=mid,old=0,new={str(k[2]):newctx[k] for k in scopes}))
    elif all(callerctx.get(k)==newctx[k] for k in scopes):covered.append(dict(kind='key_context_already_correct',part=pid,measure=mid))
    else:raise ValueError('caller key differs from both validated states')
 for addr,old in ctrl_ev.items():
  new=new_ev[addr];ov=old['node'].findtext('pitch/alter','0');nv=new['node'].findtext('pitch/alter','0')
  if ov==nv:continue
  row=byaddr.get(addr)
  if row is None or row['native_note_class']!='HeadInter' or row['implicit']!='false':raise ValueError('changed alter lacks a real physical source head')
  x=caller_ev.get(addr);dest=dest_ev.get(addr)
  if x is None or dest is None:raise ValueError('caller source note missing')
  step=new['node'].findtext('pitch/step');scope=(addr[0],addr[1],int(new['node'].findtext('staff','1')))
  if scope not in affected or new['node'].find('accidental') is not None or int(nv)!=key_alter(step,newctx[scope]):raise ValueError('explicit or unrelated accidental change refused')
  current=x['node'].findtext('pitch/alter','0')
  if current==nv:
   if x['node'].findtext('pitch/step')!=step:raise ValueError('caller source note step changed')
   covered.append(dict(kind='alter_already_correct',part=addr[0],measure=addr[1],note=addr[2]));continue
  if current!=ov or base_music(x)!=base_music(old):raise ValueError('caller pitch/time/voice or accidental changed')
  pitch=dest['node'].find('pitch');alter=pitch.find('alter')
  if int(nv)==0:
   if alter is not None:pitch.remove(alter)
  else:
   if alter is None:alter=E.Element('alter');pitch.insert(1,alter)
   alter.text=nv
  changes.append(dict(kind='source_key_alter',part=addr[0],measure=addr[1],note=addr[2],head=row['head'],unit=int(row['unit']),old=ov,new=nv,step=step))
 if without_key_alter(original)!=without_key_alter(result):raise ValueError('key transfer changed other caller fields')
 finalctx=key_contexts(result)
 for k in affected:
  if finalctx.get(k)!=newctx[k]:raise ValueError('unresolved inherited key context')
 report=dict(changes=changes,covered=covered,source_native_sha256=expected_native_sha,all_caller_nonkey_nonalter_fields_exact=True,all_note_counts_durations_onsets_voices_lyrics_and_expression_preserved=True)
 if not changes:return original,report
 return result,report