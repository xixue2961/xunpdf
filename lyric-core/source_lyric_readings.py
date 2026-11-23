"""Keep source geometry as part of OCR identity across original and packed views."""
def reference_bounds(letter):
 x,y,w,h=letter['bounds'];pad=max(3,round(.08*letter['font_height']))
 return [max(0,x-pad),max(0,y-pad),w+2*pad,h+2*pad]

def reference_id(letter):
 return 'local-ocr-'+'-'.join(str(v) for v in letter['bounds'])

def matches_reference(reference,letter):
 if any(reference[k]!=letter[k] for k in ('unit','system','staff','text','confidence','baseline','font_height')):return False
 if reference['bounds']!=reference_bounds(letter):return False
 legacy='local-ocr-'+str(letter['bounds'][0])+'-'+str(letter['bounds'][1])
 return reference['item'] in (reference_id(letter),legacy)

def resolve_contained_packed_fragments(characters):
 """A packed fragment cannot be a second occurrence inside the same original glyph.
 Resolve only equal labels, exact containment, and an at-least-as-confident original
 reading. Keep genuinely different or uncertain readings for the existing guards.
 """
 kept=[];decisions=[]
 for small in characters:
  owners=[]
  if small.get('reading_view')=='spacing_normalized_original_columns':
   x,y,w,h=small['bounds']
   for full in characters:
    if not full.get('source_ocr_file','').endswith('/original.tsv') or full.get('reading_view'):continue
    if any(full[k]!=small[k] for k in ('unit','system','staff','text')):continue
    if full['confidence']<.95 or full['confidence']<small['confidence'] or full.get('punctuation','')!=small.get('punctuation',''):continue
    a,b,c,d=full['bounds']
    if [a,b,c,d]!=[x,y,w,h] and a<=x and b<=y and x+w<=a+c and y+h<=b+d:owners.append(full)
  if len(owners)==1:
   decisions.append(dict(reason='packed_partial_glyph_inside_original_complete_read',text=small['text'],packed_fragment=small,retained_original=owners[0]))
  else:kept.append(small)
 return kept,decisions
