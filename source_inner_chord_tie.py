
"""Restore a missing middle tie only when the original arc fits the noteheads."""
from itertools import product
import numpy as np

from source_chord_tie import add_tie, staff_lines


def pitch(note):
    return tuple(note.findtext('pitch/'+tag) for tag in ('step','alter','octave'))


def boxed_row(row):
    return [int(row[tag]) for tag in 'xywh']


def arc_between(image,staff,previous,following,next_chord_x,space):
    """Check the isolated ink between the displaced middle head and the next chord."""
    x0=previous[0]+previous[2]
    span=next_chord_x-x0
    if not 1.65*space<=span<=3.25*space or abs(following[1]-previous[1])>.23*space:
        return None
    u=np.linspace(.10,.90,51)
    cols=np.rint(x0+u*span).astype(int)
    if cols.min()<0 or cols.max()>=image.shape[1]:
        return None
    lines=staff_lines(staff,cols)
    baseline=previous[1]*(1-u)+following[1]*u
    threshold_radius=np.array([-1,0,1])[:,None]

    def coverage(model,threshold):
        ys=np.rint(model).astype(int)[None,:]+threshold_radius
        if ys.min()<0 or ys.max()>=image.shape[0]:
            return []
        line_clear=(np.abs(ys[None,:,:]-lines[:,None,:])>2.0).all(axis=0)
        support=((image[ys,cols]<threshold)&line_clear).any(axis=0)
        return [float(zone.mean()) for zone in np.array_split(support,5)]

    matches=[]
    for lift,peak,fall in product((.10,.16,.22,.28,.34),(.29,.35,.41,.47,.53,.59),(-.10,-.04,.02,.08,.14,.20)):
        linear=baseline+space*(lift*(1-u)+fall*u)
        curved=linear-4*space*peak*u*(1-u)
        supports=[coverage(curved,t) for t in (160,208)]
        if not all(supports) or min(map(min,supports))<.85:
            continue
        flat=coverage(linear,208)
        if not flat or sum(value<=.35 for value in flat[1:4])<2:
            continue
        quality=min(map(min,supports))
        matches.append((quality,sum(map(sum,supports)),dict(
            threshold_supports=supports,straight_support=flat,span=[int(cols.min()),int(cols.max())],
            vertical=[round(float(curved.min())),round(float(curved.max()))],shape=[lift,peak,fall])))
    return max(matches,key=lambda item:item[:2])[2] if matches else None


def arcs_already_recognized(system):
    ties={n.get('id'):{} for n in system.findall("sig/inters/slur[@tie='true']")}
    for rel in system.findall('sig/relations/relation'):
        slur=rel.find('slur-head')
        if slur is not None and rel.get('source') in ties:
            ties[rel.get('source')].setdefault(slur.get('side'),[]).append(rel.get('target'))
    return {(link['LEFT'][0],link['RIGHT'][0]) for link in ties.values()
            if len(link.get('LEFT',[]))==len(link.get('RIGHT',[]))==1}


def restore_supported_inner_ties(bar,indexed,system,image,space):
    upper,_=indexed
    upper_notes=[n for n in bar.findall('note') if n.findtext('staff','1')=='1']
    groups=[]
    offset=0
    for _,chord,old in upper:
        count=len(old)
        groups.append((chord,list(zip((row for row,_ in old),upper_notes[offset:offset+count],strict=True))))
        offset+=count
    assert offset==len(upper_notes)
    if not groups:
        return []
    native_ties=arcs_already_recognized(system)
    proofs=[]
    for (left_id,left),(right_id,right) in zip(groups,groups[1:]):
        if len(left)!=len(right) or len(left)!=3:
            continue
        if [pitch(n) for _,n in left]!=[pitch(n) for _,n in right]:
            continue
        first=sorted(zip(left,right,strict=True),key=lambda pair:int(pair[0][0]['y']))
        upper_pair,middle,lower_pair=first
        if not all(pair[0][1].find("tie[@type='start']") is not None
                   and pair[1][1].find("tie[@type='stop']") is not None
                   and (pair[0][0]['head'],pair[1][0]['head']) in native_ties
                   for pair in (upper_pair,lower_pair)):
            continue
        (lrow,lnote),(rrow,rnote)=middle
        if (lnote.find('tie') is not None or rnote.find('tie') is not None
            or lnote.find('notations/tied') is not None or rnote.find('notations/tied') is not None
            or any(row['staff']!=lrow['staff'] for group in (left,right) for row,_ in group)):
            continue
        staff=system.find(f"part/staff[@id='{lrow['staff']}']")
        if staff is None or len(staff.findall('lines/line'))!=5:
            continue
        right_x=min(int(row['x']) for row,_ in right)
        lhs,rhs=boxed_row(lrow),boxed_row(rrow)
        measured=arc_between(image,staff,lhs,rhs,right_x,space)
        if measured is None:
            continue
        add_tie(lnote,'start')
        add_tie(rnote,'stop')
        proofs.append(dict(chords=[left_id,right_id],heads=[lrow['head'],rrow['head']],
                           staff=lrow['staff'],pitch=pitch(lnote),space=space,
                           left_head=lhs,right_head=rhs,next_chord_x=right_x,ink=measured))
    return proofs

