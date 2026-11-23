package org.audiveris.omr.score;
import java.nio.file.*;import java.util.*;import java.security.*;import java.awt.Rectangle;
import org.audiveris.omr.sheet.Book;
import org.audiveris.omr.sig.inter.*;
public class SourceTimesEarly383 {
 static void require(boolean ok,String message){if(!ok)throw new IllegalArgumentException(message);}
 static String sha(Path p)throws Exception{return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(p)));}
 static Set<String> heads(Book book){var result=new TreeSet<String>();for(var stub:book.getStubs())for(var sys:stub.getSheet().getSystems())for(var inter:sys.getSig().vertexSet())if(inter instanceof HeadInter)result.add(stub.getNumber()+"/"+sys.getId()+"/"+inter.getId());return result;}
 public static void main(String[] a)throws Exception{
  ((ch.qos.logback.classic.Logger)org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME)).setLevel(ch.qos.logback.classic.Level.WARN);
  Path input=Path.of(a[0]),source=Path.of(a[1]),plan=Path.of(a[2]),out=Path.of(a[3]);String hash=sha(input);require(hash.equals(a[4]),"source native hash mismatch");Files.createDirectories(out);SourceStructure335.source=source;
  var book=Book.loadBook(input);require(book!=null,"native load failed");var before=heads(book);var touched=new LinkedHashSet<org.audiveris.omr.score.Page>();var keys=new HashSet<String>();int added=0,retained=0,restoredBars=0;
  for(String line:Files.readAllLines(Path.of(a[5]))){
   var f=line.split("\\t");require(f.length==9,"source barline plan fields");int unit=Integer.parseInt(f[0]);Path image=source.resolve(String.format("unit-%03d.png",unit));require(sha(image).equals(f[8]),"barline source image changed");var sys=book.getStub(unit).getSheet().getSystems().stream().filter(v->v.getId()==Integer.parseInt(f[1])).findFirst().orElseThrow();var staff=sys.getStaves().stream().filter(v->v.getId()==Integer.parseInt(f[3])).findFirst().orElseThrow();var stack=sys.getStacks().stream().filter(v->v.getIdValue()==Integer.parseInt(f[2])).findFirst().orElseThrow();var measure=stack.getMeasureAt(staff.getPart());var bar=measure.getRightPartBarline();require(bar!=null&&bar.getStaffBarline(staff.getPart(),staff)==null,"barline association is not missing");var box=new Rectangle(Integer.parseInt(f[4]),Integer.parseInt(f[5]),Integer.parseInt(f[6]),Integer.parseInt(f[7]));double il=staff.getMeanInterline();require(Math.abs(box.getCenterX()-stack.getRight())<.5*il&&box.width<=.5*il&&Math.abs(box.height-4*il)<il,"barline geometry mismatch");var im=javax.imageio.ImageIO.read(image.toFile());require(im!=null,"barline source unavailable");
   for(int x=box.x;x<box.x+box.width;x++){int dark=0;for(int y=box.y;y<box.y+box.height;y++){int rgb=im.getRGB(x,y);int gray=(((rgb>>16)&255)*299+((rgb>>8)&255)*587+(rgb&255)*114)/1000;if(gray<160)dark++;}require(dark>=.95*box.height,"barline has no continuous source ink");}
   var restored=new StaffBarlineInter(org.audiveris.omr.glyph.Shape.THIN_BARLINE,0.8);restored.setStaff(staff);restored.setBounds(box);sys.getSig().addVertex(restored);bar.addStaffBarline(restored);measure.afterReload();var next=measure.getSibling(org.audiveris.omr.util.HorizontalSide.RIGHT);if(next!=null)next.afterReload();touched.add(sys.getPage());restoredBars++;System.out.println("SOURCE_BARLINE_RESTORED "+String.join("/",Arrays.copyOf(f,4)));
  }
  var timeRows = Files.readAllLines(plan);
  var staffVotes = new HashMap<String, Set<Integer>>();
  for (String line : timeRows) {
   var fields = line.split("\\t");
   require(fields.length == 11, "time plan fields");
   String group = String.join("/", fields[0], fields[1], fields[2], fields[4], fields[5]);
   staffVotes.computeIfAbsent(group, ignored -> new HashSet<>()).add(Integer.parseInt(fields[3]));
  }
  record MeterDisagreement(String group, String details) {}
  var meterDisagreements = new ArrayList<MeterDisagreement>();
  var blockedGroups = new HashSet<String>();
  record MeterEdit(String group, Runnable apply, boolean addition) {}
  var meterEdits = new ArrayList<MeterEdit>();
  for(String line:timeRows){
   String[] f=line.split("\\t");require(f.length==11,"time plan fields");String key=String.join("/",Arrays.copyOf(f,4));require(keys.add(key),"duplicate source time target");int unit=Integer.parseInt(f[0]);require(sha(source.resolve(String.format("unit-%03d.png",unit))).equals(f[10]),"source image changed");
   var sys=book.getStub(unit).getSheet().getSystems().stream().filter(s->s.getId()==Integer.parseInt(f[1])).findFirst().orElseThrow();var staff=sys.getStaves().stream().filter(s->s.getId()==Integer.parseInt(f[3])).findFirst().orElseThrow();var stack=sys.getStacks().stream().filter(s->s.getIdValue()==Integer.parseInt(f[2])).findFirst().orElseThrow();var measure=stack.getMeasureAt(staff.getPart());int n=Integer.parseInt(f[4]),d=Integer.parseInt(f[5]);require(n>=2&&n<=12&&(d==2||d==4||d==8),"unsupported time");
   var box=new Rectangle(Integer.parseInt(f[6]),Integer.parseInt(f[7]),Integer.parseInt(f[8]),Integer.parseInt(f[9]));double il=staff.getMeanInterline();require(box.x>=staff.getAbscissa(org.audiveris.omr.util.HorizontalSide.LEFT)&&box.getMaxX()<=staff.getAbscissa(org.audiveris.omr.util.HorizontalSide.RIGHT)&&Math.abs(box.height-4*il)<il&&Math.abs(box.y-staff.getFirstLine().yAt(box.x))<.4*il,"source time geometry mismatch");
   int rank=staff.getPart().getStaves().indexOf(staff);var previous=measure.getTimeSignature(rank);
   String group = String.join("/", f[0], f[1], f[2], f[4], f[5]);
   if (previous != null) {
    int oldNumerator = previous.getNumerator();
    int oldDenominator = previous.getDenominator();
    if (oldNumerator == n && oldDenominator == d) {
     retained++;
     continue;
    }
    Rectangle common = box.intersection(previous.getBounds());
    boolean originalGlyphMatches = common.width >= .75 * Math.min(box.width, previous.getBounds().width)
            && common.height >= .8 * Math.min(box.height, previous.getBounds().height);
    boolean fullStaffConsensus = sys.getStaves().stream().allMatch(s -> staffVotes.get(group).contains(s.getId()));
    boolean canReplace = fullStaffConsensus && originalGlyphMatches
            && previous.getStaff() == staff && previous instanceof TimeWholeInter;
    if (canReplace) {
     meterEdits.add(new MeterEdit(group, () -> {
      previous.modify(null, new TimeRational(n, d));
      previous.setGrade(Math.max(.8, previous.getGrade()));
     }, false));
    } else {
     blockedGroups.add(group);
    }
    meterDisagreements.add(new MeterDisagreement(group, String.join("\t", f[0], f[1], f[2], f[3],
            oldNumerator + "/" + oldDenominator, n + "/" + d)));
    continue;
   }
   require(stack==sys.getStacks().getFirst()||box.x>=stack.getLeft()-.25*il&&box.x<=stack.getLeft()+1.5*il,"source time not at native bar boundary");
   meterEdits.add(new MeterEdit(group, () -> {
    var time = new TimeCustomInter(n, d, .8);
    time.setStaff(staff);
    time.setBounds(box);
    sys.getSig().addVertex(time);
    stack.addInter(time);
    System.out.println("SOURCE_TIME_ADDED " + key + " " + n + "/" + d);
   }, true));
  }
  for (var edit : meterEdits) {
   if (!blockedGroups.contains(edit.group())) {
    edit.apply().run();
    if (edit.addition()) {
     added++;
    }
   }
  }
  var disagreementRows = new ArrayList<String>();
  disagreementRows.add("unit\tsystem\tstack\tstaff\tnative_time\tsource_time\tstate");
  for (var disagreement : meterDisagreements) {
   disagreementRows.add(disagreement.details()
           + (blockedGroups.contains(disagreement.group()) ? "\tunresolved" : "\tcorrected"));
  }
  Files.write(out.resolve("source-time-disagreements.tsv"), disagreementRows);
  Path approvedConflicts = Path.of(a[6]);
  if (!blockedGroups.isEmpty()) {
   List<String> approvedRows = new ArrayList<>();
   for (String row : Files.readAllLines(approvedConflicts)) {
    if (row.isBlank()) {
     continue;
    }
    String[] fields = row.split("\\t", -1);
    require(fields.length == 16, "source time chord claim fields");
    String group = String.join("/", fields[0], fields[1], fields[2], fields[4], fields[5]);
    if (!blockedGroups.contains(group)) {
     approvedRows.add(row);
    }
   }
   approvedConflicts = out.resolve("source-time-approved-conflicts.tsv");
   Files.write(approvedConflicts, approvedRows);
  }
  SourceTimeConflicts335.repair(book, approvedConflicts, out.resolve("source-time-conflicts.tsv"));
  var expectedHeads=new TreeSet<String>(before);
  for(String line:Files.readAllLines(out.resolve("source-time-conflicts.tsv"))){var f=line.split("\\t",-1);if(f.length>=7&&f[5].equals("retired"))for(String head:f[6].split(","))require(expectedHeads.remove(f[0]+"/"+f[1]+"/"+head),"retired source head absent from original");}
  // Prepare source-proven meter before LINKS/RHYTHMS; leave the native step cursor unchanged.
  require(heads(book).equals(expectedHeads),"source meter recompute changed physical heads");
  for(var stub:book.getStubs())stub.setModified(true);book.setModified(true);book.store(out.resolve("refined.omr"),false);book=Book.loadBook(out.resolve("refined.omr"));require(book!=null&&heads(book).equals(expectedHeads),"reloaded native heads changed");
  Files.writeString(out.resolve("meter-recovery.tsv"),"added_times\t"+added+"\nretained_times\t"+retained+"\nrestored_barlines\t"+restoredBars+"\nretired_time_heads\t"+(before.size()-expectedHeads.size())+"\n");require(sha(input).equals(hash),"original native archive changed");System.out.println("SOURCE_TIMES_EARLY383_PREPARED added="+added+" retained="+retained);System.exit(0);
 }
}



