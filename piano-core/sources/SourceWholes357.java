package org.audiveris.omr.score;
import java.nio.file.*;import java.util.*;import java.awt.*;import java.security.*;
import org.audiveris.omr.sheet.Book;import org.audiveris.omr.sig.inter.*;import org.audiveris.omr.glyph.Shape;
public class SourceWholes357 {
 static void require(boolean b,String s){if(!b)throw new IllegalArgumentException(s);}
 static String sha(Path p)throws Exception{return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(p)));}
 public static void main(String[] a)throws Exception{
 ((ch.qos.logback.classic.Logger)org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME)).setLevel(ch.qos.logback.classic.Level.WARN);
 Path input=Path.of(a[0]),source=Path.of(a[1]),out=Path.of(a[3]);String original=sha(input);Files.createDirectories(out);

 var book=Book.loadBook(input);require(book!=null,"load failed");SourceStructure335.source=source;
 var groups=new LinkedHashMap<String,java.util.List<String[]>>();for(String line:Files.readAllLines(Path.of(a[2]))){var f=line.split("\t");require(f.length==12,"plan fields");groups.computeIfAbsent(String.join("/",Arrays.copyOf(f,4)),k->new ArrayList<>()).add(f);}
 var touched=new LinkedHashSet<org.audiveris.omr.sheet.rhythm.MeasureStack>();int created=0;
 for(var rows:groups.values()){
  var f=rows.getFirst();int unit=Integer.parseInt(f[0]);require(sha(source.resolve(String.format("unit-%03d.png",unit))).equals(f[11]),"source pixels changed");
  var sheet=book.getStub(unit).getSheet();var sys=sheet.getSystems().stream().filter(s->s.getId()==Integer.parseInt(f[1])).findFirst().orElseThrow();var staff=sys.getStaves().stream().filter(s->s.getId()==Integer.parseInt(f[3])).findFirst().orElseThrow();var stack=sys.getStacks().stream().filter(s->s.getIdValue()==Integer.parseInt(f[2])).findFirst().orElseThrow();var measure=stack.getMeasureAt(staff.getPart());var sig=sys.getSig();
  require(staff.getPart().getStaves().size()==2,"not grand staff");require(stack.getExpectedDuration()!=null&&stack.getExpectedDuration().equals(org.audiveris.omr.math.Rational.ONE),"whole note needs independently validated 4/4 context");
  var existing=measure.getHeadChords().stream().filter(c->c.getStaff()==staff).toList();
  boolean memberMode=existing.size()==1&&existing.getFirst().getNotes().stream().allMatch(n->n.getShape()==Shape.WHOLE_NOTE);
  boolean sustainedMode=!existing.isEmpty()&&!memberMode;
  if(sustainedMode)require(existing.stream().allMatch(c->c.getVoice()!=null&&c.getNotes().stream().noneMatch(n->n.getShape()==Shape.WHOLE_NOTE)),"ambiguous sustained layer");
  if(!sustainedMode)require(measure.getRestChords().stream().noneMatch(c->c.getStaff()==staff),"existing rest requires separate decision");
  var heads=new ArrayList<HeadInter>();double grade=1;
  for(var row:rows){
   var box=new Rectangle(Integer.parseInt(row[4]),Integer.parseInt(row[5]),Integer.parseInt(row[6]),Integer.parseInt(row[7]));require(box.x>=stack.getLeft()&&box.getMaxX()<=stack.getRight(),"outside source bar");
   var overlaps=sig.vertexSet().stream().filter(n->n instanceof HeadInter&&n.getBounds().intersects(box)).toList();
   if(!overlaps.isEmpty()){require(overlaps.size()==1&&memberMode&&existing.getFirst().getNotes().contains(overlaps.getFirst())&&overlaps.getFirst().getShape()==Shape.WHOLE_NOTE,"unexpected overlap");continue;}
   HeadInter donor=null;for(var ds:book.getStub(Integer.parseInt(row[9])).getSheet().getSystems())for(var n:ds.getSig().vertexSet())if(n.getId()==Integer.parseInt(row[10])&&n instanceof HeadInter h)donor=h;
   require(donor!=null&&donor.getShape()==Shape.WHOLE_NOTE,"wrong donor");var clef=measure.getClefBefore(new Point((int)box.getCenterX(),(int)box.getCenterY()),staff);require(clef!=null,"missing source clef");
   double pitch=staff.pitchPositionOf(new java.awt.geom.Point2D.Double(box.getCenterX(),box.getCenterY()));require(Math.abs(pitch-Double.parseDouble(row[8]))<.25,"staff pitch disagreement");grade=Math.min(grade,donor.getGrade());heads.add(new HeadInter(box,Shape.WHOLE_NOTE,donor.getGrade(),staff,Math.rint(pitch)));
  }
  require(!heads.isEmpty(),"no missing heads");var chord=memberMode?existing.getFirst():new HeadChordInter(grade);
  if(!memberMode){chord.setStaff(staff);chord.setMeasure(measure);sig.addVertex(chord);measure.addInter(chord);}
  for(var head:heads){sig.addVertex(head);chord.addMember(head);created++;}touched.add(stack);
  System.out.println("WHOLE_LANE_CREATED "+String.join("/",Arrays.copyOf(f,4))+" heads="+heads.size());
 }
 for(var stack:touched)new org.audiveris.omr.sheet.rhythm.PageRhythm(stack.getSystem().getPage()).reprocessStack(stack);
 // Existing logical parts are preserved.
 for(var stub:book.getStubs())stub.setModified(true);book.setModified(true);book.store(out.resolve("refined.omr"),false);book=Book.loadBook(out.resolve("refined.omr"));require(book!=null,"reload failed");
 // Existing logical parts are preserved.
 book.setExportPathSansExt(out.resolve("result"));require(book.export(book.getValidStubs(),book.getScores()),"export failed");SourceStructure335.writeLedgers(out);require(original.equals(sha(input)),"source book changed");System.out.println("WHOLE346_EXPORTED heads="+created);System.exit(0);
 }
}
