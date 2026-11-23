
"""Check original drum ticks, tuplet digits and bracket geometry."""
from itertools import product
from pathlib import Path
import cv2,numpy as np


def components(image,box,threshold=208):
    x,y,u,v=map(round,box)
    if min(x,y)<0 or u>image.shape[1] or v>image.shape[0] or x>=u or y>=v:
        return []
    tile=image[y:v,x:u]
    _,_,stats,_=cv2.connectedComponentsWithStats((tile<threshold).astype('uint8'))
    return [[x+int(a),y+int(b),int(w),int(h),int(area)] for a,b,w,h,area in stats[1:]]


def digit_views(image,box,space):
    options=[c for c in components(image,box) if .68*space<=c[2]<=1.18*space
             and 1.05*space<=c[3]<=1.65*space and .48*c[2]*c[3]<=c[4]<=.72*c[2]*c[3]]
    if len(options)!=1:
        return None,[]
    x,y,w,h,_=options[0]
    ink=image[y:y+h,x:x+w]
    views=[]
    for scale in (1.80,2.35):
        crop=cv2.resize(ink,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
        pad=cv2.copyMakeBorder(crop,6,6,24,24,cv2.BORDER_CONSTANT,value=255)
        views.append(cv2.cvtColor(pad,cv2.COLOR_GRAY2BGR))
    return options[0],views


def bracket(image,box,space,number):
    digits_x=number[0]+number[2]/2
    evidence=[]
    for th in (160,208):
        found=[c for c in components(image,box,th) if 3.2*space<=c[2]<=6.2*space
               and .65*space<=c[3]<=1.35*space and c[4]>.12*c[2]*c[3]]
        if len(found)!=2:
            return []
        a,b=sorted(found)
        if (abs(a[1]-b[1])>.14*space
            or not (a[0]+a[2]<digits_x<b[0] and abs(a[0]+a[2]-number[0])<.25*space)):
            return []
        for mark,side in ((a,'left'),(b,'right')):
            x,y,w,h,_=mark
            tile=(image[y:y+h,x:x+w]<th)
            end=tile[:, :round(.4*space)] if side=='left' else tile[:,-round(.4*space):]
            if tile[:round(.32*space)].any(axis=0).mean()<.90 or end.any(axis=1).mean()<.86:
                return []
        evidence.append(dict(threshold=th,strokes=[a,b]))
    return evidence


def cross_head(image,stem_x,staff_top,space):
    x=round(stem_x-.49*space)
    y=round(staff_top-.5*space)
    offsets=np.arange(-round(.38*space),round(.34*space)+1)
    winners=[]
    for dx,dy in product(range(-2,3),repeat=2):
        cx,cy=x+dx,y+dy
        xx=cx+offsets
        if xx.min()<0 or xx.max()>=image.shape[1] or cy+max(abs(offsets))+2>=image.shape[0] or cy-max(abs(offsets))-2<0:
            continue
        proof=[]
        for threshold in (160,208):
            arms=[]
            for slope in (-1,1):
                yy=cy+slope*offsets
                saw=(np.array([image[yy+j,xx]<threshold for j in (-1,0,1)]).any(axis=0))
                arms.extend(float(p.mean()) for p in np.array_split(saw,3))
            corner=np.array([image[cy-round(.33*space):cy-round(.22*space),cx-1:cx+2],
                             image[cy+round(.22*space):cy+round(.33*space),cx-1:cx+2]])
            if min(arms)<.80 or (corner<threshold).mean()>.38:
                break
            proof.append(dict(threshold=threshold,ink=arms))
        if len(proof)==2:
            winners.append((min(min(n['ink']) for n in proof),-abs(dx)-abs(dy),dict(center=[cx,cy],arms=proof)))
    return max(winners,key=lambda n:n[:2])[2] if winners else None

