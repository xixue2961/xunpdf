package org.audiveris.omr.score;
import org.audiveris.omr.sig.inter.MetronomeInter;
import org.audiveris.proxymusic.Direction;import org.audiveris.proxymusic.ScorePartwise;import org.audiveris.proxymusic.ScorePart;import org.audiveris.proxymusic.Note;import org.audiveris.proxymusic.Backup;import org.audiveris.proxymusic.Forward;
import java.util.*;import java.nio.file.*;import java.nio.charset.StandardCharsets;
public final class SourceTempoLedger323 {
 record Proof(String crop,String source){}
 static final IdentityHashMap<MetronomeInter,Proof> proofs=new IdentityHashMap<>();
 static final ArrayList<String> rows=new ArrayList<>();static int sequence;
 public static void clear(){proofs.clear();rows.clear();sequence=0;}
 public static void register(MetronomeInter m,String crop,String source){proofs.put(m,new Proof(crop,source));}
 static String text(Object value){return value==null?"":value.toString();}
 static String b64(String value){return Base64.getEncoder().encodeToString(value.getBytes(StandardCharsets.UTF_8));}
 public static void mark(MetronomeInter metro,Direction direction,ScorePartwise.Part part,ScorePartwise.Part.Measure measure){
  var proof=proofs.get(metro);if(proof==null)return;
  int unit=metro.getStaff().getSystem().getSheet().getStub().getNumber(),system=metro.getStaff().getSystem().getId();
  String id="xun-tempo-u"+unit+"-s"+system+"-m"+metro.getId()+"-e"+(++sequence);direction.setId(id);
  var metronomes=direction.getDirectionType().stream().map(t->t.getMetronome()).filter(Objects::nonNull).toList();if(metronomes.size()!=1)throw new IllegalArgumentException("ambiguous exported metronome");var m=metronomes.getFirst();
  if(!metro.getNote().toMusicXml().equals(m.getBeatUnit())||metro.getNote().hasDot()!=(m.getBeatUnitDot().size()==1))throw new IllegalArgumentException("exported tempo differs from verified source note");
  String partId=((ScorePart)part.getId()).getId();long anchor=measure.getNoteOrBackupOrForward().stream().filter(e->e instanceof Note||e instanceof Backup||e instanceof Forward).count();
  rows.add(String.join("\t",id,""+unit,""+system,""+metro.getId(),proof.source(),proof.crop(),partId,measure.getNumber(),""+anchor,text(direction.getStaff()),text(direction.getVoice()),m.getBeatUnit(),""+m.getBeatUnitDot().size(),direction.getSound().getTempo().toPlainString(),b64(m.getPerMinute().getValue())));
 }
 public static void write(Path path)throws Exception{var all=new ArrayList<String>();all.add("id\tunit\tsystem\tmetro\tsource_sha256\tcrop_sha256\tpart\tmeasure\tanchor\tstaff\tvoice\tbeat_unit\tdots\tqpm\tper_minute_b64");all.addAll(rows);Files.write(path,all,StandardCharsets.UTF_8);}
}
