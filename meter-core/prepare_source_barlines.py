from pathlib import Path
import zipfile,xml.etree.ElementTree as E,json,hashlib
import numpy as np
from PIL import Image

def prepare_bars(job,out):
 job=Path(job);out=Path(out);rows=[];evidence=[]
 with zipfile.ZipFile(job/'raw/input.omr') as z:
  for name in z.namelist():
   if not __import__('re').fullmatch(r'sheet#\d+/sheet#\d+\.xml',name):continue
   unit=int(name.split('/')[0][6:]);sheet=E.fromstring(z.read(name));path=job/f'unit-{unit:03d}.png';digest=hashlib.sha256(path.read_bytes()).hexdigest();gray=None
   for sy in sheet.findall('page/system'):
    nodes={n.get('id'):n for n in sy.findall('sig/inters/*')};stacks={s.get('id'):s for s in sy.findall('stack')}
    for part in sy.findall('part'):
     for m in part.findall('measure'):
      bar=m.find('right-barline')
      if bar is None:continue
      refs=bar.findtext('staff-barlines','').split();present=[nodes[i].get('staff') for i in refs if i in nodes]
      for st in part.findall('staff'):
       if st.get('id') in present:continue
       # Only a dangling native reference at an established measure boundary.
       if not refs or all(i in nodes for i in refs):raise ValueError('missing barline is not a dangling source reference')
       stack=stacks[m.get('id')];x=int(stack.get('right'));ys=[float(e.find('point').get('y')) for e in st.findall('lines/line')];il=(ys[-1]-ys[0])/4;y0=round(ys[0]);y1=round(ys[-1])+1
       peers=[n for n in nodes.values() if n.tag=='staff-barline' and abs(int(n.find('bounds').get('x'))-x)<.4*il]
       if len({n.get('staff') for n in peers})<2:raise ValueError('barline lacks other printed staff anchors')
       if gray is None:
        with Image.open(path) as im:gray=np.asarray(im.convert('L'))
       left=x-round(.45*il);right=x+round(.45*il)+1;ink=gray[y0:y1,left:right]<160;coverage=ink.mean(axis=0);cols=np.flatnonzero(coverage>=.95)
       if not len(cols) or any(b-a!=1 for a,b in zip(cols,cols[1:])) or len(cols)>.5*il:raise ValueError('source vertical barline not uniquely proved')
       bx=left+int(cols[0]);bw=len(cols)
       rows.append('\t'.join(map(str,[unit,sy.get('id'),m.get('id'),st.get('id'),bx,y0,bw,y1-y0,digest])))
       evidence.append(dict(unit=unit,system=sy.get('id'),stack=m.get('id'),staff=st.get('id'),dangling_refs=refs,bounds=[bx,y0,bw,y1-y0],minimum_column_coverage=float(min(coverage[cols])),source_sha256=digest))
 out.mkdir(parents=True,exist_ok=True);(out/'barlines.tsv').write_text('\n'.join(rows)+('\n' if rows else ''),encoding='utf8');(out/'source-barline-plan.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf8');return evidence
if __name__=='__main__':
 import sys;print(prepare_bars(sys.argv[1],sys.argv[2]))
