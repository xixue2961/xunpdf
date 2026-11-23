"""Verify the difference between one and two flags, and partial beam ink."""
from functools import lru_cache
from hashlib import sha256
from itertools import product
from pathlib import Path
from statistics import correlation
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from PIL import Image


class SourceMarks:
    def __init__(self, job, book):
        self.job=Path(job)
        self.archive=ZipFile(book)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, value, traceback):
        self.archive.close()
        self.graph.cache_clear()
        self.pixels.cache_clear()

    @lru_cache(maxsize=16)
    def graph(self, unit, system):
        sheet=ET.fromstring(self.archive.read(f"sheet#{unit}/sheet#{unit}.xml"))
        sig=sheet.find(f"page/system[@id='{system}']/sig")
        return {n.get("id"):n for n in sig.find("inters")},list(sig.find("relations"))

    @lru_cache(maxsize=4)
    def pixels(self, unit, digest):
        path=self.job/f"unit-{unit:03d}.png"
        if sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError("original notation pixels differ from accepted ledger")
        with Image.open(path) as im:
            return im.convert("L")

    def feature(self, row):
        unit=int(row["unit"])
        nodes,relations=self.graph(unit,row["system"])
        chord=row["chord"]
        stem_ids=[r.get("target") for r in relations
                  if r.get("source")==chord and r.find("chord-stem") is not None]
        if len(stem_ids)!=1:
            return None
        [stem_id]=stem_ids
        stem=nodes[stem_id]
        points=[(float(v.get("x")),float(v.get("y"))) for v in stem.findall("median/*")]
        if len(points)!=2:
            return None
        note_y=int(row["y"])+int(row["h"])/2
        end=max(points,key=lambda point:abs(point[1]-note_y))
        direction="down" if end[1]>note_y else "up"
        flags=[nodes[r.get("source")] for r in relations
               if r.get("target")==stem_id and r.find("flag-stem") is not None]
        beams=[nodes[r.get("source")] for r in relations
               if r.get("target")==stem_id and r.find("beam-stem") is not None]
        im=self.pixels(unit,row["source_sha256"])
        return dict(row=row,end=end,direction=direction,flags=flags,beams=beams,image=im)

    @staticmethod
    def _flag_view(f,dx=0,dy=0):
        x,y=map(round,f["end"])
        x+=dx
        y+=dy
        im=f["image"]
        return (im.crop((x+3,y-43,x+18,y)), im.crop((x+22,y-43,x+35,y)))

    def flag_distance(self,target,reference):
        template,margin=self._flag_view(reference)
        ref=template.load()
        side=margin.load()
        usable=[sum(side[x,y]<170 for x in range(margin.width))<.65*margin.width for y in range(margin.height)]
        best=0
        for dx,dy in product(range(-2,3),repeat=2):
            image,border=self._flag_view(target,dx,dy)
            actual=image.load()
            guide=border.load()
            rows=[y for y,valid in enumerate(usable)
                  if valid and sum(guide[x,y]<170 for x in range(border.width))<.65*border.width]
            if len(rows)<20:
                continue
            scores=[]
            for ink in (120,160,190):
                a,b,both=0,0,0
                for y in rows:
                    for x in range(image.width):
                        px,py=ref[x,y]<ink,actual[x,y]<ink
                        a+=px;b+=py;both+=px and py
                scores.append(both/max(1,a+b-both))
            best=max(best,min(scores))
        return best

    def double_flag(self, row, donors, negatives):
        candidate=self.feature(row)
        if not candidate or candidate["direction"]!="down" or candidate["beams"] or len(candidate["flags"])>1:
            return None
        if candidate["flags"] and candidate["flags"][0].get("shape")!="FLAG_1_DOWN":
            return None
        signatures={"FLAG_2_DOWN":donors,"FLAG_1_DOWN":negatives}
        evidence={}
        for shape, refs in signatures.items():
            scores={}
            for r in refs:
                key=r["unit"],r["system"],r["staff"]
                if key in scores:
                    continue
                v=self.feature(r)
                if v is None or v["direction"]!="down" or v["beams"]:
                    continue
                if [flag.get("shape") for flag in v["flags"]]==[shape]:
                    scores[key]=self.flag_distance(candidate,v)
            evidence[shape]=sorted(scores.values(),reverse=True)
        a=evidence["FLAG_2_DOWN"]
        b=evidence["FLAG_1_DOWN"]
        if len(a)<2 or len(b)<2 or a[1]<.74 or a[1]-b[0]<.21:
            return None
        return {"double_flag_matches":a,"single_flag_matches":b}

    @staticmethod
    def _iou(a,b):
        a,b=list(a.getdata()),list(b.getdata())
        values=[]
        for t in (120,175,205):
            hit=sum(i<t and j<t for i,j in zip(a,b,strict=True))
            union=sum(i<t or j<t for i,j in zip(a,b,strict=True))
            values.append(hit/max(1,union))
        return min(values)

    def hook(self,row,donors):
        current=self.feature(row)
        if (not current or len(current["beams"])!=1 or current["beams"][0].tag!="beam"
                or current["flags"] or current["direction"]!="down"):
            return None
        for donor in donors:
            if (donor["unit"],donor["system"])!=(row["unit"],row["system"]):
                continue
            ref=self.feature(donor)
            if not ref or len(ref["beams"])!=2 or ref["direction"]!=current["direction"]:
                continue
            hooks=[n for n in ref["beams"] if n.tag=="beam-hook"]
            if len(hooks)!=1:
                continue
            b=hooks[0].find("bounds")
            x,y,w,h=(int(b.get(q)) for q in ("x","y","w","h"))
            template=ref["image"].crop((x,y,x+w,y+h))
            xx=round(x+current["end"][0]-ref["end"][0])
            yy=round(y+current["end"][1]-ref["end"][1])
            if abs(current["end"][0]-ref["end"][0])>w:
                continue
            for dx,dy in product(range(-2,3),repeat=2):
                candidate=current["image"].crop((xx+dx,yy+dy,xx+dx+w,yy+dy+h))
                mask_support=self._iou(template,candidate)
                if mask_support<.79:
                    continue
                gray_support=correlation(list(template.getdata()),list(candidate.getdata()))
                if gray_support>=.91:
                    return {"hook_mask_support":mask_support,"hook_gray_support":gray_support,
                            "bounds":[xx+dx,yy+dy,w,h],"paired_source_chord":donor["chord"]}
        return None
