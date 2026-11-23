"""Original-pixel Han glyph reading from independently repeated source fonts."""
from pathlib import Path
import argparse,json,hashlib,zipfile,xml.etree.ElementTree as E,collections,re
import cv2,numpy as np
from sparse_source_font import SparseSourceFontMatcher
from source_lyric_readings import reference_id
import time

def han(s):return len(s)==1 and '\u3400'<=s<='\u9fff'
def box(n):return [int(n.find('bounds').get(k)) for k in ['x','y','w','h']]
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
DARK_THRESHOLDS=(112,128)
ALIGNED_DARK_THRESHOLDS = {
    'dark_ink_independent_aligned_staff_OCR': DARK_THRESHOLDS,
    'dark_ink_independent_aligned_staff_OCR_watermark': (112,144),
}
THIN_INK_MODE = 'dark_ink_two_thin_glyph_references'

def is_thin_glyph(tile, height):
    """Check a continuous horizontal stroke within a CJK glyph cell."""
    ys, xs = np.where(tile < DARK_THRESHOLDS[1])
    if not len(xs):
        return False
    width = int(np.ptp(xs)) + 1
    depth = int(np.ptp(ys)) + 1
    return (
        .72 * height <= width <= 1.17 * height
        and depth <= .15 * height
        and np.unique(xs).size == width
        and len(xs) >= 1.5 * width
    )


def dark_peer_readings(group, witnesses, refs, images, accepted, evidence_mode):
    """Retry aligned, independently read glyphs when light ink defeats the regular match."""
    key=(group['unit'],group['system'],group['staff'])
    height=group['font_height']
    image=images[group['unit']]
    peers=collections.defaultdict(list)
    for ref in refs:
        if (ref['unit'],ref['system'])==key[:2] and ref['staff']!=key[2] and ref.get('source_type')=='independent_cropped_local_OCR':
            peers[ref['text']].append(ref)
    additions=[]
    for own in witnesses[key]:
        if own['confidence']<.95:continue
        x,_,w,_=own['bounds'];cx=x+w/2
        if any(abs(c['center']-cx)<.35*height for c in accepted+additions):continue
        if sum(abs(v['bounds'][0]+v['bounds'][2]/2-cx)<.3*height for v in witnesses[key])!=1:continue
        options=[]
        for ref in peers[own['text']]:
            rx,ry,rw,rh=ref['bounds']
            if abs(rx+rw/2-cx)>.3*height or abs(ref['font_height']/height-1)>.15:continue
            left=max(0,round(cx-rw/2-.22*height));top=max(0,round(group['baseline']-(ref['baseline']-ry)-.19*height))
            right=min(image.shape[1],round(cx+rw/2+.22*height));bottom=min(image.shape[0],round(top+rh+.38*height))
            roi=image[top:bottom,left:right]
            tile=image[ry:ry+rh,rx:rx+rw]
            if roi.shape[0]<rh or roi.shape[1]<rw or tile.shape!=(rh,rw):continue
            proof=[]
            for threshold in ALIGNED_DARK_THRESHOLDS[evidence_mode]:
                template=(tile<threshold).astype(np.uint8);search=(roi<threshold).astype(np.uint8)
                if template.var()==0:break
                _,score,_,(dx,dy)=cv2.minMaxLoc(cv2.matchTemplate(search,template,cv2.TM_CCOEFF_NORMED))
                found=search[dy:dy+rh,dx:dx+rw];shared=np.logical_and(found,template).sum()
                precision=float(shared/max(1,found.sum()));recall=float(shared/max(1,template.sum()))
                if score<.95 or min(precision,recall)<.98:break
                proof.append(dict(bounds=[left+dx,top+dy,rw,rh],score=score,precision=precision,recall=recall,scale=1.))
            if len(proof)!=2 or proof[0]['bounds']!=proof[1]['bounds']:continue
            matched=proof[0]['bounds']
            if abs(matched[0]+rw/2-cx)>.25*height:continue
            options.append(dict(text=own['text'],bounds=matched,reference=ref,thresholds=proof,quality=min(p['score'] for p in proof)))
        if not options:continue
        best=max(options,key=lambda v:v['quality'])
        if any(abs(best['bounds'][0]+best['bounds'][2]/2-v['bounds'][0]-v['bounds'][2]/2)>.20*height for v in options):continue
        bx,_,bw,_=best['bounds']
        additions.append(dict(text=own['text'],bounds=best['bounds'],center=bx+bw/2,font_matches=[best],evidence_mode=evidence_mode))
    return additions

def thin_ink_readings(group, refs, images, accepted, ambiguous, matcher, x0, y0):
    """Recover a thin Han glyph only on agreement between independent staves."""
    unit, system, staff = group['unit'], group['system'], group['staff']
    height = group['font_height']
    candidates = []
    for ref in refs:
        identity = (ref['unit'], ref['system'], ref['staff'])
        if identity == (unit, system, staff) or ref['text'] != '一':
            continue
        if abs(ref['font_height'] / height - 1) > .35:
            continue
        rx, ry, rw, rh = ref['bounds']
        tile = images[ref['unit']][ry:ry+rh, rx:rx+rw]
        if not is_thin_glyph(tile, ref['font_height']):
            continue
        hits = []
        for threshold in DARK_THRESHOLDS:
            template = (tile < threshold).astype(np.uint8)
            found = {}
            for y, x, score, precision, recall in matcher.match(threshold, template):
                expected_y = group['baseline'] - (ref['baseline'] - ry)
                if abs(y + y0 - expected_y) > .15 * height:
                    continue
                found[x, y] = dict(bounds=[int(x+x0), int(y+y0), rw, rh],
                                   score=score, precision=precision, recall=recall, scale=1.)
            hits.append(found)
        for location in sorted(hits[0].keys() & hits[1].keys()):
            bounds = hits[0][location]['bounds']
            bx, by, bw, bh = bounds
            center = bx + bw/2
            if any(abs(v['center'] - center) < .3 * height for v in accepted + ambiguous):
                continue
            if not is_thin_glyph(images[unit][by:by+bh, bx:bx+bw], height):
                continue
            pair = [v[location] for v in hits]
            candidates.append((center, identity, dict(text=ref['text'], bounds=bounds,
                               reference=ref, thresholds=pair, quality=min(v['score'] for v in pair))))
    additions = []
    for center, _, proof in sorted(candidates, key=lambda v: (-v[2]['quality'], v[0])):
        if any(abs(v['center']-center) < .3 * height for v in additions):
            continue
        matching = {}
        for other_center, identity, other in candidates:
            if abs(other_center-center) < .15 * height:
                matching.setdefault(identity, other)
        if len(matching) < 2:
            continue
        additions.append(dict(text=proof['text'], bounds=proof['bounds'], center=center,
                              font_matches=list(matching.values()), evidence_mode=THIN_INK_MODE))
    return additions


def scan(book,source,scope=None,extra_refs=None):
    images={};groups=[];refs=[];pending=[]
    with zipfile.ZipFile(book) as archive:
        for name in archive.namelist():
            match=re.fullmatch(r'sheet#([1-9][0-9]*)/sheet#\1.xml',name)
            if not match:continue
            unit=int(match[1]);path=Path(source)/f'unit-{unit:03d}.png';images[unit]=cv2.imdecode(np.fromfile(path,np.uint8),0);digest=sha(path)
            for sy in E.fromstring(archive.read(name)).findall('page/system'):
                nodes=list(sy.find('sig/inters'))
                for part in sy.findall('part'):
                    if len(part.findall('staff'))!=1:continue
                    for staff in part.findall('staff'):
                        sid=staff.get('id');items=[n for n in nodes if n.tag=='lyric-item' and n.get('staff')==sid and any(han(c) for c in n.get('value',''))]
                        if sum(sum(han(c) for c in n.get('value','')) for n in items)<4:continue
                        heights=[box(n)[3] for n in items if box(n)[3]>=8]
                        if not heights:continue
                        height=float(np.median(heights));bottoms=[box(n)[1]+box(n)[3] for n in items];baseline=float(np.median(bottoms));near=[n for n in items if abs(box(n)[1]+box(n)[3]-baseline)<=.3*height or (box(n)[3]<.35*height and .5*height<=box(n)[2]<=1.3*height and .15*height<=baseline-(box(n)[1]+box(n)[3]/2)<=.85*height)]
                        if len(near)<.8*len(items):pending.append(dict(unit=unit,system=sy.get('id'),staff=sid,reason='multiple_or_ambiguous_lyric_baselines'));continue
                        g=dict(unit=unit,system=sy.get('id'),staff=sid,baseline=baseline,font_height=height,left=int(staff.get('left')),right=int(staff.get('right')),source_sha256=digest,han_items=len(near));groups.append(g)
                        for n in near:
                            text=n.get('value','')
                            if not han(text):continue
                            x,y,bw,bh=box(n);top=max(y,int(baseline-1.12*height));bottom=min(y+bh,int(baseline+.12*height));tile=images[unit][top:bottom,x:x+bw];ys,xs=np.where(tile<208)
                            if not len(xs):continue
                            pad=max(3,round(.08*height));bx=max(0,x+int(xs.min())-pad);by=max(0,top+int(ys.min())-pad);right=min(images[unit].shape[1],x+int(xs.max())+1+pad);bottom=min(images[unit].shape[0],top+int(ys.max())+1+pad);bounds=[bx,by,right-bx,bottom-by]
                            if bounds[2]>1.6*height or bounds[3]>1.5*height:continue
                            refs.append(dict(unit=unit,system=sy.get('id'),staff=sid,item=n.get('id'),text=text,bounds=bounds,baseline=baseline,font_height=height))
    witnesses=collections.defaultdict(list)
    if extra_refs is not None:
        local=json.loads(Path(extra_refs).read_text(encoding='utf8'))
        for group in local['scopes']:
            for letter in group['characters']:
                if letter['confidence']<.95 or not han(letter['text']):continue
                u=letter['unit'];path=Path(source)/f'unit-{u:03d}.png'
                if sha(path)!=letter['source_sha256']:raise ValueError('local OCR original page changed')
                x,y,bw,bh=letter['bounds'];pad=max(3,round(.08*letter['font_height']));bounds=[max(0,x-pad),max(0,y-pad),bw+2*pad,bh+2*pad]
                refs.append(dict(unit=u,system=letter['system'],staff=letter['staff'],item=reference_id(letter),text=letter['text'],bounds=bounds,baseline=letter['baseline'],font_height=letter['font_height'],source_type='independent_cropped_local_OCR',confidence=letter['confidence']))
                witnesses[u,letter['system'],letter['staff']].append(letter)
    perchar=collections.Counter(v['text'] for v in refs);results=[]
    for g in groups:
        key=(g['unit'],g['system'],g['staff'])
        if scope is not None and key not in scope:continue
        im=images[g['unit']];height=g['font_height'];y0=max(0,int(g['baseline']-1.4*height));y1=min(im.shape[0],int(g['baseline']+.3*height));x0=max(0,g['left']);x1=min(im.shape[1],g['right']);roi=im[y0:y1,x0:x1];hits=[];matcher=SparseSourceFontMatcher(roi);scope_started=time.monotonic()
        for ref in refs:
            if (ref['unit'],ref['system'],ref['staff'])==key or perchar[ref['text']]<2:continue
            if abs(ref['font_height']/height-1)>.35:continue
            rx,ry,rw,rh=ref['bounds'];tile=images[ref['unit']][ry:ry+rh,rx:rx+rw];threshold_hits=[]
            for threshold in [160,208]:
                found=[];search=(roi<threshold).astype(np.uint8)
                for scale in [.90,.925,.95,.97,.985,1.,1.015,1.03,1.05,1.075,1.10]:
                    tpl=(cv2.resize(tile,None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)<threshold).astype(np.uint8);th,tw=tpl.shape
                    if th>search.shape[0] or tw>search.shape[1] or tpl.var()==0:continue
                    for y,x,score,precision,recall in matcher.match(threshold,tpl):
                        expected=g['baseline']-(ref['baseline']-ry)*scale
                        if abs(y+y0-expected)>.15*height:continue
                        found.append(dict(bounds=[int(x+x0),int(y+y0),tw,th],score=score,precision=precision,recall=recall,scale=scale))
                threshold_hits.append(found)
            for a in threshold_hits[0]:
                compatible=[b for b in threshold_hits[1] if max(abs(x-y) for x,y in zip(a['bounds'],b['bounds']))<=1]
                if not compatible:continue
                b=max(compatible,key=lambda v:v['score']);hits.append(dict(text=ref['text'],bounds=a['bounds'],reference=ref,thresholds=[a,b],quality=min(a['score'],b['score'])))
        clusters=[]
        for hit in sorted(hits,key=lambda v:v['quality'],reverse=True):
            cx=hit['bounds'][0]+hit['bounds'][2]/2
            group=next((v for v in clusters if abs(cx-v['center'])<.3*height),None)
            if group is None:group=dict(center=cx,hits=[]);clusters.append(group)
            group['hits'].append(hit)
        characters=[];ambiguous=[]
        for cluster in sorted(clusters,key=lambda v:v['center']):
            supported={}
            for hit in cluster['hits']:
                references=supported.setdefault(hit['text'],{});identity=(hit['reference']['unit'],hit['reference']['system'],hit['reference']['staff'])
                if identity not in references or hit['quality']>references[identity]['quality']:references[identity]=hit
            qualified={}
            evidence_modes={}
            for text,proofs in supported.items():
                own=[v for v in witnesses[key] if v['text']==text and abs(v['bounds'][0]+v['bounds'][2]/2-cluster['center'])<.3*height]
                if len(proofs)>=2:
                    qualified[text]=list(proofs.values());evidence_modes[text]='two_other_physical_staff_fonts'
                elif proofs and len(own)==1:
                    qualified[text]=list(proofs.values());evidence_modes[text]='independent_local_OCR_and_other_physical_staff_font'
            if len(qualified)!=1:
                if qualified:ambiguous.append(dict(center=cluster['center'],characters=list(qualified),reason='source_character_font_ambiguous'))
                continue
            text,proofs=next(iter(qualified.items()));best=max(proofs,key=lambda v:v['quality']);characters.append(dict(text=text,bounds=best['bounds'],center=cluster['center'],font_matches=proofs,evidence_mode=evidence_modes[text]))
        for evidence_mode, thresholds in ALIGNED_DARK_THRESHOLDS.items():
            # The established two-source OCR check can resolve an ambiguous font match.
            # The new threshold fallback may only fill an otherwise unclaimed position.
            blocked = characters + ambiguous if thresholds != DARK_THRESHOLDS else characters
            characters.extend(dark_peer_readings(g, witnesses, refs, images, blocked, evidence_mode))
        characters.extend(thin_ink_readings(g, refs, images, characters, ambiguous, SparseSourceFontMatcher(roi, DARK_THRESHOLDS), x0, y0))

        characters.sort(key=lambda v:v['center'])
        result=dict(**g,matcher_statistics=dict(calls=matcher.calls,cache_hits=matcher.cache_hits,full_metric_checks=matcher.full_metric_checks,elapsed_seconds=round(time.monotonic()-scope_started,3)),characters=characters,decoded=''.join(v['text'] for v in characters),pending=ambiguous);results.append(result);print('SOURCE_HAN_FONT_SCOPE',key,'letters',len(characters),result['decoded'],result['matcher_statistics'],flush=True)
    return dict(source_book_sha256=sha(book),source=str(source),reference_count=len(refs),font_references=refs,reference_characters=len(perchar),source_groups=len(groups),staff_groups=groups,scopes=results,pending=pending,rule='Independent crop OCR plus another physical staff font, or two other staff fonts; dual160/208 NCC>=.95 precision/recall>=.98; no language-model text inference')
