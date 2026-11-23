"""Find a source half-chord dropped under an erroneous half-rest and a merged tie."""
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile
import hashlib
import json
import sys
import cv2
import numpy as np


def box(node):
    n=node.find('bounds')
    return tuple(int(n.get(k)) for k in 'xywh')


def find_pixels(image, first, suspect, following, staff):
    fx,fy,fw,fh=box(first)
    ux,uy,uw,uh=box(suspect)
    tx,ty,tw,th=box(following)
    ys=[float(line.find('point').get('y')) for line in staff.findall('lines/line')]
    if len(ys)!=5:
        return None
    space=float(np.median(np.diff(ys)))
    if not .30*space<=fy-uy<=.65*space or not .30<=(ux-fx)/(tx-fx)<=.65:
        return None
    if abs(fy-ty)>.20*space or abs(fh-uh)>3:
        return None
    x0,x1=round(ux-2.1*space),ux+3
    y0,y1=round(fy-.30*space),round(fy+fh+.30*space)
    stem_x=ux+1
    start_y=round(fy+fh+.28*space)
    end_y=round(fy+fh+2.8*space)
    if min(x0,y0)<0 or max(fx+fw,tx+tw,x1)>=image.shape[1] or end_y>=image.shape[0]:
        return None

    def off_staff(samples):
        return np.logical_and.reduce([abs(samples-y)>2.75 for y in ys])

    checks=[]
    for t in (128,160,208):
        template=(image[fy:fy+fh,fx:fx+fw]<t).astype(np.uint8)
        view=(image[y0:y1,x0:x1]<t).astype(np.uint8)
        if any(a<b for a,b in zip(view.shape,template.shape)):
            return None
        _,corr,_,(dx,dy)=cv2.minMaxLoc(cv2.matchTemplate(view,template,cv2.TM_CCOEFF_NORMED))
        read=view[dy:dy+fh,dx:dx+fw]
        total=np.logical_and(template,read).sum()
        coord=(x0+dx,y0+dy,fw,fh)
        recall=float(total/max(1,template.sum()))
        if (corr<.76 or recall<.95 or abs(coord[1]-fy)>2
                or not .75*space<=ux-coord[0]<=1.3*space):
            return None
        stem=[]
        for x in range(stem_x-1,stem_x+2):
            stem.append(float((image[start_y:end_y,x]<t).mean()))
        if min(stem)<.94:
            return None
        half_band=np.arange(round(fy+fh+.86*space),round(fy+fh+1.46*space))
        band=half_band[off_staff(half_band)]
        support=[]
        for a,b in [(fx+fw/2,coord[0]+fw/2),(coord[0]+fw/2,tx+tw/2)]:
            xs=np.rint(np.linspace(a+(b-a)*.36,a+(b-a)*.63,31)).astype(int)
            support.append(float(np.any(image[band[:,None],xs]<t,axis=0).mean()))
        # In the gap below the middle chord, a single continuous arc would still
        # have ink away from the stem. Two true arcs bend toward the new head.
        xs=np.r_[stem_x-11:stem_x-4,stem_x+5:stem_x+12]
        gap=float(np.any(image[band[:,None],xs]<t,axis=0).mean())
        if min(support)<.95 or gap>.12:
            return None
        checks.append(dict(threshold=t,bounds=coord,head_correlation=corr,head_recall=recall,
                           stem=stem,two_curve_bulges=support,empty_below_middle=1-gap))
    if len({tuple(v['bounds']) for v in checks})!=1:
        return None
    return checks


def scan(book,source):
    results=[]
    with ZipFile(book) as archive:
        for member in archive.namelist():
            if not member.startswith('sheet#') or not member.endswith('.xml'):
                continue
            unit=int(member.split('/')[0][6:])
            image=None
            path=source/f'unit-{unit:03d}.png'
            for system in E.fromstring(archive.read(member)).findall('page/system'):
                nodes={n.get('id'):n for n in system.findall('sig/inters/*')}
                relations=system.findall('sig/relations/relation')
                members=defaultdict(list)
                arc_ends=defaultdict(dict)
                stems={}
                for rel in relations:
                    a,b=rel.get('source'),rel.get('target')
                    if rel.find('containment') is not None:
                        members[a].append(b)
                    if rel.find('chord-stem') is not None:
                        stems[a]=b
                    slur=rel.find('slur-head')
                    if slur is not None:
                        arc_ends[a][slur.get('side')]=b
                heads_to_chords={n:c for c,heads in members.items() if nodes[c].tag=='head-chord'
                                 for n in heads if nodes[n].tag=='head'}
                for part in system.findall('part'):
                    staves=part.findall('staff')
                    if len(staves)!=1:
                        continue
                    staff=staves[0]
                    bars=part.findall('measure')
                    for current,following in zip(bars,bars[1:]):
                        if current.get('abnormal')!='true':
                            continue
                        chords=current.findtext('head-chords','').split()
                        rest_chords=current.findtext('rest-chords','').split()
                        if len(chords)!=2 or not rest_chords:
                            continue
                        first,second=sorted(chords,key=lambda c:box(nodes[c])[0])
                        if any(len(members[c])!=1 for c in (first,second)):
                            continue
                        left,up=(nodes[members[c][0]] for c in (first,second))
                        if left.get('shape')!='NOTEHEAD_VOID' or up.get('shape')!='NOTEHEAD_VOID':
                            continue
                        if second not in stems or box(nodes[stems[second]])[3]>=.5*box(up)[3]:
                            continue
                        voices=[v for v in current.findall('voice') if v.find('slots/entry') is not None]
                        if len(voices)!=1:
                            continue
                        begins=[n for n in voices[0].findall('slots/entry') if n.find('value').get('status')=='BEGIN']
                        if len(begins)!=2 or begins[0].find('value').get('chord')!=first:
                            continue
                        fake=begins[1].find('value').get('chord')
                        if (fake not in rest_chords or len(members[fake])!=1
                                or nodes[members[fake][0]].get('shape')!='HALF_REST'):
                            continue
                        stack=system.find(f"stack[@id='{current.get('id')}']")
                        if stack is None or stack.get('expected')!='1':
                            continue
                        clock={n.get('id'):Fraction(n.get('time-offset')) for n in stack.findall('slot')}
                        if [clock.get(n.findtext('key')) for n in begins]!=[Fraction(0),Fraction(1,2)]:
                            continue
                        updated=[(arc,ends['RIGHT']) for arc,ends in arc_ends.items()
                                 if nodes[arc].get('tie')=='true' and nodes[arc].get('above')=='false'
                                 and ends.get('LEFT')==left.get('id') and ends.get('RIGHT') in heads_to_chords]
                        if len(updated)!=1:
                            continue
                        arc,end=updated[0]
                        end_chord=heads_to_chords[end]
                        if end_chord not in following.findtext('head-chords','').split():
                            continue
                        right=nodes[end]
                        if right.get('pitch')!=left.get('pitch') or int(up.get('pitch'))!=int(left.get('pitch'))-1:
                            continue
                        if image is None:
                            image=cv2.imdecode(np.fromfile(path,np.uint8),0)
                        witness=find_pixels(image,left,up,right,staff)
                        if witness:
                            results.append(dict(unit=unit,system=system.get('id'),staff=staff.get('id'),
                                 native_part=part.get('id'),stack=current.get('id'),first=first,upper_chord=second,
                                 upper_head=up.get('id'),short_stem=stems[second],false_rest=fake,merged_tie=arc,
                                 following_head=end,source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                 pixel_witnesses=witness))
    return results






def rebuild_from_witness(book, dest, source, proof):
    """Apply one verified foreground reconstruction to a new OMR archive."""
    member=f"sheet#{proof['unit']}/sheet#{proof['unit']}.xml"
    assert hashlib.sha256((source/f"unit-{proof['unit']:03d}.png").read_bytes()).hexdigest()==proof['source_sha256']
    with ZipFile(book) as original:
        sheet=E.fromstring(original.read(member))
        system=sheet.find(f"page/system[@id='{proof['system']}']")
        part=system.find(f"part[@id='{proof['native_part']}']")
        staff=part.find(f"staff[@id='{proof['staff']}']")
        measure=part.find(f"measure[@id='{proof['stack']}']")
        voice=[v for v in measure.findall('voice') if v.find('slots/entry') is not None]
        assert len(voice)==1
        slots=voice[0].findall('slots/entry')
        graph=system.find('sig/inters')
        edges=system.find('sig/relations')
        nodes={n.get('id'):n for n in graph}
        [known]=[r for r in edges if r.get('source')==proof['first'] and r.find('containment') is not None]
        first=nodes[known.get('target')]
        up=nodes[proof['upper_head']]
        following=nodes[proof['following_head']]
        stem=nodes[proof['short_stem']]
        chord=nodes[proof['upper_chord']]
        joined=nodes[proof['merged_tie']]
        assert {x.find('value').get('chord') for x in slots}=={proof['first'],proof['false_rest']}
        ys=[float(line.find('point').get('y')) for line in staff.findall('lines/line')]
        space=float(np.median(np.diff(ys)))
        x,y,w,h=proof['pixel_witnesses'][1]['bounds']
        ux,uy,uw,uh=box(up)
        fx,fy,fw,fh=box(first)
        tx,ty,tw,th=box(following)
        assert up.get('staff')==first.get('staff')==following.get('staff')==staff.get('id')
        newid,nextid=[str(int(sheet.get('last-persistent-id'))+i) for i in (1,2)]
        assert newid not in nodes and nextid not in nodes
        sheet.set('last-persistent-id',nextid)

        def bounds(node,values):
            node.attrib.update(dict(zip('xywh',map(str,values))))

        note=E.SubElement(graph,'head',pitch=first.get('pitch'),shape='NOTEHEAD_VOID',grade='0.95',
                          staff=staff.get('id'),id=newid)
        bounds(E.SubElement(note,'bounds'),(x,y,w,h))
        staff.find('notes').text+=' '+newid
        line_x=ux+1
        start=round(uy+.44*space)
        end=round(start+4.02*space)
        stem.attrib.pop('glyph',None)
        stem.attrib.update(width='3',grade='0.95')
        bounds(stem.find('bounds'),(line_x-1,start,3,end-start+1))
        for key,py in [('p1',start),('p2',end)]:
            stem.find('median/'+key).attrib.update(x=str(line_x),y=str(py))
        bounds(chord.find('bounds'),(x,uy,ux+uw-x,end-uy+1))
        [link]=[r for r in edges if r.get('source')==up.get('id') and r.get('target')==stem.get('id')
                and r.find('head-stem') is not None]
        side=link.find('head-stem')
        side.attrib.update({'head-side':'LEFT','consistency':'1.4','dx':'0','dy':'0','grade':'1'})
        side.find('extension-point').attrib.update(x=str(line_x),y=str(uy))
        attach=E.SubElement(edges,'relation',source=newid,target=stem.get('id'))
        side=E.SubElement(attach,'head-stem',attrib={'head-side':'RIGHT','consistency':'1.4','dx':'0','dy':'0','grade':'1'})
        E.SubElement(side,'extension-point',x=str(line_x),y=str(y))
        E.SubElement(E.SubElement(edges,'relation',source=chord.get('id'),target=newid),'containment')
        E.SubElement(E.SubElement(edges,'relation',source=up.get('id'),target=newid),'head-head',grade='1')

        def arc(node, mark, ax,ay,bx,by):
            inside=round(max(ay,by)+1.7*space)
            pts=dict(zip(('x1','y1','ctrlx1','ctrly1','ctrlx2','ctrly2','x2','y2'),
                         map(str,(ax,ay,round(ax+.22*(bx-ax)),inside-2,
                                   round(ax+.77*(bx-ax)),inside,bx,by))))
            bounds(node,(ax,min(ay,by)-2,bx-ax+1,round(1.45*space)))
            mark.attrib.update(pts)

        joined.attrib.pop('glyph',None)
        floor=y+h
        arc(joined.find('bounds'),joined.find('curve'),fx+round(.73*fw),fy+fh+3,line_x-5,floor+2)
        [openarc]=[r for r in edges if r.get('source')==joined.get('id') and r.find("slur-head[@side='RIGHT']") is not None]
        assert openarc.get('target')==proof['following_head']
        openarc.set('target',newid)
        second=E.SubElement(graph,'slur',above='false',tie='true',shape='SLUR_BELOW',grade='0.95',id=nextid)
        arc(E.SubElement(second,'bounds'),E.SubElement(second,'curve'),
            line_x+5,floor+3,tx-round(.61*space),ty+round(1.22*space))
        for head,side in ((newid,'LEFT'),(following.get('id'),'RIGHT')):
            E.SubElement(E.SubElement(edges,'relation',source=nextid,target=head),'slur-head',side=side,grade='1')
        for slot in slots:
            event=slot.find('value')
            if event.get('chord')==proof['false_rest']:
                event.set('chord',chord.get('id'))
        with ZipFile(dest,'x') as new:
            for member_item in original.infolist():
                payload=E.tostring(sheet,encoding='UTF-8',xml_declaration=True) if member_item.filename==member else original.read(member_item)
                new.writestr(member_item,payload)
    return newid



if __name__=='__main__':
    book,source,dest,report=map(Path,sys.argv[1:])
    recorded=hashlib.sha256(book.read_bytes()).hexdigest()
    findings=scan(book,source)
    if findings:
        if len(findings)!=1:
            raise ValueError('multiple sustained chord candidates require separate source validation')
        [finding]=findings
        finding['recovered_head']=rebuild_from_witness(book,dest,source,finding)
    if hashlib.sha256(book.read_bytes()).hexdigest()!=recorded:
        raise ValueError('original source changed')
    with report.open('x',encoding='utf8') as f:
        json.dump(dict(source_book_sha256=recorded,findings=findings),f,indent=2)
