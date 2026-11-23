"""Pixel boundaries for locally re-read CJK lyric bands, including trailing punctuation."""
from pathlib import Path
import json,sys,hashlib
import numpy as np
import cv2
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from cjk_word_pixels import _segment,_runs

def han(c):return len(c)==1 and '\u3400'<=c<='\u9fff'
def parse(text):
    blocks=[];block=None;word=None
    for line in text.splitlines():
        f=line.split('\t')
        if f[0]=='L' and len(f)==7:
            block=dict(confidence=float(f[1]),box=list(map(int,f[2:6])),text=f[6],characters=[]);blocks.append(block)
        elif f[0]=='C' and len(f)==6 and block is not None:block['characters'].append((f[5],*map(int,f[1:5])))
    return blocks

def refine_band(image,text,scope):
    gray=np.asarray(Image.open(image).convert('L'));h=scope['font_height'];dx,dy=scope['crop'][:2];letters=[];pending=[]
    for block in parse(text):
        chars=block['characters'];body=[c for c in chars if han(c[0])];punct=[c for c in chars if c[0] in '，。；、！？：']
        expected=''.join(c for c in block['text'] if han(c))
        if not body:continue
        if block['confidence']<.95 or ''.join(c[0] for c in body)!=expected:
            pending.append(dict(text=block['text'],reason='local_character_sequence_or_confidence'));continue
        if any(not han(c[0]) and c[0] not in '，。；、！？：' for c in chars):
            pending.append(dict(text=block['text'],reason='mixed_script_preserved'));continue
        x,y,bw,bh=block['box'];pad=max(3,round(.08*h));x0=max(0,x-pad);x1=min(gray.shape[1],x+bw+pad);y0=max(0,round(scope['baseline']-dy-1.08*h));y1=min(gray.shape[0],round(scope['baseline']-dy+.12*h));roi=gray[y0:y1,x0:x1];punct_start=None
        roi_body=roi.copy();punctuation_after={};punctuation_failed=False
        for mark in punct:
            index=chars.index(mark);preceding=[v for v in chars[:index] if han(v[0])]
            if not preceding:
                punctuation_failed=True;break
            last=preceding[-1];lastcenter=last[1]+last[3]/2-x0;pc=mark[1]+mark[3]/2-x0
            runs=_runs((roi<160).any(axis=0));possible=[(a,b) for a,b in runs if b-a<.4*h and abs((a+b)/2-pc)<.4*h and (a+b)/2>lastcenter+.2*h]
            if not possible:
                punctuation_failed=True;break
            start=min(a for a,b in possible);_,labels,stats,_=cv2.connectedComponentsWithStats((roi<200).astype(np.uint8));ids=set(int(v) for v in np.unique(labels[:,start:max(b for a,b in possible)]))-{0}
            if not ids or any(stats[k,2]>=.4*h or stats[k,0]+stats[k,2]/2<=lastcenter+.2*h for k in ids):
                punctuation_failed=True;break
            for k in ids:roi_body[labels==k]=255
            key=len(preceding)-1;punctuation_after[key]=punctuation_after.get(key,'')+mark[0]
        if punctuation_failed:
            pending.append(dict(text=block['text'],reason='source_punctuation_boundary_ambiguous'));continue
        segmentations=[]
        for threshold in [128,160,200]:
            segmentations.append(_segment(roi_body<threshold,len(body),h))
        if any(v is None for v in segmentations):
            pending.append(dict(text=block['text'],reason='source_character_count_not_supported_by_pixels',threshold_segments=segmentations,font_height=h));continue
        pieces=segmentations[1][0];tolerance=max(3,round(.1*h))
        if any(max(abs(a-c),abs(b-d))>tolerance for result in segmentations for (a,b),(c,d) in zip(pieces,result[0])):
            pending.append(dict(text=block['text'],reason='source_character_boundaries_threshold_conflict',threshold_segments=[v[0] for v in segmentations],font_height=h,tolerance=tolerance));continue
        for i,((ch,*_), (a,b)) in enumerate(zip(body,pieces)):
            patch=roi_body[:,a:b];yy,xx=np.where(patch<160)
            if not len(xx):raise ValueError('empty source Han segment')
            bx=x0+a+int(xx.min());by=y0+int(yy.min());right=x0+a+int(xx.max())+1;bottom=y0+int(yy.max())+1
            entry=dict(text=ch,bounds=[dx+bx,dy+by,right-bx,bottom-by],confidence=block['confidence'],unit=scope['unit'],system=scope['system'],staff=scope['staff'],source_sha256=scope['source_sha256'],baseline=scope['baseline'],font_height=h,source_ocr_file=scope['ocr_file'],segmentation_thresholds=[128,160,200])
            if i in punctuation_after:entry['punctuation']=punctuation_after[i]
            letters.append(entry)
    return letters,pending

