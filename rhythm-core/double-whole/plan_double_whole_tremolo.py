from pathlib import Path
from hashlib import sha256
from zipfile import ZipFile
from xml.etree import ElementTree as E
import json,sys
import numpy as np
import cv2
from scan_double_whole_tremolo import scan,bounds

book, source, out = map(Path,sys.argv[1:])
findings=scan(book,source)
with ZipFile(book) as z, out.open("x",encoding="utf8") as stream:
    for f in findings:
        unit=f["unit"]; system=f["system"]
        doc=E.fromstring(z.read(f"sheet#{unit}/sheet#{unit}.xml"))
        sy=doc.find(f"page/system[@id='{system}']")
        nodes={n.get("id"):n for n in sy.findall("sig/inters/*")}
        image=cv2.imdecode(np.fromfile(source/f"unit-{unit:03d}.png",np.uint8),0)
        for staff in f["staves"]:
            main=[nodes[i] for i in staff["native_heads"][0]]
            follow=sorted([nodes[i] for i in staff["native_heads"][1]],key=lambda n:bounds(n)[1])
            donor="0"; box=[0,0,0,0]; sp=staff["interline"]
            if len(follow)==2:
                a,b=map(bounds,follow)
                mid=(a[1]+b[1])/2
                peers=[n for n in main if abs(bounds(n)[1]-mid)<.16*sp]
                if len(peers)==1 and abs(a[0]-b[0])<=1 and abs(b[1]-a[1]-2*sp)<.25*sp:
                    ref=peers[0]
                    x,y,w,h=bounds(ref)
                    xx,yy=round((a[0]+b[0])/2)-4,round(mid)-4
                    exemplar=image[y:y+h,x:x+w]
                    test=image[yy:yy+h+8,xx:xx+w+8]
                    positions=[]
                    for cut in (160,208):
                        aa=(exemplar<cut).astype("uint8")
                        bb=(test<cut).astype("uint8")
                        _,similarity,_,(gx,gy)=cv2.minMaxLoc(cv2.matchTemplate(bb,aa,cv2.TM_CCOEFF_NORMED))
                        actual=bb[gy:gy+h,gx:gx+w]
                        hit=np.logical_and(aa,actual).sum()
                        if similarity>=.91 and hit/actual.sum()>=.96 and hit/aa.sum()>=.97:
                            positions.append((gx+xx,gy+yy,w,h))
                    if len(positions)==2 and positions[0]==positions[1]:
                        donor=ref.get("id"); box=list(positions[0])
            row=[unit,system,f["stack"],staff["staff"],*staff["chords"],f["source_sha256"],donor,*box]
            for cut in (160,208):
                view=staff["thresholds"][cut]
                row.extend([view["left"],view["right"],view["slope"],*view["bands"][0],*view["bands"][1]])
            stream.write("\t".join(map(str,row))+"\n")
print("SOURCE_DOUBLE_WHOLE_PLAN",len(findings))
