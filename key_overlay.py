from pathlib import Path
import copy,csv,json,hashlib,xml.etree.ElementTree as E
from source_context import merge_verified_clef_export
from tempo_overlay import canonical

def without_key_alter(root):
 root=copy.deepcopy(root)
 for p in root.findall('part'):
  for m in p.findall('measure'):
   for attr in list(m.findall('attributes')):
    for key in list(attr.findall('key')):attr.remove(key)
    if len(attr)==0:m.remove(attr)
   for pitch in m.findall('note/pitch'):
    e=pitch.find('alter')
    if e is not None:pitch.remove(e)
 return canonical(root)

def key_contexts(root):
 result={}
 for p in root.findall('part'):
  count=max([int(e.text) for e in p.findall('measure/attributes/staves')]+[1]);current={i:None for i in range(1,count+1)}
  for m in p.findall('measure'):
   for key in m.findall('attributes/key'):
    ids=[int(key.get('number'))] if key.get('number') else list(current)
    for i in ids:current[i]=int(key.findtext('fifths'))
   for i,v in current.items():result[(p.get('id'),m.get('number'),i)]=v
 return result

def verify_key_ledger(ledger_file,scan,source):
 source=Path(source);supported={(str(f['unit']),str(f['system']),str(f['staff'])):f for f in scan['findings'] if f['state']=='missing_header_key_supported'};scopes={}
 with Path(ledger_file).open(encoding='utf8') as f:rows=list(csv.DictReader(f,delimiter='\t'))
 for row in rows:
  key=(row['unit'],row['system'],row['staff']);proof=supported.get(key)
  if proof is None:raise ValueError('key export lacks source evidence')
  if row['donor_staff']!=proof['donor_staff'] or row['donor_key']!=proof['donor_key'] or int(row['fifths'])!=proof['fifths'] or row['source_sha256']!=proof['source_sha256']:raise ValueError('key export source identity changed')
  if hashlib.sha256((source/f"unit-{int(row['unit']):03d}.png").read_bytes()).hexdigest()!=row['source_sha256']:raise ValueError('source key image changed')
  addr=(row['part'],row['measure'],int(row['staff_number']))
  if addr in scopes:raise ValueError('ambiguous source key address')
  scopes[addr]=row
 for key in supported:
  if not any((r['unit'],r['system'],r['staff'])==key and r['header']=='true' for r in rows):raise ValueError('source key has no exported header address')
 return scopes

def key_alter(step,fifths):
 order='FCGDAEB' if fifths>0 else 'BEADGCF';return (1 if fifths>0 else -1) if step in order[:abs(fifths)] else 0

def merge_verified_keys(original,control,refined,scopes):
 merge_verified_clef_export(control,original)
 if without_key_alter(control)!=without_key_alter(refined):raise ValueError('native key recovery changed unrelated content')
 oldctx=key_contexts(control);newctx=key_contexts(refined)
 for addr,new in newctx.items():
  if oldctx.get(addr)!=new:
   proof=scopes.get(addr)
   if proof is None or int(proof['fifths'])!=new:raise ValueError('unproven effective key change')
 result=copy.deepcopy(original);key_changes=[];pitch_changes=[]
 for old,new,base,dest in zip(control.findall('part'),refined.findall('part'),original.findall('part'),result.findall('part')):
  if len({old.get('id'),new.get('id'),base.get('id')})!=1:raise ValueError('key part identity changed')
  count=max([int(e.text) for e in old.findall('measure/attributes/staves')]+[1]);active={i:None for i in range(1,count+1)}
  for a,b,c,d in zip(old.findall('measure'),new.findall('measure'),base.findall('measure'),dest.findall('measure')):
   number=a.get('number')
   if len({number,b.get('number'),c.get('number')})!=1:raise ValueError('key measure identity changed')
   # Preserve original key elements except those contradicted by an exact source header proof.
   for attr in d.findall('attributes'):
    for key in list(attr.findall('key')):
     ids=[int(key.get('number'))] if key.get('number') else list(active);oldf=int(key.findtext('fifths'));desired={}
     for i in ids:
      addr=(old.get('id'),number,i);proof=scopes.get(addr);desired[i]=int(proof['fifths']) if proof is not None and proof['header']=='true' else oldf
      if desired[i]!=oldf and newctx.get(addr)!=desired[i]:raise ValueError('source key and refined context disagree')
     if any(v!=oldf for v in desired.values()):
      if key.find('cancel') is not None:raise ValueError('explicit key cancellation requires source review')
      if len(set(desired.values()))==1:key.find('fifths').text=str(next(iter(desired.values())))
      else:
       pos=list(attr).index(key);attr.remove(key)
       for i,val in desired.items():k=copy.deepcopy(key);k.set('number',str(i));k.find('fifths').text=str(val);attr.insert(pos,k);pos+=1
      key_changes.append(dict(part=old.get('id'),measure=number,old=oldf,new=desired))
     active.update(desired)
   for i in active:
    addr=(old.get('id'),number,i);proof=scopes.get(addr)
    if proof is not None and proof['header']=='true' and active[i]!=int(proof['fifths']):
     expected=int(proof['fifths'])
     if newctx.get(addr)!=expected:raise ValueError('missing source key context')
     attr=d.find('attributes')
     if attr is None:attr=E.Element('attributes');d.insert(next((j for j,e in enumerate(d) if e.tag!='print'),len(d)),attr)
     key=E.Element('key',{'number':str(i)} if count>1 else {});E.SubElement(key,'fifths').text=str(expected);pos=next((j for j,e in enumerate(attr) if e.tag not in ('divisions','key')),len(attr));attr.insert(pos,key);key_changes.append(dict(part=old.get('id'),measure=number,old=active[i],new={i:expected}));active[i]=expected
   aa=a.findall('note');bb=b.findall('note');cc=c.findall('note');dd=d.findall('note')
   if len({len(aa),len(bb),len(cc)})!=1:raise ValueError('key recovery changed note count')
   for index,(x,y,z,t) in enumerate(zip(aa,bb,cc,dd)):
    ov=x.findtext('pitch/alter','0');nv=y.findtext('pitch/alter','0')
    if ov==nv:continue
    staff=int(x.findtext('staff','1'));addr=(old.get('id'),number,staff);proof=scopes.get(addr)
    if proof is None or x.find('accidental') is not None:raise ValueError('pitch change not justified by source key')
    expected=key_alter(x.findtext('pitch/step'),int(proof['fifths']))
    if int(nv)!=expected or int(ov) not in (0,key_alter(x.findtext('pitch/step'),oldctx.get(addr) or 0)):raise ValueError('unexpected source-key pitch alteration')
    if z.findtext('pitch/alter','0')!=ov:raise ValueError('original pitch differs from control')
    pitch=t.find('pitch');alter=pitch.find('alter')
    if alter is None:alter=E.Element('alter');pitch.insert(1,alter)
    alter.text=nv;pitch_changes.append(dict(part=old.get('id'),measure=number,note=index,step=x.findtext('pitch/step'),old=ov,new=nv))
 return result,dict(key_changes=key_changes,pitch_alter_changes=pitch_changes,all_original_nonkey_nonalter_content_preserved=True)
