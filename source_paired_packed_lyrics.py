
"""Separate a packed lyric only when a second printed staff confirms every glyph."""
from collections import Counter, defaultdict
from copy import deepcopy
import csv
from fractions import Fraction
from functools import lru_cache
from hashlib import file_digest
from itertools import product
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import score


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def digest(path):
    with path.open("rb") as handle:
        return file_digest(handle,"sha256").hexdigest()


def signature(event):
    n=event["node"]
    return (event["start"],event["duration"],
            tuple(n.findtext(field,"0") for field in ("staff","voice","pitch/step","pitch/alter","pitch/octave")))


PUNCTUATION="，。；、！？："


def lyric_tokens(word):
    letters=word.rstrip(PUNCTUATION)
    suffix=word[len(letters):]
    if not 2<=len(letters)<=8 or any(not "\u3400"<=c<="\u9fff" for c in letters) or len(suffix)>1:
        return []
    return [*letters[:-1],letters[-1]+suffix]


def exact_column_score(source_events, final_events, octave_rows, rhythm_rows):
    """Pair anchors only when every clock and pitch delta in the bar has a source receipt."""
    confirmed=set()
    for bar in octave_rows.keys()|rhythm_rows.keys():
        original={k:v for k,v in source_events.items() if k[:2]==bar}
        actual={k:v for k,v in final_events.items() if k[:2]==bar}
        if not original or original.keys()!=actual.keys():
            continue
        octave_changes=Counter()
        edits=0
        shift=Fraction(0)
        matched=set()
        patch=rhythm_rows.get(bar,{})
        for k in sorted(original):
            left,right=signature(original[k]),signature(actual[k])
            if left[2][:-1]!=right[2][:-1] or left[0]+shift!=right[0]:
                break
            if left[2][-1]!=right[2][-1]:
                octave_changes[left[2][-1],right[2][-1]]+=1
            clocks=patch.get(k[2])
            if clocks is None:
                if left[1]!=right[1]:
                    break
            else:
                if tuple(map(str,(left[1],right[1])))!=clocks or left[1]==right[1]:
                    break
                if any(e["node"].find("chord") is not None for e in original.values()):
                    break
                shift+=right[1]-left[1]
                edits+=1
            if left!=right:
                matched.add(k)
        else:
            if octave_changes==octave_rows.get(bar,Counter()) and edits==len(patch):
                confirmed.update(matched)
    return confirmed


def packed_candidates(root, target_events, native_events, ledger, confirmed_columns):
    parts={p.get("id") for p in root.findall("part") if not p.findall("measure/attributes/staves")}
    for (part,measure,i),event in target_events.items():
        n=event["node"]
        lyric=n.find("lyric")
        word=n.findtext("lyric/text","")
        tokens=lyric_tokens(word)
        if (part not in parts or not tokens or len(n.findall("lyric"))!=1
                or not set(lyric.attrib)<={"number","name","placement"}
                or any(c.tag not in {"syllabic","text"} or c.attrib for c in lyric)):
            continue
        for left in range(i-len(tokens)+1,i+1):
            keys=[(part,measure,index) for index in range(left,left+len(tokens))]
            if any(k not in ledger or k not in target_events or k not in native_events for k in keys):
                continue
            events=[target_events[k] for k in keys]
            print_rows=[ledger[k] for k in keys]
            first=print_rows[0]
            if any(e["node"].find("pitch") is None or e["node"].find("chord") is not None
                   or e["node"].find("grace") is not None
                   or k not in confirmed_columns and signature(e)!=signature(native_events[k])
                   for k,e in zip(keys,events)):
                continue
            extra={}
            blocked=False
            for position,e in enumerate(events):
                element=e["node"]
                lyrics=element.findall("lyric")
                if element.find("tie[@type='stop']") is not None:
                    blocked=True
                    break
                if element is n or not lyrics:
                    continue
                text=element.findtext("lyric/text","")
                if (len(lyrics)!=1 or len(text)!=1 or not "\u3400"<=text<="\u9fff"
                        or lyrics[0].find("extend") is not None):
                    blocked=True
                    break
                extra[position]=lyrics[0]
            if blocked:
                continue
            if any(a["start"]+a["duration"]!=b["start"] or a["node"].findtext("voice")!=b["node"].findtext("voice")
                   for a,b in zip(events,events[1:])):
                continue
            if any(any(r[f]!=first[f] for f in ("unit","system","staff","source_sha256")) for r in print_rows):
                continue
            yield dict(part=part,measure=measure,word=word,tokens=tokens,events=events,rows=print_rows,node=n,extra=extra)


def box(node):
    return tuple(int(node.find("bounds").get(k)) for k in ("x","y","w","h"))


def physical_item(graph,case):
    row=case["rows"][0]
    left=int(row["x"])-.95*int(row["w"])
    right=int(case["rows"][-1]["x"])+int(row["w"])*2
    items=[]
    for n in graph.findall("sig/inters/lyric-item"):
        if n.get("staff")!=row["staff"] or n.get("value")!=case["word"]:
            continue
        x,y,w,h=box(n)
        if left<=x<=int(row["x"])+int(row["w"]) and x+w>=right-int(row["w"]):
            items.append((n, (x,y,w,h)))
    return items[0] if len(items)==1 else None


def match_boxes(image, boxes, delta, thresholds=(128,160,208),slack=3,*,horizontal=range(-2,3),vertical=None):
    """Use one shared registration for every letter in the repeated printed group."""
    best=None
    for dx,dy in product(horizontal,range(-slack,slack+1) if vertical is None else vertical):
        qualities=[]
        for x,y,w,h in boxes:
            q=image.crop((x-3,y-3,x+w+3,y+h+3))
            u=image.crop((x-3+dx,y-3+delta+dy,x+w+3+dx,y+h+3+delta+dy))
            qa,ua=list(q.getdata()),list(u.getdata())
            metrics=[]
            for cutoff in thresholds:
                hits=sum(a<cutoff and b<cutoff for a,b in zip(qa,ua))
                union=sum(a<cutoff or b<cutoff for a,b in zip(qa,ua))
                metrics.append(hits/max(1,union))
            qualities.append(metrics)
        low=min(min(m) for m in qualities)
        if best is None or low>best[0]:
            best=(low,dx,dy,qualities)
    return best


def characters(frame,case,confidence=.96,allow_printed_punctuation=False):
    row=case["rows"][0]
    chars=[]
    if frame["source_sha256"]!=row["source_sha256"]:
        return None
    for token,r in zip(case["tokens"],case["rows"]):
        head_x=int(r["x"])+int(r["w"])/2
        options=[v for v in frame["characters"] if v.get("confidence",0)>=confidence and same_syllable(v["text"]+v.get("punctuation",""),token,
                                   allow_missing_punctuation=allow_printed_punctuation)
                 and v.get("source_sha256")==row["source_sha256"]
                 and abs(v["bounds"][0]+v["bounds"][2]/2-head_x)<=.42*int(r["w"])]
        if len(options)!=1:
            return None
        chars.append(options[0])
    ordered=[v["bounds"] for v in chars]
    if any(a[0]+a[2]>=b[0] for a,b in zip(ordered,ordered[1:])):
        return None
    return ordered


def apply(case,tokens):
    owner=case["node"]
    old=owner.find("lyric")
    for letter,event in zip(tokens,case["events"]):
        n=event["node"]
        leaf=deepcopy(old)
        leaf.find("text").text=letter
        if n is owner:
            position=list(n).index(old)
            n.remove(old)
        else:
            position=next((i for i,c in enumerate(n) if c.tag in {"play","listen"}),len(n))
        n.insert(position,leaf)



def remove_duplicate_continuation(events,native,rows,frames,job):
    """Remove a repeat only when the source has no glyph at the tied continuation."""
    found=[]
    for (part,measure,index),current in events.items():
        node=current["node"]
        said=node.findtext("lyric/text","")
        if (len(said)!=1 or not "㐀"<=said<="鿿" or len(node.findall("lyric"))!=1
                or node.find("tie[@type='stop']") is None or node.find("tie[@type='start']") is not None):
            continue
        lyric=node.find("lyric")
        if lyric.get("number","1")=="1" or lyric.find("extend") is not None:
            continue
        keys=[(part,measure,index+di) for di in (-1,0,1)]
        if any(k not in rows or k not in native or k not in events or signature(native[k])!=signature(events[k]) for k in keys):
            continue
        before,_,after=(events[k] for k in keys)
        follower=after["node"]
        if (before["node"].find("tie[@type='start']") is None
                or before["start"]+before["duration"]!=current["start"] or current["start"]+current["duration"]!=after["start"]
                or signature(before)[2]!=signature(current)[2] or before["node"].find("lyric") is None
                or len(follower.findall("lyric"))!=1 or follower.findtext("lyric/text")!=said
                or follower.find("lyric").get("number","1")!="1" or node.findtext("voice")!=follower.findtext("voice")
                or follower.find("tie[@type='stop']") is not None):
            continue
        if sum(l.get("number")==lyric.get("number") for k,e in events.items() if k[:2]==(part,measure)
               for l in e["node"].findall("lyric"))!=1:
            continue
        left,right=(rows[k] for k in keys[1:])
        if any(left[f]!=right[f] for f in ("unit","system","staff","source_sha256")):
            continue
        unit,system,staff=left["unit"],left["system"],left["staff"]
        frame=frames.get((unit,system,staff))
        if not frame or frame["source_sha256"]!=left["source_sha256"]:
            continue
        a=float(left["x"])+float(left["w"])/2
        b=float(right["x"])+float(right["w"])/2
        options=[c for c in frame["characters"] if c["text"]==said and c["confidence"]>=.98
                 and c["source_sha256"]==left["source_sha256"]
                 and abs(c["bounds"][0]+c["bounds"][2]/2-b)<=.4*int(right["w"])]
        if len(options)!=1:
            continue
        [glyph]=options
        x,y,w,h=glyph["bounds"]
        if b-a<1.1*w:
            continue
        absence=(round(a-.42*w),y-3,round(a+.42*w),y+h+3)
        if absence[2]+5>=x:
            continue
        source=job/f"unit-{int(unit):03d}.png"
        if digest(source)!=left["source_sha256"]:
            continue
        with Image.open(source) as image:
            blank=image.crop(absence).convert("L")
            actual=image.crop((x,y,x+w,y+h)).convert("L")
            if min(blank.getdata())<220 or sum(v<170 for v in actual.getdata())<.07*w*h:
                continue
        node.remove(lyric)
        found.append(dict(part=part,measure=measure,note_index=index,text=said,next_note_index=index+1,
                          source_sha256=left["source_sha256"],blank_bounds=absence,printed_glyph=glyph["bounds"]))
    return found


def same_syllable(actual,expected,allow_missing_punctuation=True):
    return actual==expected or (allow_missing_punctuation and actual==expected.rstrip(PUNCTUATION)
                                and actual!=expected)


def split_donor_candidates(case, entries, syllables, events, native, confirmed_columns, *, displaced=False):
    unit,system,staff=(case["rows"][0][k] for k in ("unit","system","staff"))
    part=case["part"]
    for peer in (int(staff)-1,int(staff)+1):
        slot=entries.get((unit,system,str(peer),case["measure"]),[])
        by_key=dict(slot)
        for first,_ in slot:
            if first[0]==part:
                continue
            keys=[(first[0],first[1],first[2]+i) for i in range(len(case["tokens"]))]
            if any(k not in by_key or (k not in confirmed_columns and signature(events[k])!=signature(native[k]))
                   for k in keys):
                continue
            tokens=case["tokens"]
            labels=[syllables[k] for k in keys]
            added_comma=(not displaced and labels==[*tokens[:-1],tokens[-1]+"，"]
                         and tokens[-1][-1] not in PUNCTUATION)
            if not added_comma and not all(same_syllable(a,b) for a,b in zip(labels,tokens)):
                continue
            rows=[by_key[k] for k in keys]
            anchors=case["rows"]
            distances=[int(a["x"])-int(b["x"]) for a,b in zip(anchors,rows)]
            offsets=[a["start"]-events[k]["start"] for a,k in zip(case["events"],keys)]
            if displaced:
                if len(keys)>3 or len(set(offsets))!=1 or offsets[0]==0 or max(distances)-min(distances)>4:
                    continue
                pitch_spacing=min(int(b["x"])-int(a["x"]) for a,b in zip(anchors,anchors[1:]))
                if min(abs(d) for d in distances)<=2 or max(abs(d) for d in distances)>pitch_spacing+4:
                    continue
            elif any(abs(d)>2 for d in distances) or any(offsets):
                continue
            yield dict(part=first[0],measure=case["measure"],word=case["word"],tokens=case["tokens"],indices=keys,
                       events=[events[k] for k in keys],rows=rows,added_comma=added_comma)


def physical_donor(graph,case,entries,syllables):
    """Recognize the same phrase even if the native text formed several boxes."""
    staff=case["rows"][0]["staff"]
    objects=[(n,box(n)) for n in graph.findall("sig/inters/lyric-item")
             if n.get("staff")==staff]
    rows=case["rows"]
    def walk(index,chosen):
        if index==len(rows):
            yield chosen
            return
        for node,region in objects:
            item=node.get("value","")
            size=sum("\u3400"<=c<="\u9fff" for c in item)
            if not size or index+size>len(rows) or item!="".join(case["tokens"][index:index+size]):
                continue
            x,_,w,_=region
            first=rows[index];last=rows[index+size-1]
            start=int(first["x"]);end=int(last["x"]);width=int(last["w"])
            if start-.95*width<=x<=start+width and end+.5*width<=x+w<=end+3*width:
                yield from walk(index+size,chosen+[(node.get("id"),region)])
    paths=list(walk(0,[]))
    if len(paths)==1:
        boxes=paths[0]
        xs,ys,ws,hs=zip(*(values for _,values in boxes))
        x,y=min(xs),min(ys)
        return [i for i,_ in boxes],(x,y,max(a+b for a,b in zip(xs,ws))-x,max(a+b for a,b in zip(ys,hs))-y)
    if paths:
        return None

    # A substring of a single native box is usable only when every note under
    # that full box already has the matching split text and the order is exact.
    # This also lets a newly verified complete phrase witness a shorter repeat.
    key=case["indices"][0]
    found=[]
    for node,region in objects:
        word=node.get("value","")
        tokens=lyric_tokens(word)
        for offset in range(1+len(tokens)-len(case["tokens"])):
            if tokens[offset:offset+len(case["tokens"])]!=case["tokens"]:
                continue
            addresses=[(key[0],key[1],i) for i in range(key[2]-offset,key[2]-offset+len(tokens))]
            if any(k not in entries or k not in syllables or not same_syllable(syllables[k],token)
                   for k,token in zip(addresses,tokens)):
                continue
            head,tail=entries[addresses[0]],entries[addresses[-1]]
            x,_,w,_=region
            if (int(head["x"])-int(head["w"])<=x<=int(head["x"])+int(head["w"])
                    and int(tail["x"])<=x+w<=int(tail["x"])+3*int(tail["w"])):
                found.append(([node.get("id")],region))
    return found[0] if len(found)==1 else None


def printed_word_overlap(job,case,peer,source_box,peer_box,frames):
    for pair,other,area,other_area in ((case,peer,source_box,peer_box),(peer,case,peer_box,source_box)):
        u,sy,st=(pair["rows"][0][k] for k in ("unit","system","staff"))
        frame=frames.get((u,sy,st))
        boxes=characters(frame,pair,.95) if frame else None
        if not boxes:
            continue
        strict=characters(frame,pair)
        peer_frame=frames.get((u,sy,other["rows"][0]["staff"]))
        independently_read=(peer_frame and characters(frame,pair,.98,True) and characters(peer_frame,other,.95,True))
        image=job/f"unit-{int(u):03d}.png"
        source_sha=pair["rows"][0]["source_sha256"]
        if digest(image)!=source_sha or other["rows"][0]["source_sha256"]!=source_sha:
            continue
        if any(x<area[0]-3 or x+w>area[0]+area[2]+3 or y<area[1]-3 or y+h>area[1]+area[3]+3
               for x,y,w,h in boxes):
            continue
        delta=other_area[1]-area[1]
        slack=max(3,min(10,abs(other_area[3]-area[3])+3))
        thresholds=(128,160,208)
        used_dual_ocr=False
        with Image.open(image) as raw:
            patch=raw.convert("L")
            low,dx,dy,metrics=match_boxes(patch,boxes,delta,thresholds,slack)
            if not ((strict and low>=.94) or low>=.98):
                if not independently_read:
                    continue
                # With two exact OCR reads, near-white antialias strokes may differ.
                thresholds=(128,160,185)
                low,dx,dy,metrics=match_boxes(patch,boxes,delta,thresholds,slack)
                if low<.94:
                    continue
                used_dual_ocr=True
        ox,oy,ow,oh=other_area
        if any(not(ox-3<=x+dx and x+w+dx<=ox+ow+3 and oy-3<=y+delta+dy and y+h+delta+dy<=oy+oh+3)
               for x,y,w,h in boxes):
            continue
        return dict(source_sha256=source_sha,glyph_boxes=boxes,peer_ink_overlap=metrics,
                    peer_shift=[dx,delta+dy],ocr_staff=st,ink_thresholds=thresholds,
                    two_independent_high_confidence_readings=used_dual_ocr)
    return None


def transferred_word_overlap(job,case,peer,source_box,peer_box,frames):
    """Verify text missing from target OCR by registering ink at its own note columns."""
    u,sy,st=(peer["rows"][0][key] for key in ("unit","system","staff"))
    frame=frames.get((u,sy,st))
    if not frame:
        return None
    comma=peer.get("added_comma",False)
    ocr_case=dict(peer,tokens=[*peer["tokens"][:-1],peer["tokens"][-1]+"，"]) if comma else peer
    glyphs=characters(frame,ocr_case,.95 if comma else .98)
    ox,oy,ow,oh=peer_box
    if not glyphs or any(not (ox-3<=x and x+w<=ox+ow+3 and oy-3<=y and y+h<=oy+oh+3)
                         for x,y,w,h in glyphs):
        return None
    source_sha=case["rows"][0]["source_sha256"]
    image=job/f"unit-{int(u):03d}.png"
    if peer["rows"][0]["source_sha256"]!=source_sha or digest(image)!=source_sha:
        return None
    target_frame=frames.get((u,sy,case["rows"][0]["staff"]))
    if target_frame:
        for token,row in zip(case["tokens"],case["rows"]):
            width=int(row["w"])
            center=int(row["x"])+width/2
            if any(g.get("confidence",0)>=.95 and g.get("source_sha256")==source_sha
                   and abs(g["bounds"][0]+g["bounds"][2]/2-center)<=.42*width
                   and not same_syllable(g["text"]+g.get("punctuation",""),token)
                   for g in target_frame["characters"]):
                return None
    sx,sy,sw,sh=source_box
    distances=[int(a["x"])-int(b["x"]) for a,b in zip(case["rows"],peer["rows"])]
    vertical=range(sy-min(y for _,y,_,_ in glyphs)-3,sy+sh-max(y+h for _,y,_,h in glyphs)+4)
    if not vertical or len(vertical)>2*max(h for _,_,_,h in glyphs):
        return None
    cuts=(128,160,185)
    with Image.open(image) as raw:
        gray=raw.convert("L")
    # Engravers can center the glyphs separately. Keep one baseline and
    # bound each horizontal shift by its own verified note column.
    best=None
    for dy in vertical:
        alignment=[match_boxes(gray,[glyph],0,cuts,horizontal=range(offset-2,offset+3),vertical=(dy,))
                   for glyph,offset in zip(glyphs,distances)]
        low=min(item[0] for item in alignment)
        if best is None or low>best[0]:
            best=low,dy,alignment
    low,dy,alignment=best
    if low<(.98 if comma else .94):
        return None
    shifts=[dx for _,dx,_,_ in alignment]
    qualities=[metrics[0] for _,_,_,metrics in alignment]
    transferred=[[x+dx,y+dy,w,h] for (x,y,w,h),dx in zip(glyphs,shifts)]
    if any(not(sx-3<=x and x+w<=sx+sw+3) or
           abs(x+w/2-int(row["x"])-int(row["w"])/2)>.42*int(row["w"])
           for (x,y,w,h),row in zip(transferred,case["rows"])):
        return None
    evidence=dict(source_sha256=source_sha,glyph_boxes=transferred,peer_ink_overlap=qualities,
                  peer_glyph_shifts=[[dx,dy] for dx in shifts],ocr_staff=st,ink_thresholds=cuts,
                  transferred_to_target_note_columns=True)
    if comma:
        # Verify the punctuation independently of the two character boxes.
        # Both printed staves must contain the same isolated small ink mark.
        x,y,w,h=glyphs[-1]
        dx=shifts[-1]
        left=x+w
        top=y+h//2
        right=min(ox+ow,sx+sw-dx,left+round(w*.55))
        bottom=min(oy+oh,sy+sh-dy,y+round(h*1.24))
        if left>=right or top>=bottom:
            return None
        spot=gray.crop((left,top,right,bottom)).point(lambda pixel: 255 if pixel<128 else 0).getbbox()
        if not spot:
            return None
        a,b,c,d=spot
        if (a<2 or c>=right-left or b<1 or d>=bottom-top
                or not .10*w<=c-a<=.30*w or not .18*h<=d-b<=.42*h):
            return None
        comma_box=[left+a,top+b,c-a,d-b]
        mark_score,_,_,mark_metrics=match_boxes(gray,[comma_box],0,cuts,horizontal=(dx,),vertical=(dy,))
        if mark_score<.92:
            return None
        evidence["comma_glyph"]=[left+a+dx,top+b+dy,c-a,d-b]
        evidence["comma_ink_overlap"]=mark_metrics[0]
    return evidence


def repair_with_split_peer(groups,book_page,job,events,native,ledger_rows,confirmed_columns,frames,already_split):
    indexed=defaultdict(list)
    for k,row in ledger_rows.items():
        if k in already_split and k in native:
            indexed[row["unit"],row["system"],row["staff"],k[1]].append((k,row))
    supported=defaultdict(list)
    for (unit,system,measure,_,_),cases in groups.items():
        graph=book_page(unit).find(f"page/system[@id='{system}']")
        if graph is None:
            continue
        for case in cases:
            if case["extra"] or case["node"].findtext("lyric/text")!=case["word"]:
                continue
            source=physical_item(graph,case)
            if source is None:
                continue
            for displaced in (False,True):
                for peer in split_donor_candidates(case,indexed,already_split,events,native,confirmed_columns,
                                                   displaced=displaced):
                    donor=physical_donor(graph,peer,ledger_rows,already_split)
                    if donor is None:
                        continue
                    evidence=None if displaced or peer["added_comma"] else printed_word_overlap(
                        job,case,peer,source[1],donor[1],frames)
                    if not evidence and len(case["tokens"])<=3:
                        evidence=transferred_word_overlap(job,case,peer,source[1],donor[1],frames)
                    if evidence:
                        supported[case["node"]].append((case,peer,source[0].get("id"),donor[0],evidence))
                if supported[case["node"]]:
                    break
    changed=[]
    for options in supported.values():
        if len(options)!=1:
            continue
        [(case,peer,source_id,donor_ids,evidence)]=options
        if any(event["node"] is not case["node"] and event["node"].find("lyric") is not None for event in case["events"]):
            continue
        tokens=case["tokens"]
        if evidence.get("comma_glyph"):
            tokens=[*tokens[:-1],tokens[-1]+"，"]
        apply(case,tokens)
        changed.append(dict(part=case["part"],measure=case["measure"],word=case["word"],witness=peer["part"],
                            target_source_id=source_id,witness_source_ids=donor_ids,**evidence))
    return changed


def certified_fragments(graph,cases,verified_boxes,raw):
    """A partial OCR duplicate can only occupy ink inside the proven printed character."""
    certified=[]
    for case,boxes in zip(cases,verified_boxes):
        for i,entry in case["extra"].items():
            event=case["events"][i]["node"]
            text=entry.findtext("text","")
            if event.findall("lyric")!=[entry] or text==case["tokens"][i]:
                return None
            x,y,w,h=boxes[i]
            staff=case["rows"][i]["staff"]
            hits=[]
            for item in graph.findall("sig/inters/lyric-item"):
                if item.get("staff")!=staff or item.get("value")!=text:
                    continue
                u,v,fw,fh=box(item)
                if (x-2<=u and v>=y-2 and u+fw<=x+w+2 and v+fh<=y+h+2
                        and 0<fw<=.65*w and 0<fh<=h+2):
                    hits.append((item.get("id"),u,v,fw,fh))
            if len(hits)!=1:
                return None
            [(identity,u,v,fw,fh)]=hits
            char=list(raw.crop((x-2,y-2,x+w+2,y+h+2)).getdata())
            fragment=list(raw.crop((u,v,u+fw,v+fh)).getdata())
            ratios=[sum(k<t for k in fragment)/max(1,sum(k<t for k in char)) for t in (128,160,208)]
            if not all(.12<=value<=.65 for value in ratios):
                return None
            certified.append(dict(part=case["part"],measure=case["measure"],note_index=int(case["rows"][i]["note_index"]),
                                  target=event,entry=entry,item=identity,old_text=text,
                                  fragment_bounds=[u,v,fw,fh],full_character=case["tokens"][i],
                                  ink_fractions=ratios))
    return certified


def recover(root,job):
    job=Path(job)
    sources=job/"source-syllable-evidence/source-characters.json"
    receipt=job/"source-native-notation.json"
    if not sources.is_file() or not receipt.is_file():
        return root,{"changes":[]}
    info=read(receipt)
    native_xml=Path(info["accepted_native_xml"])
    ledger=native_xml.with_name("note-ledger.tsv")
    archive=Path(info["accepted_native_book"])
    if not ledger.is_file() and not info["stages"]:
        native_xml=job/"source-native-notation/curve-overlap-ledger/result.mxl"
        ledger=native_xml.with_name("note-ledger.tsv")
    if not all(f.is_file() for f in (archive,native_xml,ledger)):
        return root,{"changes":[]}
    frames={(str(v["unit"]),v["system"],v["staff"]):v for v in read(sources)["scopes"]}
    with ledger.open(encoding="utf8",newline="") as stream:
        mapped=defaultdict(list)
        for row in csv.DictReader(stream,delimiter="\t"):
            if row["repeat_copy"]=="false" and row["head"]!="0":
                mapped[row["part"],row["measure"],int(row["note_index"])].append(row)
    ledger_rows={k:v[0] for k,v in mapped.items() if len(v)==1}
    native,_=_score_events(score(native_xml))
    cloned=deepcopy(root)
    events,_=_score_events(cloned)
    octave_rows=defaultdict(Counter)
    clef_file=job/"source-context.json"
    if clef_file.is_file():
        clefs=read(clef_file)
        if clefs.get("state")=="restored" and clefs.get("source_native_sha256")==digest(archive):
            for change in clefs.get("octave_changes",[]):
                if change["kind"]=="pitch/octave":
                    octave_rows[change["part"],change["measure"]][change["old"],change["new"]]+=1
    rhythm_rows=defaultdict(dict)
    timing=job/"source-rhythm-context.json"
    if timing.is_file():
        rhythms=read(timing)
        if (rhythms.get("state")=="restored" and rhythms.get("all_other_score_fields_exact")
                and rhythms.get("source_book_sha256")==digest(archive)):
            for change in rhythms.get("changes",[]):
                k=change["part"],change["measure"],change["note_index"]
                row=ledger_rows.get(k)
                if (row is not None and change.get("source_sha256")==row["source_sha256"]
                        and change.get("lyrics_pitch_and_other_note_fields_preserved")
                        and change.get("kind") in {"source_flag_recovery","source_absent_dot_removal"}):
                    rhythm_rows[k[:2]][k[2]]=change["duration_before"],change["duration_after"]
    confirmed_columns=exact_column_score(native,events,octave_rows,rhythm_rows)
    groups=defaultdict(list)
    for c in packed_candidates(root,events,native,ledger_rows,confirmed_columns):
        row=c["rows"][0]
        groups[row["unit"],row["system"],c["measure"],c["events"][0]["start"],len(c["tokens"])].append(c)
    already_split={k:v["node"].findtext("lyric/text","") for k,v in events.items()
                   if len(v["node"].findall("lyric"))==1 and
                   len(v["node"].findtext("lyric/text","").rstrip(PUNCTUATION))==1}
    changes=[]
    with ZipFile(archive) as book:
        @lru_cache(maxsize=2)
        def page(unit):
            return ET.fromstring(book.read(f"sheet#{unit}/sheet#{unit}.xml"))
        for (unit,system,measure,_,_),cases in groups.items():
            graph=page(unit).find(f"page/system[@id='{system}']")
            if graph is None:
                continue
            recorded=[(c,physical_item(graph,c)) for c in cases]
            confirmed=[(c,mark) for c,mark in recorded if mark is not None]
            if len(confirmed)!=2:
                continue
            ((a,first),(b,second))=confirmed
            cases=(a,b)
            recorded=(first,second)
            differences=sum(l!=r for l,r in zip(a["tokens"],b["tokens"]))
            punctuation=lambda word:word[len(word.rstrip(PUNCTUATION)):]
            if differences>1 or punctuation(a["word"])!=punctuation(b["word"]):
                continue
            if a["part"]==b["part"] or abs(int(a["rows"][0]["staff"])-int(b["rows"][0]["staff"]))!=1:
                continue
            if any(abs(int(i["x"])-int(j["x"]))>2 or e["start"]!=d["start"]
                   for i,j,e,d in zip(a["rows"],b["rows"],a["events"],b["events"])):
                continue
            original=job/f"unit-{int(unit):03d}.png"
            page_hash=digest(original)
            if not {c["rows"][0]["source_sha256"] for c in cases}=={page_hash}:
                continue
            proofs=[]
            with Image.open(original) as picture:
                raw=picture.convert("L")
            for index,case in enumerate(cases):
                frame=frames.get((unit,system,case["rows"][0]["staff"]))
                boxes=characters(frame,case,.95) if frame else None
                if not boxes:
                    continue
                source_box=recorded[index][1]
                peer_box=recorded[1-index][1]
                if any(not(source_box[0]-2<=x and x+w<=source_box[0]+source_box[2]+2
                           and source_box[1]-2<=y and y+h<=source_box[1]+source_box[3]+2)
                       for x,y,w,h in boxes):
                    continue
                delta=peer_box[1]-source_box[1]
                cuts=(128,160,208)
                low,dx,dy,metrics=match_boxes(raw,boxes,delta,cuts)
                if not characters(frame,case) or low<.94:
                    if differences:
                        continue
                    # The same complete native text in both staves justifies a read with
                    # slightly lower OCR confidence only if darker masks match more closely.
                    cuts=(128,140,160,208)
                    low,dx,dy,metrics=match_boxes(raw,boxes,delta,cuts)
                    if not all(min(v[:3])>=.97 and v[3]>=.92 for v in metrics):
                        other=cases[1-index]
                        other_frame=frames.get((unit,system,other["rows"][0]["staff"]))
                        other_boxes=characters(other_frame,other,.95) if other_frame else None
                        if not characters(frame,case,.98) or not other_boxes:
                            continue
                        # A light background mark can enter the highest threshold on
                        # only one staff. Require independent OCR and match the dark
                        # printed strokes at three levels in their own note columns.
                        cuts=(96,128,160)
                        low,dx,dy,metrics=match_boxes(raw,boxes,delta,cuts)
                        if any(v[0]<.94 or min(v[1:])<.97 for v in metrics):
                            continue
                        if any(abs(x+dx-ox)>2 or abs(y+delta+dy-oy)>2
                               or abs(w-ow)>2 or abs(h-oh)>2
                               for (x,y,w,h),(ox,oy,ow,oh) in zip(boxes,other_boxes)):
                            continue
                registered=[[x+dx,y+delta+dy,w,h] for x,y,w,h in boxes]
                verified=[boxes,registered] if index==0 else [registered,boxes]
                fragments=certified_fragments(graph,cases,verified,raw)
                if fragments is not None:
                    proofs.append((low,case,boxes,metrics,[dx,delta+dy],cuts,fragments))
            if len({case["word"] for _,case,*_ in proofs})!=1:
                continue
            _,case,boxes,metrics,shift,cuts,fragments=max(proofs,key=lambda proof:proof[0])
            if any(c["node"].findtext("lyric/text")!=c["word"]
                   or any(e["node"] is not c["node"] and e["node"].findall("lyric")!=
                          ([c["extra"][i]] if i in c["extra"] else [])
                          for i,e in enumerate(c["events"])) for c in cases):
                continue
            for fragment in fragments:
                fragment.pop("target").remove(fragment.pop("entry"))
            for c in cases:
                apply(c,case["tokens"])
            changes.append(dict(parts=[c["part"] for c in cases],measure=measure,word=case["word"],
                                original_words=[c["word"] for c in cases],source_sha256=page_hash,
                                glyph_boxes=boxes,source_identities=[n.get("id") for n,_ in recorded],
                                peer_ink_overlap=metrics,peer_shift=shift,ink_thresholds=cuts,
                                removed_overlapping_lyric_fragments=fragments))
        split_peer=[]
        while True:
            layer=repair_with_split_peer(groups,page,job,events,native,ledger_rows,confirmed_columns,frames,already_split)
            if not layer:
                break
            split_peer.extend(layer)
            already_split={k:v["node"].findtext("lyric/text","") for k,v in events.items()
                           if len(v["node"].findall("lyric"))==1 and
                           len(v["node"].findtext("lyric/text","").rstrip(PUNCTUATION))==1}
    duplicates=remove_duplicate_continuation(events,native,ledger_rows,frames,job)
    return (cloned if changes or duplicates or split_peer else root),{
        "changes":changes,"removed_continuation_duplicates":duplicates,"split_peer_changes":split_peer}

