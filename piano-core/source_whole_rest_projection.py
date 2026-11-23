"""Project a source-proven whole note over an exporter-generated empty staff."""
from pathlib import Path
import copy
import hashlib
from fractions import Fraction

from source_piano_merge import address,canon,events,native_map,table


def locate(tree,key):
    part,number,index=key
    parts=[p for p in tree.findall('part') if p.get('id')==part]
    if len(parts)!=1 or not any(n.text=='2' for n in parts[0].findall('measure/attributes/staves')):
        raise ValueError('source whole note has no unique piano part')
    measures=[m for m in parts[0].findall('measure') if m.get('number')==number]
    if len(measures)!=1:
        raise ValueError('source whole note has no unique measure occurrence')
    measure,=measures
    return measure,measure.findall('note')[index]


def same_measure_except_note(first,last,a,b):
    x=copy.deepcopy(first)
    y=copy.deepcopy(last)
    x.remove(x.findall('note')[a])
    y.remove(y.findall('note')[b])
    return canon(x)==canon(y)


def project_whole_rests(original,refined,control,native,old_ledger,new_ledger,plan,source):
    """Replace only an isolated synthetic whole-measure rest with authenticated ink."""
    old_rows=table(old_ledger);new_rows=table(new_ledger)
    physical_before=native_map(old_rows);physical_after=native_map(new_rows)
    if not physical_before.keys()<=physical_after.keys():
        raise ValueError('whole-note fallback would hide physical note loss')
    new=[physical_after[k] for k in physical_after.keys()-physical_before.keys()]
    if not new or any(r['shape']!='WHOLE_NOTE' or r['repeat_copy']!='false' for r in new):
        raise ValueError('fallback requires only newly sourced independent whole notes')
    with Path(plan).open(encoding='utf8') as file:
        evidence=[line.split('\t') for line in file.read().splitlines() if line]
    result=copy.deepcopy(original)
    base_events=events(original);candidate_events=events(refined)
    affected=set()
    for row in new:
        key=address(row)
        if key[:2] in affected:
            raise ValueError('multiple whole notes share the same fallback measure')
        w=[p for p in evidence if (p[0],p[1],p[3],*p[4:8],p[11])==
           (row['unit'],row['system'],row['staff'],row['x'],row['y'],row['w'],row['h'],row['source_sha256'])]
        if len(w)!=1:
            raise ValueError('native candidate does not match its exact physical whole-note plan')
        image=Path(source)/f"unit-{int(row['unit']):03d}.png"
        if hashlib.sha256(image.read_bytes()).hexdigest()!=row['source_sha256']:
            raise ValueError('source page changed after verification')
        controls=[p for p in old_rows if (p['part'],p['measure'],p['staff_number'])==
                      (row['part'],row['measure'],row['staff_number'])]
        if any(p['head']!='0' or p['native_note_class']!='FakeRest' or p['shape']!='WHOLE_REST' for p in controls):
            raise ValueError('a physical note or rest occupies that piano staff')
        cm,c=locate(control,key)
        nm,n=locate(native,key)
        _,a=locate(original,key)
        _,b=locate(refined,key)
        if not same_measure_except_note(cm,nm,key[2],key[2]) or canon(a)!=canon(c) or canon(b)!=canon(n):
            raise ValueError('whole-note replacement is not the only proven change in its native measure')
        if a.find("rest[@measure='yes']") is None or b.find('pitch') is None or b.findtext('type')!='whole':
            raise ValueError('missing independent whole-measure rest and note')
        if any(a.findtext(k)!=b.findtext(k) for k in ('staff','voice','duration')):
            raise ValueError('whole-note projection changes rhythm or voice')
        markers=(b.find('chord'),b.find('tie'),b.find('dot'),b.find('time-modification'))
        if any(v is not None for v in markers) or base_events[key][:2]!=(Fraction(0),Fraction(4)) or \
                candidate_events[key][:2]!=base_events[key][:2]:
            raise ValueError('whole note does not occupy its complete verified 4/4 span')
        m,target=locate(result,key)
        staff_notes=[node for node in m.findall('note') if node.findtext('staff','1')==row['staff_number']]
        if staff_notes!=[target]:
            raise ValueError('caller piano staff is not physically empty')
        pos=list(m).index(target)
        m.remove(target)
        m.insert(pos,copy.deepcopy(b))
        affected.add(key[:2])
    return result,dict(whole_rests_replaced=len(affected),affected_measures=sorted(affected),
                       retained_other_caller_nodes=True,mode='source-whole-rest-projection')
