package org.audiveris.omr.score;
import java.awt.Rectangle;
import java.util.*;
import java.nio.file.*;
import org.audiveris.omr.sheet.Book;
import org.audiveris.omr.sig.inter.*;
import org.audiveris.omr.sig.relation.*;

/** Retire only note interpretations contradicted by source-supported time signatures. */
public final class SourceTimeConflicts335 {
 public static void repair(Book book,Path plan,Path reportPath)throws Exception{
  List<String> report=new ArrayList<>();report.add("unit\tsystem\tstack\tstaff\tchord\tstatus\theads\tmoved_relations\tretired_symbols\treason");
  for(String line:Files.readAllLines(plan)){
   String[] f=line.split("\\t");if(f.length!=16)throw new IllegalArgumentException("source conflict format");
   String prefix=String.join("\t",Arrays.copyOf(f,4))+"\t"+f[14];
   try {
    var sys=book.getStub(Integer.parseInt(f[0])).getSheet().getSystems().stream().filter(s->s.getId()==Integer.parseInt(f[1])).findFirst().orElseThrow();
    var sig=sys.getSig();var staff=sys.getStaves().stream().filter(s->s.getId()==Integer.parseInt(f[3])).findFirst().orElseThrow();
    var stack=sys.getStacks().stream().filter(s->s.getIdValue()==Integer.parseInt(f[2])).findFirst().orElseThrow();var measure=stack.getMeasureAt(staff.getPart());
    Rectangle timeBox=new Rectangle(Integer.parseInt(f[6]),Integer.parseInt(f[7]),Integer.parseInt(f[8]),Integer.parseInt(f[9]));
    Rectangle box=new Rectangle(Integer.parseInt(f[10]),Integer.parseInt(f[11]),Integer.parseInt(f[12]),Integer.parseInt(f[13]));
    double proofMargin=.65*staff.getMeanInterline();if(timeBox.x-box.x>proofMargin||timeBox.y-box.y>proofMargin||box.getMaxX()-timeBox.getMaxX()>proofMargin||box.getMaxY()-timeBox.getMaxY()>proofMargin)throw new IllegalArgumentException("conflict region too far from source time");
    var time=measure.getTimeSignature(staff.getPart().getStaves().indexOf(staff));
    if(time==null||time.getNumerator()!=Integer.parseInt(f[4])||time.getDenominator()!=Integer.parseInt(f[5])||!timeBox.equals(time.getBounds()))throw new IllegalArgumentException("source time identity mismatch");
    var inter=sig.vertexSet().stream().filter(v->v.getId()==Integer.parseInt(f[14])).findFirst().orElseThrow();
    if(!(inter instanceof HeadChordInter chord)||chord.getMeasure()!=measure||chord.getStaff()!=staff)throw new IllegalArgumentException("chord ownership mismatch");
    var heads=new ArrayList<Inter>(chord.getNotes());Set<Integer> expected=new HashSet<>();for(String id:f[15].split(","))expected.add(Integer.parseInt(id));
    if(heads.size()!=expected.size()||heads.isEmpty())throw new IllegalArgumentException("source heads mismatch");
    for(var head:heads){
     if(!(head instanceof HeadInter)||!expected.remove(head.getId())||head.getStaff()!=staff||!box.contains(head.getBounds()))throw new IllegalArgumentException("head outside source time region");
     for(var rel:sig.edgesOf(head))if(!(rel instanceof Containment||rel instanceof HeadStemRelation||rel instanceof HeadHeadRelation))throw new IllegalArgumentException("unsupported relation on false head");
    }
    var alternatives=measure.getHeadChords().stream().filter(c->c!=chord&&c.getStaff()==staff&&c.getBounds().x>=box.x+box.width&&!c.getNotes().isEmpty()).sorted(Comparator.comparingInt(c->c.getBounds().x)).toList();
    double space=staff.getSpecificInterline();var moves=new LinkedHashMap<Relation,HeadChordInter>();var retiredSymbols=new ArrayList<Inter>();
    for(var rel:new ArrayList<Relation>(sig.edgesOf(chord))){
     if(rel instanceof Containment||rel instanceof ChordStemRelation)continue;
     if(sig.getEdgeSource(rel)!=chord)throw new IllegalArgumentException("unexpected relation direction");
     Inter target=sig.getEdgeTarget(rel);
     if(target.getBounds()!=null&&box.contains(target.getBounds())){
      if(!(target instanceof ArticulationInter))throw new IllegalArgumentException("unclassified symbol inside time signature");
      if(sig.edgesOf(target).size()!=1)throw new IllegalArgumentException("shared artifact symbol");retiredSymbols.add(target);continue;
     }
     if(!(rel instanceof ChordSyllableRelation||rel instanceof ChordDynamicsRelation||rel instanceof ChordSentenceRelation))throw new IllegalArgumentException("unsupported semantic attachment");
     if(alternatives.isEmpty())throw new IllegalArgumentException("no source-backed destination chord for attachment");
     HeadChordInter destination;
     if(rel instanceof ChordSentenceRelation){
      destination=alternatives.get(0);if(destination.getBounds().x-(box.x+box.width)>2*space)throw new IllegalArgumentException("direction destination too far");
     }else{
      double anchor=target.getBounds().getCenterX();var near=new ArrayList<HeadChordInter>(alternatives);near.sort(Comparator.comparingDouble(c->Math.abs(c.getHeadLocation().getX()-anchor)));destination=near.get(0);
      double best=Math.abs(destination.getHeadLocation().getX()-anchor);
      if(best>1.5*space||near.size()>1&&Math.abs(near.get(1).getHeadLocation().getX()-anchor)-best<.5*space)throw new IllegalArgumentException("ambiguous attachment destination");
     }
     if(sig.getEdge(destination,target)!=null)throw new IllegalArgumentException("attachment already linked to destination");moves.put(rel,destination);
    }
    List<String> moved=new ArrayList<>();
    for(var entry:moves.entrySet()){
     var rel=entry.getKey();var target=sig.getEdgeTarget(rel);sig.removeEdge(rel);if(!sig.addEdge(entry.getValue(),target,rel))throw new AssertionError("relation transfer failed");moved.add(target.getId()+"->"+entry.getValue().getId()+":"+rel.getClass().getSimpleName());
    }
    List<String> removed=new ArrayList<>();for(var artifact:retiredSymbols){removed.add(Integer.toString(artifact.getId()));artifact.remove(false);}
    for(var head:heads)head.remove(false);if(!chord.isRemoved())chord.remove(false);
    report.add(prefix+"\tretired\t"+f[15]+"\t"+String.join(",",moved)+"\t"+String.join(",",removed)+"\t");
    System.out.println("SOURCE_TIME_FALSE_CHORD_RETIRED "+prefix+" heads="+f[15]+" moved="+moved);
   }catch(IllegalArgumentException ex){report.add(prefix+"\tretained\t"+f[15]+"\t\t\t"+ex.getMessage());System.out.println("SOURCE_TIME_CONFLICT_RETAINED "+prefix+" reason="+ex.getMessage());}
  }
  Files.write(reportPath,report);
 }
}
