"""Audit exported wedge pairs without inventing missing source endpoints."""
from pathlib import Path
import json,re

def audit_wedge_integrity(root, job=None):
    problems=[]
    pairs=0
    for part in root.findall('part'):
        active={}
        for measure in part.findall('measure'):
            for wedge in measure.findall('./direction/direction-type/wedge'):
                number=wedge.get('number','1')
                kind=wedge.get('type')
                location=dict(part=part.get('id'),measure=measure.get('number'),number=number)
                if not number.isdigit() or not 1<=int(number)<=16:
                    problems.append(dict(location,kind='invalid_wedge_number'))
                if kind in ('crescendo','diminuendo'):
                    if number in active:
                        problems.append(dict(location,kind='overlapping_wedge_number',prior_start=active[number]))
                    active[number]=measure.get('number')
                elif kind=='stop':
                    if number not in active:
                        problems.append(dict(location,kind='wedge_stop_without_start'))
                    else:
                        active.pop(number)
                        pairs+=1
        for number,measure in active.items():
            problems.append(dict(part=part.get('id'),measure=measure,number=number,kind='wedge_start_without_stop'))
    pending={}
    if job is not None:
        for log in sorted(Path(job).glob('*.log')):
            with log.open(encoding='utf8',errors='replace') as stream:
                for line_number,line in enumerate(stream,1):
                    marker=line.find('SOURCE_WEDGE_PENDING ')
                    if marker<0:continue
                    message=line[marker:].strip()
                    entry=pending.setdefault(message,dict(message=message,locations=[]))
                    entry['locations'].append(dict(log=log.name,line=line_number))
    report=dict(state='needs_source_review' if problems or pending else 'paired',paired_wedges=pairs,export_issues=problems,native_pending_diagnostics=list(pending.values()),scope='Paired wedge export integrity only; pending diagnostics are not an error count or music accuracy percentage; no source object or score field is changed')
    if job is not None:
        (Path(job)/'wedge-integrity.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return report
