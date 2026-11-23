"""Omit repeated source top-left printed bar labels; preserve XML measures and tempo."""
from pathlib import Path
import json,csv,copy,hashlib,zipfile,xml.etree.ElementTree as E,collections,re
from math import gcd
from functools import reduce
from source_lyric_roles import _score_events,_role_anchor_signature,_music_signature
from source_expression_overlay import canonical

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def scan_printed_measure_numbers(book,source,ledger):
    rows=list(csv.DictReader(Path(ledger).open(encoding='utf8'),delimiter='\t'));bychord=collections.defaultdict(list)
    for row in rows:
        if row['repeat_copy']=='false':bychord[int(row['unit']),row['system'],row['chord']].append(row)
    candidates=[];barline_candidates=[];pending=[]
    with zipfile.ZipFile(book) as archive:
        for name in archive.namelist():
            match=re.fullmatch(r'sheet#([1-9][0-9]*)/sheet#\1.xml',name)
            if not match:continue
            unit=int(match[1]);rt=E.fromstring(archive.read(name));digest=sha(Path(source)/f'unit-{unit:03d}.png')
            for system_index,sy in enumerate(rt.findall('page/system')):
                staves=sy.findall('part/staff')
                if not staves:continue
                staff=staves[0];points=staff.findall('lines/line/point');ys=[float(p.get('y')) for p in points];top=min(ys);space=(max(ys)-top)/4;left=int(staff.get('left'));nodes={n.get('id'):n for n in sy.find('sig/inters')};relations=list(sy.find('sig/relations'));contains=collections.defaultdict(list)
                for edge in relations:
                    if edge.find('containment') is not None:contains[edge.get('source')].append(edge.get('target'))
                for word in nodes.values():
                    token=word.get('value','').strip()
                    if word.tag!='word' or not token.isdigit() or not 1<=int(token)<=9999 or word.get('staff')!=staff.get('id') or word.find('bounds') is None:continue
                    x,y,bw,bh=[int(word.find('bounds').get(k)) for k in ['x','y','w','h']];nx=(x-left)/space;ny=(top-y-bh)/space
                    line_header=(-.25<=nx<=2.5 and .5<=ny<=5 and bh<=3.5*space)
                    barlines=sorted({int(s.get(k)) for s in sy.findall('stack') for k in ('left','right')})
                    near_barline=(barlines and min(abs(x+bw/2-v) for v in barlines)<=1.4*space
                                  and .9<=ny<=3.1 and bh<=3.5*space)
                    if not (line_header or near_barline):continue
                    owners=[nodes[k] for k,ids in contains.items() if word.get('id') in ids and k in nodes and nodes[k].tag=='sentence' and nodes[k].get('role')=='Direction']
                    if len(owners)!=1 or contains[owners[0].get('id')]!=[word.get('id')]:continue
                    parent=owners[0];links=[edge.get('source') for edge in relations if edge.get('target')==parent.get('id') and edge.find('chord-sentence') is not None]
                    if len(links)!=1:pending.append(dict(unit=unit,system=sy.get('id'),word=word.get('id'),reason='printed_label_anchor_not_unique'));continue
                    mapped=bychord.get((unit,sy.get('id'),links[0]),[])
                    if not mapped:pending.append(dict(unit=unit,system=sy.get('id'),word=word.get('id'),reason='printed_label_anchor_not_exported'));continue
                    row=min(mapped,key=lambda v:int(v['note_index']))
                    if row['source_sha256']!=digest:raise ValueError('source number image/ledger changed')
                    record=dict(unit=unit,system=sy.get('id'),system_order=system_index,word=word.get('id'),
                                sentence=parent.get('id'),chord=links[0],value=token,bounds=[x,y,bw,bh],
                                normalized_left=nx,normalized_gap_above=ny,source_sha256=digest,
                                stack_count=len(sy.findall('stack')),target_note=row)
                    if line_header:candidates.append(record)
                    if near_barline and row['measure'].isdecimal() and int(token)-int(row['measure']) in (0,1):
                        barline_candidates.append(record)
    candidates.sort(key=lambda v:(v['unit'],v['system_order']));by_system=collections.Counter((v['unit'],v['system']) for v in candidates);clusters=[]
    for c in candidates:
        if by_system[c['unit'],c['system']]!=1:pending.append(dict(unit=c['unit'],system=c['system'],reason='multiple_numeric_directions_in_header'));continue
        cluster=next((g for g in clusters if abs(c['normalized_left']-g[0]['normalized_left'])<=.75 and abs(c['normalized_gap_above']-g[0]['normalized_gap_above'])<=1.25),None)
        if cluster is None:cluster=[];clusters.append(cluster)
        cluster.append(c)
    proposals=[]
    for cluster in clusters:
        runs=[];run=[]
        for c in cluster:
            if run and not 0<int(c['value'])-int(run[-1]['value'])<=max(16,2*run[-1]['stack_count']):runs.append(run);run=[]
            run.append(c)
        if run:runs.append(run)
        for run in runs:
            if len(run)>=3:
                for c in run:c['independent_system_peers']=len(run);proposals.append(c)
            else:pending.extend(dict(unit=c['unit'],system=c['system'],word=c['word'],reason='not_repeated_monotonic_top_left_bar_labels') for c in run)
    barline_candidates.sort(key=lambda v:(v['unit'],v['system_order'],v['bounds'][0]))
    series=[int(v['value']) for v in barline_candidates]
    distinct_systems={(v['unit'],v['system']) for v in barline_candidates}
    if len(series)>=5 and len(distinct_systems)>=4 and all(b>a for a,b in zip(series,series[1:])):
        cadence=reduce(gcd,(b-a for a,b in zip(series,series[1:])))
        if cadence>=2 and sum(b-a==cadence for a,b in zip(series,series[1:]))>=3:
            already={(v['unit'],v['system'],v['word']) for v in proposals}
            additions=[]
            for candidate in barline_candidates:
                if (candidate['unit'],candidate['system'],candidate['word']) in already:continue
                candidate['independent_system_peers']=len(distinct_systems)
                candidate['barline_print_cadence']=cadence
                additions.append(candidate)
            proposals.extend(additions)
            accepted={(v['unit'],v['system'],v['word']) for v in additions}
            pending=[v for v in pending if (v.get('unit'),v.get('system'),v.get('word')) not in accepted]
    return dict(source_book_sha256=sha(book),source_ledger_sha256=sha(ledger),proposals=proposals,pending=pending,rule='Single-word numeric Direction at repeated top-left staff position in >=3 increasing independent systems, or repeated cadence above barlines with matching score ordinals; only omission from textual export, never measure construction')

def apply_printed_measure_number_policy(original,control,plan,book,source,ledger):
    if scan_printed_measure_numbers(book,source,ledger)!=plan:raise ValueError('source printed-number evidence changed')
    oldev,dirs=_score_events(original);ce,cd=_score_events(control);result=copy.deepcopy(original);events,rd=_score_events(result);changes=[];covered=[];pending=list(plan['pending']);removed=[]
    for candidate in plan['proposals']:
        row=candidate['target_note'];key=row['part'],row['measure'],int(row['note_index']);value=candidate['value']
        if key not in events or key not in ce or _role_anchor_signature(events[key])!=_role_anchor_signature(ce[key]):raise ValueError('printed-number anchor music changed')
        def locate(directions):return [d for d in directions if (d['part'],d['measure'],d['start'])==(key[0],key[1],events[key]['start']) and any((w.text or '').strip()==value for w in d['node'].findall('direction-type/words'))]
        native=locate(cd);matches=locate(rd)
        if len(native)!=1:raise ValueError('native printed-number XML identity changed')
        if not matches:covered.append(dict(part=key[0],measure=key[1],value=value));continue
        if len(matches)!=1:pending.append(dict(part=key[0],measure=key[1],value=value,reason='caller_numeric_word_ambiguous'));continue
        direction=matches[0]['node']
        if direction.find('sound') is not None or direction.find('direction-type/metronome') is not None:pending.append(dict(part=key[0],measure=key[1],value=value,reason='tempo_or_other_sounding_direction_preserved'));continue
        if len(direction.findall('direction-type'))!=1 or len(direction.find('direction-type'))!=1:pending.append(dict(part=key[0],measure=key[1],value=value,reason='combined_direction_preserved'));continue
        measure=events[key]['measure'];position=list(measure).index(direction);removed.append((key[:2],position,copy.deepcopy(direction)));measure.remove(direction);rd=[d for d in rd if d['node'] is not direction];changes.append(dict(part=key[0],measure=key[1],printed_value=value,source_unit=candidate['unit'],source_system=candidate['system'],source_word=candidate['word'],peer_systems=candidate['independent_system_peers']))
    neutral=copy.deepcopy(result)
    for (pid,mid),position,node in reversed(removed):neutral.find(f"part[@id='{pid}']/measure[@number='{mid}']").insert(position,node)
    if canonical(neutral)!=canonical(original):raise ValueError('printed-number omission changed unrelated XML')
    final,_=_score_events(result)
    if {k:_music_signature(v) for k,v in oldev.items()}!={k:_music_signature(v) for k,v in final.items()}:raise ValueError('printed-number policy changed music')
    return (result if changes else original),dict(changes=changes,covered=covered,pending=pending,all_measure_structures_notes_lyrics_tempi_and_other_fields_exact=True)




def engraving_candidates(book, source, ledger, maps):
    """Match page-edge stamps and system headers to native note anchors."""
    rows = collections.defaultdict(list)
    with Path(ledger).open(encoding="utf8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if row["repeat_copy"] == "false":
                rows[int(row["unit"]),row["system"],row["chord"]].append(row)
    matches = []
    with zipfile.ZipFile(book) as archive:
        for unit, mapping in sorted(maps.items()):
            name = f"sheet#{unit}/sheet#{unit}.xml"
            if name not in archive.namelist():
                continue
            width,height = mapping["size"]
            physical_page = mapping["pdf_page"]
            systems = E.fromstring(archive.read(name)).findall("page/system")
            headers = []
            for system in systems:
                staff = system.find("part/staff")
                if staff is None:
                    continue
                y = [float(n.get("y")) for n in staff.findall("lines/line/point")]
                if y and max(y)>min(y):
                    headers.append((system.get("id"),float(staff.get("left")),min(y),(max(y)-min(y))/4))
            for system in systems:
                sid = system.get("id")
                nodes = {n.get("id"):n for n in system.find("sig/inters")}
                edges = list(system.find("sig/relations"))
                words, chords = collections.defaultdict(list), collections.defaultdict(list)
                for e in edges:
                    if e.find("containment") is not None:
                        words[e.get("source")].append(e.get("target"))
                    if e.find("chord-sentence") is not None:
                        chords[e.get("target")].append(e.get("source"))
                for parent, ids in words.items():
                    sentence = nodes.get(parent)
                    if (len(ids)!=1 or sentence is None or sentence.get("role")!="Direction"
                            or len(chords[parent])!=1):
                        continue
                    word = nodes.get(ids[0])
                    if word is None or word.tag!="word" or word.find("bounds") is None:
                        continue
                    token = word.get("value","").strip()
                    if not re.fullmatch(r"[1-9][0-9]{0,3}",token):
                        continue
                    x,y,w,h = (int(word.find("bounds").get(key)) for key in ("x","y","w","h"))
                    stamp = int(token)==physical_page and y < .085*height and (x<.13*width or x>.87*width)
                    nearby = [g for g,left,top,space in headers if -space < x-left < 2.2*space
                              and .5*space < top-y-h < 4.5*space and h<4.6*space]
                    mapped = rows[unit,sid,chords[parent][0]]
                    if not mapped or not (stamp or len(nearby)==1):
                        continue
                    row = min(mapped,key=lambda r:int(r["note_index"]))
                    if not stamp and (not row["measure"].isdecimal() or abs(int(row["measure"])-int(token))>3):
                        continue
                    matches.append(dict(kind="page" if stamp else "measure", unit=unit,
                                        system=sid if stamp else nearby[0], token=token, row=row,
                                        position=(x,y,w,h)))
    stamps=[m for m in matches if m["kind"]=="page"]
    bars=[m for m in matches if m["kind"]=="measure"]
    good_page = len({m["unit"] for m in stamps})>=3
    series = sorted(bars,key=lambda m:(m["unit"],m["position"][1]))
    good_bars = (len({(m["unit"],m["system"]) for m in series})>=3
                 and all(int(a["token"])<int(b["token"]) for a,b in zip(series,series[1:])))
    checks={}
    result=[]
    for m in matches:
        if not (good_page if m["kind"]=="page" else good_bars):
            continue
        unit=m["unit"]
        if unit not in checks:
            image=Path(source)/f"unit-{unit:03d}.png"
            checks[unit]=sha(image) if image.is_file() else None
        if checks[unit]==m["row"]["source_sha256"]:
            result.append(m)
    return result


def omit_engraving_labels(root, job):
    report=dict(state="unchanged",changes=[])
    job=Path(job)
    manifest=job/"source-piano-context.json"
    mapping=job/"source-map.json"
    if not manifest.is_file() or not mapping.is_file():
        return root,report
    from source_tempo_context import _score
    context=json.loads(manifest.read_text(encoding="utf8")).get("native_recovery",{})
    props=("native_book","native_note_ledger","native_xml")
    if any(not context.get(prop) or not Path(context[prop]).is_file() for prop in props):
        return root,report
    map_data=json.loads(mapping.read_text(encoding="utf8"))
    maps={int(item["unit"]):item for item in (map_data if isinstance(map_data,list) else map_data["units"])}
    candidates=engraving_candidates(context["native_book"],context.get("source_job",job),
                                     context["native_note_ledger"],maps)
    if not candidates:
        return root,report
    source=_score(Path(context["native_xml"]))
    anchors, native=_score_events(source)
    fixed=copy.deepcopy(root)
    _, directions=_score_events(fixed)

    def standalone(entry,token):
        node=entry["node"]
        words=node.find("direction-type/words")
        return (words is not None and (words.text or "").strip()==token
                and words.get("font-family")=="sans-serif" and words.get("enclosure") is None
                and [e.tag for e in node] in (["direction-type"],["direction-type","staff"])
                and len(node[0])==1)

    def identity(entry):
        n=entry["node"]
        return (entry["part"],entry["measure"],entry["staff"],n.get("placement"),
                sorted(n.find("direction-type/words").attrib.items()))

    for m in candidates:
        row=m["row"]
        key=(row["part"],row["measure"],int(row["note_index"]))
        if key not in anchors:
            continue
        base=[d for d in native if (d["part"],d["measure"],d["start"])==(*key[:2],anchors[key]["start"])
              and standalone(d,m["token"])]
        if len(base)!=1:
            continue
        hits=[d for d in directions if standalone(d,m["token"]) and identity(d)==identity(base[0])]
        if len(hits)!=1:
            continue
        d=hits[0]
        measure=fixed.find(f"part[@id='{d['part']}']/measure[@number='{d['measure']}']")
        measure.remove(d["node"])
        directions.remove(d)
        report["changes"].append(dict(part=d["part"],measure=d["measure"],printed_value=m["token"],
                                      kind=m["kind"],unit=m["unit"],system=m["system"]))
    if report["changes"]:
        report["state"]="omitted"
        return fixed,report
    return root,report
