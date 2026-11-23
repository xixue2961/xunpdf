package org.audiveris.omr.score;
import org.audiveris.omr.sheet.Book;import org.audiveris.omr.sheet.Staff;import org.audiveris.omr.sig.inter.*;import org.audiveris.proxymusic.ScorePartwise;import org.audiveris.proxymusic.ScorePart;import org.audiveris.proxymusic.Note;
import java.nio.file.*;import java.util.*;import java.awt.Rectangle;
/** Reconstruct source-supported native meter context before exporting any repaired rhythm. */
public final class SourceStructure335 {
 static Path source;static final Map<Integer,String> imageHashes=new HashMap<>();static final List<String> ledger=new ArrayList<>();static final List<String> allNotes=new ArrayList<>();
 static String sha(Path p)throws Exception{return HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(p)));}
 static void require(boolean b,String message){if(!b)throw new IllegalArgumentException(message);}
 record TimeProof(int unit,int system,int stack,Staff staff,Rectangle bounds,int num,int den){}
 record BarProof(int unit,int system,int stack,Staff staff,Rectangle bounds){}
 public static void mark(AbstractNoteInter note,AbstractChordInter chord,ScorePartwise.Part part,ScorePartwise.Part.Measure measure,boolean repeated){
  try{
   var staff=note.getStaff()!=null?note.getStaff():chord.getStaff();require(staff!=null,"note has no physical staff");var system=staff.getSystem();int unit=system.getSheet().getStub().getNumber();
   if(!imageHashes.containsKey(unit))imageHashes.put(unit,sha(source.resolve(String.format("unit-%03d.png",unit))));var b=note.getBounds();if(b==null){require(!(note instanceof HeadInter),"pitched head has no bounds");b=new Rectangle();}
   long index=measure.getNoteOrBackupOrForward().stream().filter(v->v instanceof Note).count()-1;
   String row=String.join("\t",""+unit,""+system.getId(),""+staff.getPart().getId(),""+staff.getId(),""+chord.getId(),""+note.getId(),""+note.getShape(),imageHashes.get(unit),((ScorePart)part.getId()).getId(),measure.getNumber(),""+index,""+(staff.getIndexInPart()+1),""+repeated,""+b.x,""+b.y,""+b.width,""+b.height);
   if(note instanceof HeadInter)ledger.add(row);
   allNotes.add(row+"\t"+note.getClass().getSimpleName()+"\t"+note.isImplicit()+"\t"+(note.getGlyph()==null?"none":""+note.getGlyph().getId()));
  }catch(Exception ex){throw new IllegalStateException("source note ledger failed",ex);}
 }
 public static void main(String[] args){try{run(args);System.exit(0);}catch(Throwable t){t.printStackTrace();System.exit(1);}}
 static void run(String[] a)throws Exception {
  ((ch.qos.logback.classic.Logger)org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME)).setLevel(ch.qos.logback.classic.Level.WARN);
  Path input=Path.of(a[0]),evidence=Path.of(a[3]),out=Path.of(a[4]);source=Path.of(a[2]);require(sha(input).equals(a[1]),"native archive changed");require(Set.of("control","apply").contains(a[5]),"invalid mode");String hint=a[6];
  if(!hint.isBlank()){var field=PartCollation.class.getDeclaredField("constants");field.setAccessible(true);var constants=field.get(null);var hf=constants.getClass().getDeclaredField("partsHint");hf.setAccessible(true);((org.audiveris.omr.constant.Constant.String)hf.get(constants)).setStringValue(hint);}
  var book=Book.loadBook(input);require(book!=null,"load failed");sourceNodes(book,out.resolve("before-nodes.tsv"));assigned(book,out.resolve("before-assigned-heads.tsv"));var times=new ArrayList<TimeProof>();var bars=new ArrayList<BarProof>();var seen=new HashSet<String>();
  for(var line:Files.readAllLines(evidence.resolve("time-plan.tsv"))){
   if(line.isBlank())continue;var f=line.split("\t");require(f.length==11,"time plan format");int unit=Integer.parseInt(f[0]),sid=Integer.parseInt(f[1]),mid=Integer.parseInt(f[2]);var sys=book.getStub(unit).getSheet().getSystems().stream().filter(s->s.getId()==sid).findFirst().orElseThrow();var stack=sys.getStacks().stream().filter(s->s.getIdValue()==mid).findFirst().orElseThrow();var staff=sys.getStaves().stream().filter(s->s.getId()==Integer.parseInt(f[3])).findFirst().orElseThrow();var measure=stack.getMeasureAt(staff.getPart());require(seen.add("time:"+unit+":"+sid+":"+mid+":"+staff.getId()),"duplicate time target");require(measure.getTimeSignature(staff.getIndexInPart())==null,"target already has time signature");require(sha(source.resolve(String.format("unit-%03d.png",unit))).equals(f[10]),"source time image changed");var bounds=new Rectangle(Integer.parseInt(f[6]),Integer.parseInt(f[7]),Integer.parseInt(f[8]),Integer.parseInt(f[9]));int num=Integer.parseInt(f[4]),den=Integer.parseInt(f[5]);require(num>0&&Set.of(2,4,8).contains(den)&&bounds.width>0&&bounds.height>0,"unsupported source time");times.add(new TimeProof(unit,sid,mid,staff,bounds,num,den));
  }
  for(var line:Files.readAllLines(evidence.resolve("barline-plan.tsv"))){
   if(line.isBlank())continue;var f=line.split("\t");require(f.length==9,"barline plan format");int unit=Integer.parseInt(f[0]),sid=Integer.parseInt(f[1]),mid=Integer.parseInt(f[2]);var sys=book.getStub(unit).getSheet().getSystems().stream().filter(s->s.getId()==sid).findFirst().orElseThrow();var stack=sys.getStacks().stream().filter(s->s.getIdValue()==mid).findFirst().orElseThrow();var staff=sys.getStaves().stream().filter(s->s.getId()==Integer.parseInt(f[3])).findFirst().orElseThrow();var bar=stack.getMeasureAt(staff.getPart()).getRightPartBarline();require(bar!=null&&bar.getStaffBarline(staff.getPart(),staff)==null,"barline no longer missing");require(seen.add("bar:"+unit+":"+sid+":"+mid+":"+staff.getId()),"duplicate barline target");require(sha(source.resolve(String.format("unit-%03d.png",unit))).equals(f[8]),"source barline image changed");bars.add(new BarProof(unit,sid,mid,staff,new Rectangle(Integer.parseInt(f[4]),Integer.parseInt(f[5]),Integer.parseInt(f[6]),Integer.parseInt(f[7]))));
  }
  if(a[5].equals("apply")){
   for(var p:bars){var sys=p.staff().getSystem();var stack=sys.getStacks().stream().filter(s->s.getIdValue()==p.stack()).findFirst().orElseThrow();var measure=stack.getMeasureAt(p.staff().getPart());Double grade=sys.getSig().vertexSet().stream().filter(v->v instanceof BarlineInter&&v.getGrade()!=null&&Math.abs(v.getCenter().x-p.bounds().getCenterX())<p.staff().getMeanInterline()).mapToDouble(v->v.getGrade()).average().orElseThrow();var raw=new BarlineInter(null,org.audiveris.omr.glyph.Shape.THIN_BARLINE,grade,new java.awt.geom.Line2D.Double(p.bounds().getCenterX(),p.bounds().y+.5,p.bounds().getCenterX(),p.bounds().getMaxY()-.5),(double)p.bounds().width);raw.setStaff(p.staff());raw.setBounds(p.bounds());sys.getSig().addVertex(raw);var b=new StaffBarlineInter(org.audiveris.omr.glyph.Shape.THIN_BARLINE,grade);b.setStaff(p.staff());sys.getSig().addVertex(b);b.addMember(raw);measure.getRightPartBarline().addStaffBarline(b);measure.afterReload();var next=measure.getSibling(org.audiveris.omr.util.HorizontalSide.RIGHT);if(next!=null)next.afterReload();}
   for(var p:times){var sys=p.staff().getSystem();var stack=sys.getStacks().stream().filter(s->s.getIdValue()==p.stack()).findFirst().orElseThrow();var time=new TimeCustomInter(p.num(),p.den(),null);time.setStaff(p.staff());time.setBounds(p.bounds());sys.getSig().addVertex(time);stack.addInter(time);}
   SourceTimeConflicts335.repair(book,evidence.resolve("time-note-conflicts-v2.tsv"),out.resolve("time-note-conflicts.tsv"));
   for(var stub:book.getStubs())for(var page:stub.getSheet().getPages()){
    for(var sys:page.getSystems())for(var stack:sys.getStacks()){for(var measure:stack.getMeasures())measure.resetRhythm();stack.resetRhythm();}
    new org.audiveris.omr.sheet.rhythm.PageRhythm(page).process();
   }
  }
  for(var score:book.getScores()){score.setLogicalsLocked(false);score.setLogicalParts(null);new ScoreReduction(score).reduce(book.getValidStubs());}
  assigned(book,out.resolve("after-assigned-heads.tsv"));sourceNodes(book,out.resolve("after-nodes.tsv"));
  if(a[5].equals("apply")){for(var stub:book.getStubs())stub.setModified(true);book.setModified(true);book.store(out.resolve("refined.omr"),false);require(Files.exists(out.resolve("refined.omr")),"refined archive missing");book=Book.loadBook(out.resolve("refined.omr"));require(book!=null,"refined archive cannot reload");for(var score:book.getScores()){score.setLogicalsLocked(false);score.setLogicalParts(null);new ScoreReduction(score).reduce(book.getValidStubs());}assigned(book,out.resolve("after-reload-assigned-heads.tsv"));sourceNodes(book,out.resolve("after-reload-nodes.tsv"));}
  book.setExportPathSansExt(out.resolve("result"));require(book.export(book.getValidStubs(),book.getScores()),"export failed");writeLedgers(out);require(sha(input).equals(a[1]),"input native archive changed");System.out.println("SOURCE_STRUCTURE_OK "+a[5]+" times="+times.size()+" bars="+bars.size()+" exported_heads="+ledger.size());
 }
 public static void writeLedgers(Path out)throws Exception{var rows=new ArrayList<String>();rows.add("unit\tsystem\tnative_part\tstaff\tchord\thead\tshape\tsource_sha256\tpart\tmeasure\tnote_index\tstaff_number\trepeat_copy\tx\ty\tw\th");rows.addAll(ledger);Files.write(out.resolve("head-ledger.tsv"),rows);var allRows=new ArrayList<String>();allRows.add(rows.getFirst()+"\tnative_note_class\timplicit\tglyph");allRows.addAll(allNotes);Files.write(out.resolve("note-ledger.tsv"),allRows);}
 static void sourceNodes(Book book,Path path)throws Exception{
  var rows=new ArrayList<String>();rows.add("unit\tsystem\tclass\tid\tstaff\tshape\tx\ty\tw\th\tvalue");
  for(var stub:book.getStubs())for(var sys:stub.getSheet().getSystems())for(var node:sys.getSig().vertexSet()){
   var b=node.getBounds();if(b==null)continue;String value="";if(node instanceof WordInter word)value=word.getValue();else if(node instanceof SentenceInter sentence)value=sentence.getValue();rows.add(String.join("\t",""+stub.getNumber(),""+sys.getId(),node.getClass().getSimpleName(),""+node.getId(),node.getStaff()==null?"none":""+node.getStaff().getId(),""+node.getShape(),""+b.x,""+b.y,""+b.width,""+b.height,value.replace('\t',' ').replace('\n',' ')));
  }
  rows.subList(1,rows.size()).sort(String::compareTo);Files.write(path,rows);
 }
 static void assigned(Book book,Path path)throws Exception{
  var rows=new ArrayList<String>();rows.add("unit\tsystem\tnative_part\tmeasure\tstaff\tvoice\tchord\thead\toffset\tduration");
  for(var stub:book.getStubs())for(var sys:stub.getSheet().getSystems())for(var part:sys.getParts())for(var measure:part.getMeasures())for(var voice:measure.getVoices())for(var chord:voice.getChords())for(var note:chord.getNotes())if(note instanceof HeadInter)rows.add(String.join("\t",""+stub.getNumber(),""+sys.getId(),""+part.getId(),""+measure.getStack().getIdValue(),""+note.getStaff().getId(),""+voice.getId(),""+chord.getId(),""+note.getId(),""+chord.getTimeOffset(),""+chord.getDuration()));
  Files.write(path,rows);
 }
}
