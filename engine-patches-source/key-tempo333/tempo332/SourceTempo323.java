import org.audiveris.omr.sheet.Book;
import org.audiveris.omr.sig.inter.*;
import org.audiveris.omr.glyph.*;
import org.audiveris.omr.run.*;
import org.audiveris.omr.classifier.*;
import java.nio.file.*;import java.util.*;import java.awt.Rectangle;
public class SourceTempo323 {
 static String sha(Path p)throws Exception{return java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(p)));}
 static void require(boolean ok,String message){if(!ok)throw new IllegalArgumentException(message);}
 record Proposal(MetronomeInter metro,BeatUnitInter.Note note,Rectangle bounds,double grade,String cropSha,String sourceSha){}
 public static void main(String[] a){try{run(a);System.exit(0);}catch(Throwable t){t.printStackTrace();System.exit(1);}}
 static void run(String[] a)throws Exception{
 ((ch.qos.logback.classic.Logger)org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME)).setLevel(ch.qos.logback.classic.Level.WARN);
 org.audiveris.omr.score.SourceTempoLedger323.clear();
 if(a.length>6&&!a[6].isBlank()){var field=org.audiveris.omr.score.PartCollation.class.getDeclaredField("constants");field.setAccessible(true);var constants=field.get(null);var hint=constants.getClass().getDeclaredField("partsHint");hint.setAccessible(true);((org.audiveris.omr.constant.Constant.String)hint.get(constants)).setStringValue(a[6]);}
 Path nativePath=Path.of(a[0]),sourceJob=Path.of(a[2]).toRealPath(),plan=Path.of(a[3]),out=Path.of(a[4]);require(sha(nativePath).equals(a[1]),"native archive SHA changed");var book=Book.loadBook(nativePath);require(book!=null,"book load");
 var proposals=new ArrayList<Proposal>();var rows=new ArrayList<String>();rows.add("unit\tsystem\tmetro\tstate\told_shape\ttop_shape\tgrade\tmargin\tdecision_method");var seen=new HashSet<String>();
 // Validate the complete plan and classify all source glyphs before mutating any native object.
 for(String line:Files.readAllLines(plan)){
  if(line.isBlank())continue;String[] f=line.split("\t",-1);require(f.length==17,"plan width");int unit=Integer.parseInt(f[0]),system=Integer.parseInt(f[1]),id=Integer.parseInt(f[2]),staff=Integer.parseInt(f[3]);require(seen.add(unit+":"+system+":"+id),"duplicate native target");
  var sheet=book.getStub(unit).getSheet();var sys=sheet.getSystems().stream().filter(s->s.getId()==system).findFirst().orElseThrow();var inter=sys.getSig().vertexSet().stream().filter(v->v.getId()==id).findFirst().orElseThrow();require(inter instanceof MetronomeInter,"target not a metronome");var metro=(MetronomeInter)inter;require(metro.getStaff()!=null&&metro.getStaff().getId()==staff,"native staff mismatch");
  var original=new Rectangle(Integer.parseInt(f[4]),Integer.parseInt(f[5]),Integer.parseInt(f[6]),Integer.parseInt(f[7]));require(original.equals(metro.getBounds()),"native metronome bounds changed: "+id+" expected="+original+" actual="+metro.getBounds());String members=metro.getMembers().stream().map(v->v.getId()).sorted().map(String::valueOf).collect(java.util.stream.Collectors.joining(","));require(members.equals(f[8]),"native members changed");
  var units=metro.getMembers().stream().filter(v->v instanceof BeatUnitInter).map(v->(BeatUnitInter)v).toList();require(units.size()<=1,"ambiguous native beat units");
  Path crop=Path.of(f[13]),source=Path.of(f[15]);require(source.toRealPath().equals(sourceJob.resolve(String.format("unit-%03d.png",unit)).toRealPath()),"source unit identity mismatch");require(sha(source).equals(f[16]),"source PNG SHA changed");require(sha(crop).equals(f[14]),"source glyph SHA changed");
  int x=Integer.parseInt(f[9]),y=Integer.parseInt(f[10]),w=Integer.parseInt(f[11]),h=Integer.parseInt(f[12]);var image=javax.imageio.ImageIO.read(crop.toFile());var originalImage=javax.imageio.ImageIO.read(source.toFile());require(image!=null&&image.getWidth()==w+4&&image.getHeight()==h+4,"crop geometry changed");require(x>=0&&y>=0&&x+w<=originalImage.getWidth()&&y+h<=originalImage.getHeight(),"source bounds outside image");
  var bp=new ij.process.ByteProcessor(image);for(int yy=0;yy<h;yy++)for(int xx=0;xx<w;xx++)if(bp.get(xx+2,yy+2)==0){int rgb=originalImage.getRGB(x+xx,y+yy);int gray=originalImage.getRaster().getNumBands()==1?originalImage.getRaster().getSample(x+xx,y+yy,0):(int)Math.round(.299*((rgb>>16)&255)+.587*((rgb>>8)&255)+.114*(rgb&255));require(gray<180,"glyph contains ink absent in source PNG");}
  var glyph=new Glyph(x-2,y-2,new RunTableFactory(Orientation.VERTICAL).createTable(bp));var evals=ShapeClassifier.getInstance().evaluate(glyph,sys,5,0.0,null);require(evals.length>0,"no glyph evaluations");var top=evals[0];double second=evals.length>1?evals[1].grade:0;var note=BeatUnitInter.Note.noteOf(top.shape);boolean high=note!=null&&top.grade>=.8&&top.grade-second>=.3;boolean structural=note==BeatUnitInter.Note.QUARTER&&top.grade>=.3&&top.grade-second>=.2&&org.audiveris.omr.score.QuarterGeometry332.accepts(bp);boolean accepted=high||structural;String decision=high?"classifier":structural?"quarter_geometry":"uncertain";String old=units.isEmpty()?"missing":String.valueOf(units.getFirst().getShape());String state=accepted?(metro.getNote()==note?"already_matches_source":"accepted"):"uncertain";
  rows.add(unit+"\t"+system+"\t"+id+"\t"+state+"\t"+old+"\t"+top.shape+"\t"+top.grade+"\t"+(top.grade-second)+"\t"+decision);if(accepted&&!state.equals("already_matches_source"))proposals.add(new Proposal(metro,note,new Rectangle(x,y,w,h),top.grade,f[14],f[16]));
 }
 if(a.length>5&&a[5].equals("validate-only")){Files.write(out.resolve("decisions.tsv"),rows);System.out.println("TEMPO_VALIDATE_OK "+proposals.size());return;}
 if(a.length>5&&a[5].equals("control"))proposals.clear();
 for(var p:proposals){var metro=p.metro();var sheet=metro.getStaff().getSystem().getSheet();var units=metro.getMembers().stream().filter(v->v instanceof BeatUnitInter).map(v->(BeatUnitInter)v).toList();
  if(!units.isEmpty()){var beat=units.getFirst();beat.setValue(p.note().getString());beat.setBounds(p.bounds());}
  else{var font=new org.audiveris.omr.ui.symbol.MusicFont(new org.audiveris.omr.text.FontInfo(metro.getMeanFont().pointSize,sheet.getStub().getMusicFamily().getFontName()));var b=p.bounds();var beat=new BeatUnitInter(null,b,p.grade(),p.note().getString(),font,p.note(),new java.awt.Point(b.x,b.y+b.height));beat.setStaff(metro.getStaff());metro.getSig().addVertex(beat);metro.addMember(beat);}
  require(metro.getNote()==p.note(),"native note assignment failed");sheet.getStub().setModified(true);org.audiveris.omr.score.SourceTempoLedger323.register(metro,p.cropSha(),p.sourceSha());
 }
 if(a.length>6&&!a[6].isBlank())for(var score:book.getScores()){score.setLogicalsLocked(false);score.setLogicalParts(null);new org.audiveris.omr.score.ScoreReduction(score).reduce(book.getValidStubs());}
 Files.write(out.resolve("decisions.tsv"),rows);book.setExportPathSansExt(out.resolve("result"));require(book.export(book.getValidStubs(),book.getScores()),"export failed");org.audiveris.omr.score.SourceTempoLedger323.write(out.resolve("tempo-ledger.tsv"));if(a.length>5&&a[5].equals("save"))book.store(out.resolve("restored.omr"),false);System.out.println("SOURCE_TEMPO_OK changed="+proposals.size());
 }
}
