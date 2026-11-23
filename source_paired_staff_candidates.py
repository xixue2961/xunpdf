from fractions import Fraction as F


def paired_due(bar,divisions,capacity):
    notes=bar.findall('note')
    if capacity is None or not notes or any(n.find('grace') is not None or not n.findtext('duration') for n in notes):
        return False
    duration={staff:sum(F(n.findtext('duration'))/divisions for n in notes
                        if n.findtext('staff','1')==staff and n.find('chord') is None)
              for staff in ('1','2')}
    return duration['2']==capacity and duration['1']>capacity




def pause_due(bar,divisions,capacity):
    if capacity!=4 or bar.find('.//offset') is not None or len(bar.findall('backup'))!=1:
        return False
    voices={'1':[],'2':[]}
    for note in bar.findall('note'):
        staff=note.findtext('staff')
        if (staff not in voices or note.find('chord') is not None
            or note.find('grace') is not None or note.find('time-modification') is not None
            or not note.findtext('duration')):
            return False
        voices[staff].append(note)
    if any(len({n.findtext('voice') for n in lane})!=1 for lane in voices.values()):
        return False
    durations={s:[F(n.findtext('duration'))/divisions for n in lane] for s,lane in voices.items()}
    if durations!={'1':[2,1]+[F(1,4)]*5,'2':[2]+[F(1,2)]*3}:
        return False
    for staff,count in (('1',2),('2',1)):
        lane=voices[staff]
        if (any(n.find('rest') is None for n in lane[:count])
            or any(n.find('pitch') is None for n in lane[count:])):
            return False
    dirs=bar.findall('direction')
    loud=[d for d in dirs if d.findtext('staff')=='1' and d.find('direction-type/dynamics/f') is not None]
    digit=[d for d in dirs if d.findtext('staff')=='2' and d.findtext('direction-type/words')=='3'
           and d.find('sound') is None]
    return len(dirs)==2 and len(loud)==len(digit)==1



def due(bar,divisions,capacity):
    return paired_due(bar,divisions,capacity) or pause_due(bar,divisions,capacity)
