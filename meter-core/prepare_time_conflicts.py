from pathlib import Path
import json,zipfile,xml.etree.ElementTree as E

def prepare_conflicts(job,plan,out):
 job=Path(job);out=Path(out);lines=[];pending=[];seen=set()
 with zipfile.ZipFile(job/'raw/input.omr') as z:
  sheets={}
  for t in plan['rows']:
   u,sid,mid,staff=t['unit'],t['system'],t['stack'],t['staff'];x,y,w,h=t['bounds'];il=h/4
   if u not in sheets:sheets[u]=E.fromstring(z.read(f'sheet#{u}/sheet#{u}.xml'))
   sy=sheets[u].find(f'page/system[@id="{sid}"]');nodes={n.get('id'):n for n in sy.findall('sig/inters/*')};rels=sy.findall('sig/relations/relation')
   for chord in sy.findall('sig/inters/head-chord'):
    if chord.get('staff')!=staff:continue
    ids=[v.get('target') for v in rels if v.get('source')==chord.get('id') and v.find('containment') is not None];heads=[nodes[i] for i in ids if i in nodes and nodes[i].tag=='head']
    if not heads:continue
    boxes=[[int(n.find('bounds').get(k)) for k in ('x','y','w','h')] for n in heads]
    overlaps=any(max(x,a)<min(x+w,a+c) and max(y,b)<min(y+h,b+d) for a,b,c,d in boxes)
    if not overlaps:continue
    key=(u,sid,chord.get('id'))
    if key in seen:raise ValueError('duplicate source-time chord claim')
    seen.add(key)
    if len(heads)!=len(ids) or any(n.get('staff')!=staff for n in heads) or not all(x-.25*il<=a+c/2<=x+w+.25*il and y-.25*il<=b+d/2<=y+h+.25*il for a,b,c,d in boxes):pending.append(dict(unit=u,system=sid,chord=chord.get('id'),reason='chord_has_non_time_region_members'));continue
    bx=min(b[0] for b in boxes);by=min(b[1] for b in boxes);right=max(b[0]+b[2] for b in boxes);bottom=max(b[1]+b[3] for b in boxes);num,den=t['time'].split('/')
    lines.append('\t'.join(map(str,[u,sid,mid,staff,num,den,x,y,w,h,bx,by,right-bx,bottom-by,chord.get('id'),','.join(ids)])))
 out.mkdir(parents=True,exist_ok=True);(out/'conflicts.tsv').write_text('\n'.join(lines)+('\n' if lines else ''),encoding='utf8');(out/'conflict-plan.json').write_text(json.dumps(dict(chords=len(lines),pending=pending),ensure_ascii=False,indent=2),encoding='utf8');return len(lines),pending
