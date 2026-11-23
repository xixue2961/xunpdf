"""Project source-confirmed long tuplets into MusicXML at stable note addresses."""
from collections import Counter, defaultdict
from fractions import Fraction
from itertools import product
from math import lcm
from pathlib import Path
import copy
import csv
import hashlib
import json
import os
import xml.etree.ElementTree as E

from source_expression_overlay import canonical
from source_lyric_roles import _score_events, _music_signature
from source_tempo_context import _score


def divs(bar,value):
    attrs=bar.find('attributes')
    if attrs is None:
        attrs=E.Element('attributes')
        bar.insert(1 if len(bar) and bar[0].tag=='print' else 0,attrs)
    division=attrs.find('divisions')
    if division is None:
        division=E.Element('divisions')
        at=next((i for i,node in enumerate(attrs) if node.tag not in ('footnote','level')),len(attrs))
        attrs.insert(at,division)
    division.text=str(value)

def put_tuplet(note,actual,normal,divisor,note_type='16th'):
    amount = {'eighth': Fraction(1,2), '16th': Fraction(1,4), '32nd': Fraction(1,8)}[note_type]
    note.find('duration').text=str(amount*normal*divisor/actual)
    stamp=E.Element('time-modification')
    E.SubElement(stamp,'actual-notes').text=str(actual)
    E.SubElement(stamp,'normal-notes').text=str(normal)
    E.SubElement(stamp,'normal-type').text=note_type
    at=next((i for i,node in enumerate(note) if node.tag in ('stem','notehead','notehead-text','staff','beam','notations','lyric','play','listen')),len(note))
    note.insert(at,stamp)

def mark(note,kind,number,side,bracket=True):
    n=note.find('notations')
    if n is None:
        n=E.Element('notations')
        at=next((i for i,c in enumerate(note) if c.tag in ('lyric','play','listen')),len(note))
        note.insert(at,n)
    info=dict(type=kind,number=str(number))
    if kind=='start':
        info.update(bracket='yes' if bracket else 'no',placement=side,**{'show-number':'actual'})
    E.SubElement(n,'tuplet',info)

def put_groups(bars,index,old_div,old_next_div,capacity,groups):
    bar=bars[index]
    if not (not bar.findall('forward') and not bar.findall('.//offset') and len(bar.findall('attributes'))<=1):
        raise ValueError("unsupported anchored tuplet rhythm")
    notes=bar.findall('note')
    indices={index for group in groups for index in group['indices']}
    if not (len(indices)==sum(g['actual'] for g in groups)):
        raise ValueError("unsupported anchored tuplet rhythm")
    scores=[notes[i] for i in sorted(indices)]
    active={n.findtext('voice') for n in scores}
    if not (len(active)==1):
        raise ValueError("unsupported anchored tuplet rhythm")
    voice=active.pop()
    lane=[n for n in notes if n.findtext('voice')==voice]
    if not (all(n.find('rest') is None and n.find('chord') is None and n.findtext('type')=='16th'
               and n.find('dot') is None and n.find('time-modification') is None
               and Fraction(n.findtext('duration'))/old_div==Fraction(1,4) for n in scores)):
        raise ValueError("unsupported anchored tuplet rhythm")
    if not (all(n.find('chord') is None and n.find('grace') is None for n in lane)):
        raise ValueError("unsupported anchored tuplet rhythm")
    old_duration=sum(Fraction(n.findtext('duration'))/old_div for n in lane)
    if not (old_duration>capacity):
        raise ValueError("unsupported anchored tuplet rhythm")
    untouched=old_duration-Fraction(len(scores),4)
    assign=[p for p in product((2,4,8),repeat=len(groups)) if untouched+Fraction(sum(p),4)==capacity]
    if len(assign)!=1:
        raise ValueError('tuplet normal count is not uniquely supported by the bar')
    new=lcm(old_div,*(Fraction(normal,4*g['actual']).denominator for g,normal in zip(groups,assign[0],strict=True)))
    initial=[v for c in bar for v in c.findall('duration')+c.findall('offset')]
    for dt in initial:
        dt.text=str(Fraction(dt.text)*new/old_div)
    divs(bar,new)
    if bars[index+1].find('attributes/divisions') is None:
        divs(bars[index+1],old_next_div)
    reset=[backup.find('duration') for backup in bar.findall('backup')
           if Fraction(backup.findtext('duration'))/new==old_duration]
    if not (len(reset)==1):
        raise ValueError("unsupported anchored tuplet rhythm")
    reset[0].text=str(capacity*new)
    for number,(g,normal) in enumerate(zip(groups,assign[0],strict=True),1):
        if not (g['indices']==list(range(g['indices'][0],g['indices'][-1]+1))):
            raise ValueError("unsupported anchored tuplet rhythm")
        items=[notes[i] for i in g['indices']]
        for n in items:
            put_tuplet(n,g['actual'],normal,new)
        mark(items[0],'start',number,g['side'])
        mark(items[-1],'stop',number,g['side'])
    if not (sum(Fraction(n.findtext('duration'))/new for n in lane)==capacity):
        raise ValueError("unsupported anchored tuplet rhythm")
    return [dict(group=g['group'],actual=g['actual'],normal=normal,note_indices=g['indices'])
            for g,normal in zip(groups,assign[0],strict=True)]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def clocks(part):
    div, meter = 1, None
    result = []
    for bar in part.findall('measure'):
        div=int(bar.findtext('attributes/divisions',str(div)))
        times=bar.findall('attributes/time')
        if times:
            rates={(t.findtext('beats'),t.findtext('beat-type')) for t in times}
            try:
                beats, unit=next(iter(rates))
                meter=Fraction(4*int(beats),int(unit)) if len(rates)==1 else None
            except (ValueError,TypeError,ZeroDivisionError):
                meter=None
        result.append((div,meter))
    return result


def check_cursors(bar, divisions, capacity):
    at=Fraction(0)
    for node in bar:
        if node.tag=='backup':
            reset=Fraction(node.findtext('duration'))/divisions
            if reset!=at:
                return False
            at=Fraction(0)
        elif node.tag=='note' and node.find('grace') is None:
            if node.find('chord') is not None:
                continue
            duration=Fraction(node.findtext('duration'))/divisions
            at += duration
    if at>capacity:
        return False
    # Every intermediate voice also ends before or at the barline.
    return all(Fraction(v.findtext('duration'))/divisions<=capacity for v in bar.findall('backup'))


def put_beamed_groups(bars,index,division,next_division,bar_quarters,groups):
    m, nxt = bars[index:index+2]
    if m.findall('forward') or m.findall('.//offset') or len(m.findall('attributes'))>1:
        return False
    notes=m.findall('note')
    voices={notes[i].findtext('voice','1') for g in groups for i in g['indices']}
    if len(voices)!=1:
        return False
    voice=next(iter(voices))
    lane=[note for note in notes if note.findtext('voice','1')==voice and note.find('chord') is None]
    if any(note.find('grace') is not None for note in lane):
        return False
    old=sum((Fraction(note.findtext('duration'))/division for note in lane),Fraction())
    if old<=bar_quarters:
        return False
    claimed = [i for group in groups for i in group['indices']]
    if len(claimed) != len(set(claimed)):
        return False
    align=[]
    for g in groups:
        indices=g['indices']
        if indices!=list(range(indices[0],indices[-1]+1)):
            return False
        picked=[notes[i] for i in indices]
        old_len={'eighth':Fraction(1,2),'16th':Fraction(1,4),'32nd':Fraction(1,8)}[g['note_type']]
        stems=[n for n in picked if n.find('chord') is None]
        if (len(stems)!=g['actual'] or any(n.find('pitch') is None or n.find('dot') is not None
                or n.find('time-modification') is not None or n.findtext('type')!=g['note_type']
                or Fraction(n.findtext('duration'))/division!=old_len for n in picked)):
            return False
        normal={3:2,5:4,6:4}[g['actual']]
        new_len=old_len*Fraction(normal,g['actual'])
        align.append((g,picked,stems,normal,old_len,new_len))
    total=old-sum((len(stems)*(before-after) for _,_,stems,_,before,after in align),Fraction())
    if total!=bar_quarters:
        return False
    new_div=lcm(division,*(after.denominator for *_,after in align))
    pointer=[n.find('duration') for n in m.findall('backup') if Fraction(n.findtext('duration'))/division==old]
    if len(pointer)>1:
        return False
    for child in m:
        duration=child.find('duration')
        if duration is not None:
            duration.text=str(Fraction(duration.text)*new_div/division)
    if pointer:pointer[0].text=str(total*new_div)
    divs(m,new_div)
    if nxt.find('attributes/divisions') is None:divs(nxt,next_division)
    for sequence,(g,picked,stems,normal,_,_) in enumerate(align,1):
        for note in picked:
            put_tuplet(note,g['actual'],normal,new_div,g['note_type'])
        mark(stems[0],'start',sequence,g['side'],bracket=False)
        mark(stems[-1],'stop',sequence,g['side'],bracket=False)
    return [dict(group=g['group'], actual=g['actual'], normal=normal, note_indices=g['indices'])
            for g, _, _, normal, _, _ in align]


def projection(root, control, ledger, source, findings, *, beamed=False):
    if not findings:
        return root, [], []
    candidates=defaultdict(list)
    with ledger.open(encoding='utf8') as stream:
        mapped=defaultdict(list)
        for row in csv.DictReader(stream,delimiter='	'):
            if row['repeat_copy']=='false':
                mapped[(int(row['unit']),row['system'],row['chord'])].append(row)
    targets={n.get('id') for n in root.findall('part-list/score-part') if n.findtext('part-name','').lower()=='piano'}
    if not targets and len(root.findall('part'))==1:
        targets={root.find('part').get('id')}
    src_events,_=_score_events(control)
    out_events,_=_score_events(root)
    pending=[]
    for hit in findings:
        identity=(hit['unit'],hit['system'])
        if sha(source/f"unit-{identity[0]:03d}.png")!=hit['source_sha256']:
            raise ValueError('连音原图像素已变化')
        refs=[mapped[(*identity,chord)] for chord in hit['chords']]
        if any(not rows if beamed else len(rows)!=1 for rows in refs):
            pending.append(dict(group=hit['group'],reason='tuplet_source_head_address_not_unique'))
            continue
        rows = [row for items in refs for row in items]
        scope={(row['part'],row['measure']) for row in rows}
        if len(scope)!=1 or any(row['source_sha256']!=hit['source_sha256'] for row in rows):
            pending.append(dict(group=hit['group'],reason='tuplet_source_context_conflict'))
            continue
        pid,bar=scope.pop()
        indices=sorted(int(row['note_index']) for row in rows)
        anchors=[(pid,bar,i) for i in indices]
        if (pid not in targets or any(key not in src_events or key not in out_events or
            _music_signature(src_events[key])!=_music_signature(out_events[key]) or
            src_events[key]['node'].get('default-x')!=out_events[key]['node'].get('default-x') for key in anchors)):
            pending.append(dict(group=hit['group'],reason='tuplet_export_anchor_changed'))
            continue
        side = hit['digit_placement'] if beamed else hit['witnesses'][0][0]['side']
        candidates[(pid,bar)].append(dict(group=hit['group'],indices=indices,actual=hit['count'],
                                          side=side,note_type=hit.get('note_type')))

    result=copy.deepcopy(root)
    changes=[]
    for part in result.findall('part'):
        pid=part.get('id')
        bars=part.findall('measure')
        numbers=Counter(bar.get('number') for bar in bars)
        original_clock=clocks(part)
        for idx,bar in enumerate(bars):
            mid=bar.get('number')
            groups=sorted(candidates.get((pid,mid),[]),key=lambda g:g['indices'])
            if not groups:
                continue
            div,capacity=original_clock[idx]
            if numbers[mid]!=1 or idx+1==len(bars) or capacity is None:
                pending.append(dict(part=pid,measure=mid,reason='tuplet_bar_clock_not_unique'))
                continue
            trial=[copy.deepcopy(bar),copy.deepcopy(bars[idx+1])]
            try:
                if canonical(trial[0])!=canonical(bar):
                    raise ValueError('tuplet bar changed while staging')
                put = put_beamed_groups if beamed else put_groups
                change=put(trial,0,div,original_clock[idx+1][0],capacity,groups)
                if not change or not check_cursors(trial[0],int(trial[0].findtext('attributes/divisions')),capacity):
                    raise ValueError('tuplet candidate has unsafe MusicXML bookmarks')
            except (AssertionError,ValueError,TypeError,ZeroDivisionError):
                pending.append(dict(part=pid,measure=mid,reason='tuplet_reflow_did_not_prove_bar_capacity'))
                continue
            for offset,after in enumerate(trial):
                before=bars[idx+offset]
                if canonical(before)!=canonical(after):
                    position=list(part).index(before)
                    part.remove(before)
                    part.insert(position,after)
                    bars[idx+offset]=after
            changes.append(dict(part=pid,measure=mid,physical_measure=idx+1,
                                meter_quarters=str(capacity),tuplet_groups=change,
                                source_style='beamed' if beamed else 'bracketed'))
    return result if changes else root, changes, pending


def export_head_ledger(book, source, expected, job, settings, env, cancel, runner, emit):
    from runtime_resources import check_memory

    app=Path(__file__).resolve().parent
    engine=Path(settings['engine_root'])
    prefix=Path(settings.get('engine_classpath_prefix','engine-classes'))
    if not prefix.is_absolute():
        prefix=app/prefix
    paths=(app/'rhythm-core/runtime',app/'piano-core/runtime',prefix,engine/'Audiveris-Bandscore/lib/*')
    java=engine/settings['java_relative']/'bin/java.exe'
    folder=job/'source-long-tuplets-ledger'
    check_memory(job,'source-long-tuplets-ledger')
    args=[java,'-Xms64m','-Xmx768m','-XX:ActiveProcessorCount=2','-Djava.awt.headless=true',
          '--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED',
          '-cp',os.pathsep.join(map(str,paths)),'org.audiveris.omr.score.SourceNativeLedger404',
          book,expected,source,folder]
    runner(args,job/'source-long-tuplets-ledger.log',env,cancel,timeout=180,emit=emit)
    if sha(book)!=expected:
        raise ValueError('原生谱在音符坐标导出期间发生变化')
    return folder/'result.mxl',folder/'head-ledger.tsv'


def refine_long_tuplets(root, job, settings, device, env, cancel, runner, emit=print, source_job=None):
    job=Path(job)
    report=dict(state='not_assessed',changes=[],pending=[])
    result=root
    try:
        from runtime_resources import check_memory
        info=read(job/'source-native-notation.json')
        book=Path(info['accepted_native_book'])
        control=Path(info['accepted_native_xml'])
        ledger=control.with_name('head-ledger.tsv')
        expected=info.get('accepted_native_sha256')
        if not expected and info['source_book']==info['accepted_native_book']:
            expected=info['source_book_sha256']
        if sha(book)!=expected:
            raise ValueError('未取得已验证的原生谱')
        source=Path(source_job) if source_job is not None else job
        evidence=job/'source-long-tuplets-pixels.json'
        engine=Path(settings['engine_root'])
        executable=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe'
        script=Path(__file__).resolve().parent/'rhythm-core/source_long_tuplet_pixels.py'
        model=engine/'cache/paddlex/official_models/en_PP-OCRv5_mobile_rec'
        check_memory(job,'source-long-tuplets')
        runner([executable,script,'--book',book,'--source',source,'--model',model,'--output',evidence],
               job/'source-long-tuplets.log',env,cancel,timeout=180,emit=emit)
        scan=read(evidence)
        if scan['source_book_sha256']!=expected:
            raise ValueError('连音识别使用了不同的原生谱')
        beamed = scan['beamed_findings']
        if scan['findings'] or beamed:
            if not ledger.is_file():
                control,ledger=export_head_ledger(book,source,expected,job,settings,env,cancel,runner,emit)
                report['ledger']='generated_from_verified_native_book'
            native = _score(control)
            bracketed_result, changes, pending = projection(root,native,ledger,source,scan['findings'])
            result, beam_changes, beam_pending = projection(bracketed_result,native,ledger,source,beamed,beamed=True)
            changes.extend(beam_changes)
            pending.extend(beam_pending)
        else:
            changes,pending=[],[]
        pending=scan['pending']+pending
        report.update(changes=changes,pending=pending,
                      state='partially_restored' if changes and pending else 'restored' if changes else
                            'needs_review' if pending else 'no_supported_groups')
        if changes:
            emit('原图连音复核：恢复'+str(sum(len(c['tuplet_groups']) for c in changes))+'组连音')
    except InterruptedError:
        raise
    except Exception as exc:
        report.update(state='unresolved',reason=str(exc))
        emit('跨谱表长连音待核对，保留现有导出')
    (job/'source-long-tuplets-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return result,report
