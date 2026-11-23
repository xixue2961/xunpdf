from pathlib import Path
import sys,json,zipfile,xml.etree.ElementTree as E,hashlib,math
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));from source_tuplet_meters import scan_source_meters

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def prepare(job,out):
 job=Path(job);out=Path(out);out.mkdir(parents=True,exist_ok=True);book=job/'raw/input.omr';before=sha(book);scan=scan_source_meters(job,out/'source-scan');hashes={v['unit']:v['image_sha256'] for v in scan['source_images']};rows=[];pending=[];changes=[]
 with zipfile.ZipFile(book) as z:
  sheets={}
  for finding in scan['results']:
   unit=finding['unit'];sid=finding['system']
   if unit not in sheets:sheets[unit]=E.fromstring(z.read(f'sheet#{unit}/sheet#{unit}.xml'))
   sy=next(s for s in sheets[unit].findall('page/system') if s.get('id')==sid);staves={s.get('id'):s for s in sy.findall('part/staff')};stacks=sy.findall('stack')
   for column in finding['source_columns']:
    strong=[v for v in column['votes'] if v['score']>=.80]
    if column['conflicting_time_at_same_column'] or len(strong)<max(2,math.ceil(len(staves)*2/3)):
     pending.append(dict(unit=unit,system=sid,x=column['x'],reason='source_meter_consensus_not_strong_enough'));continue
    if column['kind']=='courtesy':continue
    if column['native_stack'] is not None:stack=next(s for s in stacks if s.get('id')==column['native_stack']['id'])
    else:
     # An unmapped header must precede every first-stack musical head.
     first=stacks[0];heads=[e for e in sy.findall('sig/inters/head') if int(e.find('bounds').get('x'))<int(first.get('right'))]
     if not heads or not all(column['x']<int(h.find('bounds').get('x')) for h in heads):pending.append(dict(unit=unit,system=sid,x=column['x'],reason='source_header_not_before_music'));continue
     stack=first
    num,den=map(int,column['time'].split('/'))
    for vote in column['votes']:
     if vote['score']<.75:continue
     staff=staves[vote['staff']];ys=[float(e.find('point').get('y')) for e in staff.findall('lines/line')];space=(ys[-1]-ys[0])/4
     box=[int(vote['x']),round(ys[0]),max(1,round(1.7*space)),max(1,round(4*space))]
     row=[unit,sid,stack.get('id'),vote['staff'],num,den,*box,hashes[unit]];rows.append('\t'.join(map(str,row)));changes.append(dict(unit=unit,system=sid,stack=stack.get('id'),staff=vote['staff'],time=column['time'],bounds=box,score=vote['score']))
 if sha(book)!=before:raise ValueError('source book changed during scan')
 for unit,digest in hashes.items():
  if sha(job/f'unit-{unit:03d}.png')!=digest:raise ValueError('source image changed during scan')
 plan=dict(book_sha256=before,source_images=hashes,rows=changes,pending=pending,scope='printed source-consensus time signatures; no inferred meter from note sums')
 (out/'times.tsv').write_text('\n'.join(rows)+('\n' if rows else ''),encoding='utf8');(out/'source-time-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8');return plan
if __name__=='__main__':
 p=prepare(sys.argv[1],sys.argv[2]);print('SOURCE_TIME_PLAN',len(p['rows']),'pending',len(p['pending']))
