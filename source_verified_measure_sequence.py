
"""Restore continuous export numbering after proven phantom multirests."""

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile

from source_native_notation import sha


def recover(root, job):
    report=dict(state='no_change',changes=[])
    parts=root.findall('part')
    measures=[p.findall('measure') for p in parts]
    if not measures or not measures[0]:
        return root,report
    labels=[m.get('number','') for m in measures[0]]
    if len(set(labels))==len(labels):
        return root,report
    report['state']='unproven_duplicate_numbers'
    if (any(not n.isascii() or not n.isdecimal() for n in labels)
            or any([m.get('number','') for m in ms]!=labels for ms in measures[1:])
            or any(m.get('implicit')=='yes' for ms in measures for m in ms)
            or any(n.text!='1' for n in root.findall('part/measure/attributes/measure-style/multiple-rest'))):
        return root,report
    values=list(map(int,labels))
    jumps=[b-a for a,b in zip(values,values[1:])]
    if values[0]!=1 or any(j not in (0,1) for j in jumps) or max(Counter(labels).values())>2:
        return root,report

    proof_path=Path(job)/'source-multirest-export.json'
    native_path=Path(job)/'source-native-notation.json'
    if not proof_path.is_file() or not native_path.is_file():
        return root,report
    evidence=json.loads(proof_path.read_text('utf8'))
    native=json.loads(native_path.read_text('utf8'))
    book=Path(native['accepted_native_book'])
    if evidence.get('state')!='restored' or native.get('accepted_native_sha256')!=sha(book):
        return root,report
    edits={}
    for change in evidence['changes']:
        offset=int(change['physical_measure'])-1
        if not 0<=offset<len(labels) or set(change['parts'])!={p.get('id') for p in parts}:
            continue
        source_labels=change['source']['labels']
        if source_labels and all(n==labels[offset] for _,n in source_labels):
            edits[offset]=change
    duplicates=[i for i,jump in enumerate(jumps) if jump==0]
    if any(i not in edits for i in duplicates):
        return root,report
    stacks=[]
    with ZipFile(book) as archive:
        names=sorted((n for n in archive.namelist() if n.startswith('sheet#') and n.endswith('.xml')),
                     key=lambda n:int(n.split('/')[0][6:]))
        for name in names:
            page=int(name.split('/')[0][6:])
            for system in E.fromstring(archive.read(name)).findall('page/system'):
                for block in system.findall('stack'):
                    if block.get('special')=='CAUTIONARY' and block.get('duration')=='0':
                        continue
                    stacks.append((page,system.get('id'),block))
    if len(stacks)!=len(labels):
        return root,report
    for i in duplicates:
        page,system,a=stacks[i]
        next_page,next_system,b=stacks[i+1]
        source=edits[i]['source']
        if ((page,system,a.get('id'))!=(int(source['unit']),str(source['system']),str(source['stack']))
                or (page,system)!=(next_page,next_system) or a.get('special')!='MULTI_REST'
                or abs(int(a.get('right'))-int(b.get('left')))>1):
            return root,report

    clone=deepcopy(root)
    for part in clone.findall('part'):
        for i,m in enumerate(part.findall('measure'),1):
            m.set('number',str(i))
    changes=[dict(physical_measure=i,old=old,new=str(i)) for i,old in enumerate(labels,1) if old!=str(i)]
    return clone,dict(state='restored',changes=changes,physical_measures=len(labels),
                      duplicate_pairs=[dict(old=labels[i],first=i+1,second=i+2) for i in duplicates])

