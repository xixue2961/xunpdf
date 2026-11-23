package org.audiveris.omr.score;

import java.awt.Rectangle;
import java.awt.geom.Line2D;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;
import javax.imageio.ImageIO;
import org.audiveris.omr.glyph.Shape;
import org.audiveris.omr.sheet.Book;
import org.audiveris.omr.sheet.rhythm.MeasureStack;
import org.audiveris.omr.sheet.rhythm.StackRhythm;
import org.audiveris.omr.sig.inter.*;
import org.audiveris.omr.sig.relation.AugmentationRelation;
import org.audiveris.omr.sig.relation.BeamStemRelation;

/** Replay a source-verified beam and rest correction on a native score copy. */
public class SourceShortPianoRhythm542 {
    private static void require(boolean ok,String reason) {
        if (!ok) throw new IllegalArgumentException(reason);
    }
    private static Rectangle bounds(String[] row,int i) {
        return new Rectangle(Integer.parseInt(row[i]),Integer.parseInt(row[i+1]),
                             Integer.parseInt(row[i+2]),Integer.parseInt(row[i+3]));
    }
    public static void main(String[] args) throws Exception {
        require(args.length==7 && Set.of("control","apply").contains(args[6]),"arguments");
        var logger=(ch.qos.logback.classic.Logger)org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        logger.setLevel(ch.qos.logback.classic.Level.WARN);
        Path input=Path.of(args[0]), source=Path.of(args[2]), plan=Path.of(args[3]), out=Path.of(args[5]);
        require(SourceCurvedRest400.sha(input).equals(args[1]) && SourceCurvedRest400.sha(plan).equals(args[4]),
                "source or evidence identity changed");
        Files.createDirectory(out);
        var book=Book.loadBook(input);
        SourceStructure335.source=source;
        var original=SourceCurvedRest400.physical(book);
        var expected=new HashSet<>(original);
        boolean apply=args[6].equals("apply");
        var stacks=new LinkedHashSet<MeasureStack>();
        var replacementGroups=new LinkedHashMap<List<StemInter>,BeamGroupInter>();
        var previousGroups=new HashSet<BeamGroupInter>();
        var records=new ArrayList<String>();
        for (String line:Files.readAllLines(plan)) {
            if (line.isBlank()) continue;
            var row=line.split("\\t");
            int unit=Integer.parseInt(row[1]), sid=Integer.parseInt(row[2]), staffId=Integer.parseInt(row[3]);
            var png=source.resolve(String.format("unit-%03d.png",unit));
            require(SourceCurvedRest400.sha(png).equals(row[row.length-1]),"original page changed");
            var sheet=book.getStub(unit).getSheet();
            var system=sheet.getSystems().stream().filter(v->v.getId()==sid).findFirst().orElseThrow();
            var staff=system.getStaves().stream().filter(v->v.getId()==staffId).findFirst().orElseThrow();
            require(staff.getPart().getStaves().size()==2,"piano part required");
            var sig=system.getSig();
            java.util.function.Function<String,Inter> find=id->sig.vertexSet().stream()
                    .filter(n->n.getId()==Integer.parseInt(id)).findFirst().orElseThrow();
            switch (row[0]) {
                case "BEAM" -> {
                    var ids=row[4].split(",");
                    var stemIds=row[5].split(",");
                    require(ids.length==4 && stemIds.length==4,"four consecutive original notes required");
                    var chords=new ArrayList<HeadChordInter>();
                    var stems=new ArrayList<StemInter>();
                    for (int i=0;i<ids.length;i++) {
                        var chord=(HeadChordInter)find.apply(ids[i]);
                        var stem=(StemInter)find.apply(stemIds[i]);
                        require(chord.getStaff()==staff && chord.getStem()==stem && chord.getNotes().size()==1
                                && chord.getNotes().getFirst().getShape()==Shape.NOTEHEAD_BLACK
                                && chord.getFlagsNumber()==0,"source chord or stem identity changed");
                        chords.add(chord);
                        stems.add(stem);
                    }
                    var bar=chords.getFirst().getMeasure();
                    require(chords.stream().allMatch(c->c.getMeasure()==bar),"group crosses a barline");
                    stacks.add(bar.getStack());
                    var donor=(AbstractBeamInter)find.apply(row[11]);
                    require(donor.getStems().size()==4 && donor.getChords().stream().allMatch(c->c.getStaff()==staff),
                            "source beam donor has the wrong staff or stems");
                    var found=new HashSet<BeamGroupInter>();
                    for (var c:chords) for (var b:c.getBeams()) found.add(b.getGroup());
                    if (!replacementGroups.containsKey(stems)) {
                        require(found.size()==1,"partial native beams not unique");
                        var partial=found.iterator().next();
                        require(partial.getMembers().size()==2,"unsupported partial beam group");
                        for (var b:partial.getMembers()) {
                            require(b instanceof AbstractBeamInter ab && new HashSet<>(ab.getStems())
                                    .equals(Set.of(stems.get(1),stems.get(2))),"partial group should contain only middle stems");
                        }
                        previousGroups.add(partial);
                        var created=new BeamGroupInter();
                        if (apply) sig.addVertex(created);
                        replacementGroups.put(List.copyOf(stems),created);
                    }
                    if (apply) {
                        var median=new Line2D.Double(Double.parseDouble(row[6]),Double.parseDouble(row[7]),
                                                     Double.parseDouble(row[8]),Double.parseDouble(row[9]));
                        var beam=new BeamInter(0.,median,Double.parseDouble(row[10]));
                        beam.setManual(true);
                        sig.addVertex(beam);
                        var group=replacementGroups.get(stems);
                        group.addMember(beam);
                        for (var stem:stems) {
                            var edge=new BeamStemRelation();
                            edge.setExtensionPoint(BeamStemRelation.computeExtensionPoint(beam,stem));
                            edge.setBeamPortion(BeamStemRelation.computeBeamPortion(beam,stem.getCenter().x,sheet.getScale()));
                            edge.setManual(true);
                            sig.addEdge(beam,stem,edge);
                        }
                        group.invalidateCache();
                        require(new HashSet<>(beam.getStems()).equals(new HashSet<>(stems)),"original stem missing from new beam");
                    }
                    records.add("B "+String.join(",",ids));
                }
                case "REST" -> {
                    int stackId=Integer.parseInt(row[4]);
                    var stack=system.getStacks().stream().filter(x->x.getIdValue()==stackId).findFirst().orElseThrow();
                    var bar=stack.getMeasureAt(staff.getPart());
                    Rectangle bbox=bounds(row,5);
                    require(bbox.x>stack.getLeft() && bbox.getMaxX()<stack.getRight(),"rest lies outside the verified bar");
                    require(sig.vertexSet().stream().filter(n->n instanceof RestInter).noneMatch(n->n.getBounds().intersects(bbox)),
                            "rest already present");
                    stacks.add(stack);
                    if (apply) {
                        var image=ImageIO.read(png.toFile());
                        var glyph=SourceMissingChordHeads415.glyph(image,bbox,sheet);
                        var rest=new RestInter(glyph,Shape.EIGHTH_REST,.99,staff,Double.parseDouble(row[9]));
                        rest.setBounds(bbox);
                        rest.setManual(true);
                        sig.addVertex(rest);
                        var chord=new RestChordInter(.99);
                        chord.setStaff(staff);
                        chord.setMeasure(bar);
                        sig.addVertex(chord);
                        bar.addInter(chord);
                        chord.addMember(rest);
                        require(expected.add(SourceCurvedRest400.symbol(unit,sid,rest)),"original rest added twice");
                    }
                    records.add("R "+row[10]);
                }
                case "DOT" -> {
                    var dot=(AugmentationDotInter)find.apply(row[4]);
                    var head=(HeadInter)find.apply(row[5]);
                    require(dot.getStaff()==staff && head.getStaff()==staff && dot.getBounds().equals(bounds(row,6))
                            && sig.getRelation(dot,head,AugmentationRelation.class)!=null,"misread slur fragment changed");
                    var chord=head.getChord();
                    stacks.add(chord.getMeasure().getStack());
                    if (apply) {
                        dot.remove();
                        chord.invalidateCache();
                    }
                    records.add("D "+row[4]);
                }
                default -> throw new IllegalArgumentException("unverified graph action");
            }
        }
        if (apply) {
            for (var group:previousGroups) {
                for (var member:new ArrayList<>(group.getMembers())) member.remove();
                if (!group.isRemoved()) group.remove();
            }
        }
        for (var stack:stacks) {
            var meter=stack.getExpectedDuration();
            require(meter!=null,"independent meter absent");
            for (var measure:stack.getMeasures()) {
                if (apply) for (var chord:measure.getStandardChords()) chord.invalidateCache();
                measure.resetRhythm();
            }
            stack.resetRhythm();
            new StackRhythm(stack).process(meter);
            stack.getSystem().getSheet().getStub().setModified(true);
        }
        require(expected.equals(SourceCurvedRest400.physical(book)),"irrelevant source head or rest changed");
        book.setModified(true);
        book.store(out.resolve("refined.omr"),false);
        book.setExportPathSansExt(out.resolve("result"));
        require(book.export(book.getValidStubs(),book.getScores()),"native score export failed");
        SourceStructure335.writeLedgers(out);
        Files.write(out.resolve("records.txt"),records);
        require(SourceCurvedRest400.sha(input).equals(args[1]),"input source changed");
        System.out.println("SOURCE_SHORT_PIANO_RHYTHM_542 "+args[6]+" stacks="+stacks.size()+" edits="+records.size());
        System.exit(0);
    }
}
