"""Conservative shared-beam encoding for MusicXML chord interoperability."""
import copy,xml.etree.ElementTree as E

def normalize_chord_beams(root):
 before=copy.deepcopy(root);changes=[];unresolved=[]
 for part in root.findall('part'):
  for measure in part.findall('measure'):
   groups=[];group=[]
   for e in measure:
    if e.tag in ('backup','forward'):group=[]
    if e.tag!='note':continue
    if e.find('chord') is None:group=[e];groups.append(group)
    elif group:group.append(e)
   for index,group in enumerate(groups):
    if len(group)<2:continue
    beams=[n.findall('beam') for n in group];sets={tuple((e.text,tuple(sorted(e.attrib.items()))) for e in seq) for seq in beams if seq}
    if not sets:continue
    context={'part':part.get('id'),'measure':measure.get('number'),'group':index}
    identities={(n.findtext('voice'),n.findtext('duration'),n.findtext('type')) for n in group}
    if len(sets)>1 or len(identities)>1 or any(n.find('rest') is not None for n in group):
     unresolved.append(dict(context,reason='conflicting beam or chord-duration/voice definitions preserved'));continue
    lead=group[0];canonical=[copy.deepcopy(e) for e in next(seq for seq in beams if seq)]
    original_count=sum(map(len,beams))
    if original_count==len(canonical) and beams[0]:continue
    for n in group:
     for e in list(n.findall('beam')):n.remove(e)
    insert_at=next((i for i,e in enumerate(lead) if e.tag in ('notations','lyric','play','listen')),len(lead))
    for e in canonical:lead.insert(insert_at,e);insert_at+=1
    changes.append(dict(context,beam_count_before=original_count,beam_count_after=len(canonical),notes=len(group)))
 after=copy.deepcopy(root)
 for tree in (before,after):
  for n in tree.findall('part/measure/note'):
   for beam in list(n.findall('beam')):n.remove(beam)
 assert E.tostring(before)==E.tostring(after),'Unexpected change outside beam placement'
 return {'rule':'shared-chord-beams-v1','changes':changes,'unresolved':unresolved,'all_nonbeam_content_preserved':True}
