"""Conservative pixel segmentation for a recognition-confirmed CJK word.
No lyric text is invented or corrected. Return None if pixel character boundaries
cannot independently support the existing decoded character count.
"""
import numpy as np

def _runs(flags):
    out=[]
    for i in np.flatnonzero(flags):
        if out and i==out[-1][1]:out[-1][1]=int(i)+1
        else:out.append([int(i),int(i)+1])
    return out

def _segment(ink,count,height):
    runs=_runs(ink.any(axis=0));pieces=[];cut_ink=[]
    for a,b in runs:
        if b-a>1.45*height:
            n=round((b-a)/(.95*height))
            if not 2<=n<=8:return None
            cuts=[a]
            for k in range(1,n):
                ideal=a+(b-a)*k/n;lo=max(cuts[-1]+round(.40*height),round(ideal-.20*height));hi=min(b-round(.40*height),round(ideal+.20*height))
                if lo>hi:return None
                candidates=[]
                for x in range(lo,hi+1):
                    strip=ink[:,max(a,x-1):min(b,x+2)];density=float(strip.mean());candidates.append((density,abs(x-ideal),x))
                density,_,cut=min(candidates)
                if density>.18:return None
                cut_ink.append(density);cuts.append(cut)
            cuts.append(b);pieces.extend([[x,y] for x,y in zip(cuts,cuts[1:])])
        else:pieces.append([a,b])
    while True:
        choices=[]
        for i in range(len(pieces)-1):
            a,b=pieces[i];c,d=pieces[i+1];gap=c-b
            if gap<=.20*height and d-a<=1.25*height and min(b-a,d-c)<.48*height:
                choices.append((gap,min(b-a,d-c),i))
        if not choices:break
        _,_,i=min(choices);pieces[i:i+2]=[[pieces[i][0],pieces[i+1][1]]]
    if len(pieces)!=count:return None
    if any(not .35*height<=b-a<=1.32*height for a,b in pieces):return None
    return pieces,cut_ink

def refine_cjk_word(gray,word):
    if len(word)<2 or any(len(c[0])!=1 or not '\u3400'<=c[0]<='\u9fff' for c in word):return None
    x0=min(c[1] for c in word);y0=min(c[2] for c in word);x1=max(c[1]+c[3] for c in word);y1=max(c[2]+c[4] for c in word)
    if not (0<=x0<x1<=gray.shape[1] and 0<=y0<y1<=gray.shape[0]):return None
    h=y1-y0
    if h<8:return None
    segments=[]
    for threshold in (128,160,200):
        result=_segment(gray[y0:y1,x0:x1]<threshold,len(word),h)
        if result is None:return None
        segments.append(result)
    reference=segments[1][0]
    tolerance=max(3,round(.10*h))
    if any(abs(a-c)>tolerance or abs(b-d)>tolerance for segments_at_threshold,_ in segments for (a,b),(c,d) in zip(segments_at_threshold,reference)):return None
    out=[]
    for original,(a,b) in zip(word,reference):
        ink=gray[y0:y1,x0+a:x0+b]<160;rows=np.flatnonzero(ink.any(axis=1));cols=np.flatnonzero(ink.any(axis=0))
        if not len(rows) or not len(cols):return None
        if ink.mean()<.08:return None
        out.append((original[0],x0+a+int(cols[0]),y0+int(rows[0]),int(cols[-1]-cols[0]+1),int(rows[-1]-rows[0]+1)))
    return out


def refine_cjk_word_on_heads(gray, word, heads):
    """Use the OCR character centers and an independent row of noteheads to split one word."""
    if not 2 <= len(word) <= 20:
        return None
    left = min(x for _, x, _, _, _ in word)
    right = max(x + w for _, x, _, w, _ in word)
    top = min(y for _, _, y, _, _ in word)
    bottom = max(y + h for _, _, y, _, h in word)
    height = bottom - top
    if not (0 <= left < right <= gray.shape[1] and 0 <= top < bottom <= gray.shape[0] and height >= 22):
        return None
    positions = sorted(x for x in heads if left < x < right)
    if len(positions) != len(word):
        return None
    if any(abs(x + w / 2 - pos) > .24 * height
           for (_, x, _, w, _), pos in zip(word, positions)):
        return None
    gaps = [right - left for left, right in zip(positions, positions[1:])]
    if min(gaps) < .48 * height:
        return None

    def glyph_boxes(threshold):
        ink = gray[top:bottom, left:right] < threshold
        columns = ink.sum(axis=0)
        cuts = [0]
        for a, b in zip(positions, positions[1:]):
            mid = (a + b) / 2 - left
            radius = max(3, round(min(.15 * height, .22 * (b - a))))
            low = max(1, round(mid) - radius)
            high = min(len(columns) - 2, round(mid) + radius)
            cut = min(range(low, high + 1),
                      key=lambda x: (columns[x-1:x+2].sum(), abs(x - mid)))
            if columns[cut-1:cut+2].sum() > .035 * 3 * height:
                return None
            cuts.append(cut)
        cuts.append(right - left)
        boxes = []
        for x0, x1, pos in zip(cuts, cuts[1:], positions):
            rows, cols = np.where(ink[:, x0:x1])
            if not len(rows):
                return None
            x = left + x0 + int(cols.min())
            y = top + int(rows.min())
            width = int(cols.max() - cols.min() + 1)
            glyph_height = int(rows.max() - rows.min() + 1)
            density = len(rows) / (width * glyph_height)
            if not (.33 * height <= width <= 1.16 * height
                    and .36 * height <= glyph_height <= height and .10 <= density < .58
                    and abs(x + width / 2 - pos) <= .13 * height):
                return None
            boxes.append((x, y, width, glyph_height))
        return boxes

    variants = [glyph_boxes(threshold) for threshold in (128, 160, 200)]
    if any(boxes is None for boxes in variants):
        return None
    middle = variants[1]
    if any(abs(a - b) > .08 * height
           for boxes in variants for anchor, box in zip(middle, boxes) for a, b in zip(anchor, box)):
        return None
    return [(char, *box) for (char, *_), box in zip(word, middle)]

