package org.audiveris.omr.score;
import org.audiveris.omr.sheet.Book;
import org.audiveris.omr.sig.inter.*;
import org.audiveris.omr.sig.relation.ChordPedalRelation;
import org.audiveris.omr.glyph.Shape;
import org.audiveris.omr.text.TextRole;
import org.audiveris.proxymusic.Direction;import org.audiveris.proxymusic.ScorePartwise;import org.audiveris.proxymusic.ScorePart;import org.audiveris.proxymusic.Note;import org.audiveris.proxymusic.Backup;import org.audiveris.proxymusic.Forward;
import java.nio.file.*;import java.util.*;import java.nio.charset.StandardCharsets;
public class SourceTextPedals327 {
 record Proof(PedalInter pedal,WordInter word,int unit,int ps,int ws,String sourceSha,double bbox,double ink,double pixels){}
 static final IdentityHashMap<PedalInter,Proof> registered=new IdentityHashMap<>();static final ArrayList<String> ledger=new ArrayList<>();static int serial;
 static void require(boolean ok,String msg){if(!ok)throw new IllegalArgumentException(msg);}
 static String sha(Path p)throws Exception{return java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(p)));}
 static String b64(String s){return Base64.getEncoder().encodeToString(s.getBytes(StandardCharsets.UTF_8));}
 static double ink(org.audiveris.omr.glyph.Glyph a,org.audiveris.omr.glyph.Glyph b){if(a==null||b==null)return -1;var ab=a.getBuffer();var bb=b.getBuffer();int count=0,covered=0;for(int y=0;y<ab.getHeight();y++)for(int x=0;x<ab.getWidth();x++)if(ab.get(x,y)==0){count++;int bx=a.getLeft()+x-b.getLeft(),by=a.getTop()+y-b.getTop();if(bx>=0&&by>=0&&bx<bb.getWidth()&&by<bb.getHeight()&&bb.get(bx,by)==0)covered++;}return count==0?-1:(double)covered/count;}
 static double sourceInk(org.audiveris.omr.glyph.Glyph g,java.awt.image.BufferedImage im){if(g==null)return -1;var buf=g.getBuffer();int count=0,covered=0;for(int y=0;y<buf.getHeight();y++)for(int x=0;x<buf.getWidth();x++)if(buf.get(x,y)==0){count++;int xx=g.getLeft()+x,yy=g.getTop()+y;if(xx<0||yy<0||xx>=im.getWidth()||yy>=im.getHeight())continue;int gray;if(im.getRaster().getNumBands()==1)gray=im.getRaster().getSample(xx,yy,0);else{int rgb=im.getRGB(xx,yy);gray=(int)Math.round(.299*((rgb>>16)&255)+.587*((rgb>>8)&255)+.114*(rgb&255));}if(gray<230)covered++;}return count==0?-1:(double)covered/count;}
 static List<Proof> find(Book book,Path source)throws Exception{
  var found=new ArrayList<Proof>();
  for(var stub:book.getStubs()){var systems=stub.getSheet().getSystems();Path png=source.resolve(String.format("unit-%03d.png",stub.getNumber()));if(!Files.isRegularFile(png))continue;java.awt.image.BufferedImage image=null;String hash=null;
   for(int i=0;i+1<systems.size();i++){var ps=systems.get(i);var ws=systems.get(i+1);
    for(var p:ps.getSig().vertexSet())if(p instanceof PedalInter pedal&&p.getShape()==Shape.PEDAL_UP_MARK){
     var bounds=p.getBounds();if(bounds==null||p.getGlyph()==null)continue;var edges=new ArrayList<>(ps.getSig().edgesOf(p));if(edges.size()!=1||!(edges.getFirst() instanceof ChordPedalRelation))continue;var chord=ps.getSig().getEdgeSource(edges.getFirst());if(!(chord instanceof AbstractChordInter c)||c.getStaff()==null||c.getStaff().getPart().getStaves().size()!=2||c.getStaff()!=c.getStaff().getPart().getStaves().getLast())continue;
     var choices=new ArrayList<Proof>();
     for(var w:ws.getSig().vertexSet())if(w instanceof WordInter word&&!(word instanceof MusicWordInter)&&word.getGlyph()!=null){String text=word.getValue();if(text==null||text.codePointCount(0,text.length())<2||!text.codePoints().allMatch(cp->Character.UnicodeScript.of(cp)==Character.UnicodeScript.HAN))continue;
      if(!(word.getEnsemble() instanceof SentenceInter sentence)||sentence.getRole()!=TextRole.Direction||word.getStaff()!=ws.getStaves().getFirst())continue;
      if(word.getGrade()<.7||word.getGrade()<p.getGrade()+.1)continue;var wb=word.getBounds();if(wb==null||!wb.intersects(bounds))continue;var area=wb.intersection(bounds);double cover=area.getWidth()*area.getHeight()/(bounds.getWidth()*bounds.getHeight());if(cover<.98)continue;double glyphCover=ink(p.getGlyph(),word.getGlyph());if(glyphCover<.995)continue;
      if(image==null){image=javax.imageio.ImageIO.read(png.toFile());hash=sha(png);}double pixels=sourceInk(p.getGlyph(),image);if(pixels<.99)continue;choices.add(new Proof(pedal,word,stub.getNumber(),ps.getId(),ws.getId(),hash,cover,glyphCover,pixels));
     }
     if(choices.size()==1)found.add(choices.getFirst());
    }
   }
  }
  return found;
 }
 public static void mark(PedalInter p,Direction d,ScorePartwise.Part part,ScorePartwise.Part.Measure measure){var proof=registered.get(p);if(proof==null)return;String id="xun-text-pedal-u"+proof.unit()+"-s"+proof.ps()+"-p"+p.getId()+"-e"+(++serial);d.setId(id);long anchor=measure.getNoteOrBackupOrForward().stream().filter(e->e instanceof Note||e instanceof Backup||e instanceof Forward).count();ledger.add(String.join("\t",id,""+proof.unit(),""+proof.ps(),""+p.getId(),""+proof.ws(),""+proof.word().getId(),b64(proof.word().getValue()),proof.sourceSha(),((ScorePart)part.getId()).getId(),measure.getNumber(),""+anchor,d.getStaff()==null?"1":d.getStaff().toString()));}
 public static void main(String[] a){try{run(a);System.exit(0);}catch(Throwable t){t.printStackTrace();System.exit(1);}}
 static void run(String[] a)throws Exception{
  ((ch.qos.logback.classic.Logger)org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME)).setLevel(ch.qos.logback.classic.Level.WARN);Path nativePath=Path.of(a[0]),source=Path.of(a[2]),out=Path.of(a[3]);String mode=a[4];require(sha(nativePath).equals(a[1]),"native source archive SHA changed");var book=Book.loadBook(nativePath);require(book!=null,"native load failed");var proofs=find(book,source);var report=new ArrayList<String>();report.add("unit\tpedal_system\tpedal\tword_system\tword\tword_b64\tsource_sha256\tbbox_cover\tglyph_ink_cover\tsource_ink_cover");
  for(var p:proofs)report.add(String.join("\t",""+p.unit(),""+p.ps(),""+p.pedal().getId(),""+p.ws(),""+p.word().getId(),b64(p.word().getValue()),p.sourceSha(),""+p.bbox(),""+p.ink(),""+p.pixels()));Files.write(out.resolve("source-text-pedals.tsv"),report,StandardCharsets.UTF_8);
  if(mode.equals("scan")){System.out.println("TEXT_PEDAL_SCAN "+proofs.size());return;}
  if(mode.equals("control"))for(var p:proofs)registered.put(p.pedal(),p);
  else if(mode.equals("apply"))for(var p:proofs){require(p.pedal().getSig().edgesOf(p.pedal()).size()==1,"pedal relations changed");p.pedal().remove(false);}
  else throw new IllegalArgumentException("mode");
  if(a.length>5&&!a[5].isBlank()){var field=PartCollation.class.getDeclaredField("constants");field.setAccessible(true);var constants=field.get(null);var hint=constants.getClass().getDeclaredField("partsHint");hint.setAccessible(true);((org.audiveris.omr.constant.Constant.String)hint.get(constants)).setStringValue(a[5]);for(var score:book.getScores()){score.setLogicalsLocked(false);score.setLogicalParts(null);new ScoreReduction(score).reduce(book.getValidStubs());}}
  book.setExportPathSansExt(out.resolve("result"));require(book.export(book.getValidStubs(),book.getScores()),"native export failed");var ledgerRows=new ArrayList<String>();ledgerRows.add("id\tunit\tpedal_system\tpedal\tword_system\tword\tword_b64\tsource_sha256\tpart\tmeasure\tanchor\tstaff");ledgerRows.addAll(ledger);Files.write(out.resolve("text-pedal-ledger.tsv"),ledgerRows,StandardCharsets.UTF_8);require(sha(nativePath).equals(a[1]),"native archive changed");System.out.println("TEXT_PEDAL_EXPORT_OK "+mode+" "+proofs.size());
 }
}
