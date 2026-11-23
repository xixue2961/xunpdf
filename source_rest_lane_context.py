
"""Keep the source-proven rest lane after the remaining export passes."""
from copy import deepcopy
import json
from pathlib import Path
from xml.etree import ElementTree as E

from source_cross_staff_repair import _musical_identity, measures
from source_native_notation import canonical


def project_engravings(root, lane):
    pid, printed=lane['part'],lane['musicxml_measure']
    wanted=lane['physical_measure']
    fixed=deepcopy(root)
    options=[(m,d) for i,m,d in measures(fixed,pid,printed) if i==wanted]
    if len(options)!=1:
        raise ValueError('rest lane no longer has a unique source measure')
    bar,divisions=options[0]
    if divisions!=lane['divisions'] or [list(v) for v in _musical_identity(bar,divisions)]!=[list(v) for v in lane['anchor']]:
        raise ValueError('rest lane no longer fits the current note pitches and offsets')
    frag=[E.fromstring(node) for node in lane['fragment']]
    if [n.tag for n in frag]!=['forward','note','note','forward','note','backup']:
        raise ValueError('rest lane content does not match the native verification')
    cursor=next(i for i,n in enumerate(bar) if n.tag=='backup')+1
    if [canonical(n) for n in list(bar)[cursor:cursor+len(frag)]]==[canonical(n) for n in frag]:
        return root,False
    if bar[cursor].findtext('staff')!='2':
        raise ValueError('expected unambiguous transition to the lower staff')
    if int(bar[cursor-1].findtext('duration'))!=int(frag[-1].findtext('duration')):
        raise ValueError('the engraved lane does not fit this bar')
    if sum(int(n.findtext('duration')) for n in frag[:-1])!=int(frag[-1].findtext('duration')):
        raise ValueError('engraved rests would shift other notes')
    if any(n.find('rest') is None or n.find('pitch') is not None for n in frag if n.tag=='note'):
        raise ValueError('engraving supplementation cannot add pitched notes')
    for i,n in enumerate(frag):
        bar.insert(cursor+i,n)
    if [list(v) for v in _musical_identity(bar,divisions)]!=[list(v) for v in lane['anchor']]:
        raise ValueError('engraved rest projection changed a note onset')
    return fixed,True


def refine_source_rest_lanes(root,job,emit=print):
    job=Path(job)
    data_path=job/'source-native-notation.json'
    stages=[]
    if data_path.is_file():
        data=json.loads(data_path.read_text(encoding='utf8'))
        if data['state'] in ('restored','restored_with_pending') and data.get('complete_XML_and_source_note_mapping_preserved_on_reload'):
            stages=[s for s in data['stages'] if s['stage']=='cross-staff-piano' and 'engraved_rest_lane' in s]
    report=dict(state='no_source_rest_lane',changes=[],pending=[])
    output=root
    for stage in stages:
        lane=stage['engraved_rest_lane']
        try:
            output,changed=project_engravings(output,lane)
            if changed:
                report['changes'].append(dict(part=lane['part'],physical_measure=lane['physical_measure'],rests=3))
        except (ValueError,StopIteration,AttributeError,TypeError,IndexError) as exc:
            report['pending'].append(dict(part=lane['part'],physical_measure=lane['physical_measure'],reason=str(exc)))
    if stages:
        report['state']='needs_review' if report['pending'] else 'restored' if report['changes'] else 'already_present'
    if report['changes']:
        emit('已保留跨谱表伴奏上谱表的原谱休止')
    (job/'source-rest-lanes.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return output,report

