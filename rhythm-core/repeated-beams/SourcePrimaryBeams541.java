package org.audiveris.omr.score;

import java.awt.Point;
import java.awt.Rectangle;
import java.awt.geom.Line2D;
import java.awt.geom.Point2D;
import java.awt.image.BufferedImage;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;
import javax.imageio.ImageIO;
import org.audiveris.omr.glyph.Shape;
import org.audiveris.omr.sheet.Book;
import org.audiveris.omr.sheet.rhythm.MeasureStack;
import org.audiveris.omr.sheet.rhythm.StackRhythm;
import org.audiveris.omr.sig.inter.*;
import org.audiveris.omr.sig.relation.BeamStemRelation;
import org.audiveris.omr.sig.relation.HeadStemRelation;
import org.audiveris.omr.util.VerticalSide;

/** Test an original-image-bound native transaction in an isolated book copy. */
public class SourcePrimaryBeams541 {
    private static void require(boolean condition, String why) {
        if (!condition) throw new IllegalArgumentException(why);
    }

    private static Rectangle box(String[] fields, int i) {
        return new Rectangle(Integer.parseInt(fields[i]), Integer.parseInt(fields[i + 1]),
                             Integer.parseInt(fields[i + 2]), Integer.parseInt(fields[i + 3]));
    }

    public static void main(String[] a) throws Exception {
        require(a.length == 7 && Set.of("control", "apply").contains(a[6]), "arguments");
        ((ch.qos.logback.classic.Logger)org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME))
            .setLevel(ch.qos.logback.classic.Level.WARN);
        Path input = Path.of(a[0]), images = Path.of(a[2]), plan = Path.of(a[3]), out = Path.of(a[5]);
        require(SourceCurvedRest400.sha(input).equals(a[1]) && SourceCurvedRest400.sha(plan).equals(a[4]),
                "source archive or plan changed");
        Files.createDirectory(out);
        Book book = Book.loadBook(input);
        SourceStructure335.source = images;
        boolean mutate = a[6].equals("apply");
        var stacks = new LinkedHashSet<MeasureStack>();
        var falseHeads = new LinkedHashSet<HeadInter>();
        var fixedStems = new LinkedHashSet<StemInter>();
        var dotCandidates = new ArrayList<AugmentationDotInter>();
        var addedHeadBounds = new ArrayList<Rectangle>();
        var headRows = new ArrayList<String[]>();
        var beamRows = new ArrayList<String[]>();
        var pitchRows = new ArrayList<String[]>();
        var dotRows = new ArrayList<String[]>();
        for (String line : Files.readAllLines(plan)) {
            if (line.isBlank()) continue;
            var f = line.split("\\t");
            switch (f[0]) {
                case "HEAD" -> headRows.add(f);
                case "BEAM" -> beamRows.add(f);
                case "PITCH" -> pitchRows.add(f);
                case "DOT" -> dotRows.add(f);
                default -> throw new IllegalArgumentException("unsupported local proof line");
            }
        }
        var pending = new ArrayList<String[]>();
        pending.addAll(headRows);
        pending.addAll(pitchRows);
        pending.addAll(beamRows);
        pending.addAll(dotRows);
        var loaded = new HashMap<Integer, BufferedImage>();
        for (var f : pending) {
            int unit = Integer.parseInt(f[1]);
            int sysId = Integer.parseInt(f[2]);
            int staffId = Integer.parseInt(f[3]);
            var path = images.resolve(String.format("unit-%03d.png", unit));
            require(SourceCurvedRest400.sha(path).equals(f[f.length-1]), "source pixels changed");
            if (!loaded.containsKey(unit)) loaded.put(unit, ImageIO.read(path.toFile()));
            var image = loaded.get(unit);
            var sheet = book.getStub(unit).getSheet();
            var system = sheet.getSystems().stream().filter(s -> s.getId() == sysId).findFirst().orElseThrow();
            var staff = system.getStaves().stream().filter(s -> s.getId() == staffId).findFirst().orElseThrow();
            require(staff.getPart().getStaves().size() == 2, "expected piano grand staff");
            var sig = system.getSig();
            java.util.function.Function<String,Inter> node = id -> sig.vertexSet().stream()
                .filter(i -> i.getId()==Integer.parseInt(id)).findFirst().orElseThrow();
            if (f[0].equals("HEAD")) {
                var chord = (HeadChordInter)node.apply(f[4]);
                var ghost = (HeadInter)node.apply(f[5]);
                var donor = (HeadInter)node.apply(f[6]);
                var b = box(f, 7);
                require(chord.getStaff()==staff && donor.getStaff()==staff && chord.getStem()!=null
                    && ghost.getChord()==chord && ghost.getShape()==Shape.NOTEHEAD_VOID, "false-head owner changed");
                double pitch = donor.getPitch();
                require(Math.abs(staff.pitchPositionOf(new Point2D.Double(b.getCenterX(),b.getCenterY()))-pitch)<.6,
                        "new black head outside source pitch band");
                var stack = chord.getMeasure().getStack();
                require(b.x >= stack.getLeft() && b.getMaxX() <= stack.getRight(), "source head beyond barline");
                var stem = chord.getStem();
                stacks.add(stack);
                falseHeads.add(ghost);
                addedHeadBounds.add(b);
                if (mutate) {
                    if (fixedStems.add(stem)) {
                        var line = stem.getMedian();
                        double y = line.getY2()+staff.getMeanInterline();
                        double x = line.getX2();
                        int a0 = (int)Math.round(line.getY2()), b0 = (int)Math.round(y), covered = 0;
                        for (int yy = a0; yy <= b0; yy++) {
                            int col = (int)Math.round(x);
                            if (SourceMissingChordHeads415.gray(image,col-1,yy)<160
                                || SourceMissingChordHeads415.gray(image,col,yy)<160
                                || SourceMissingChordHeads415.gray(image,col+1,yy)<160) covered++;
                        }
                        require(covered >= .78 * (b0-a0+1), "original stem does not reach its beam");
                        var bounds = new Rectangle(stem.getBounds());
                        bounds.height = Math.max(bounds.height,(int)Math.ceil(y)-bounds.y+1);
                        stem.setGlyph(SourceMissingChordHeads415.glyph(image,bounds,sheet));
                        stem.setBounds(bounds);
                        stem.setMedian(line.getP1(),new Point2D.Double(x,y));
                    }
                    var head = new HeadInter(b,Shape.NOTEHEAD_BLACK,donor.getGrade(),staff,pitch);
                    head.setGlyph(SourceMissingChordHeads415.glyph(image,b,sheet));
                    head.setManual(true);
                    sig.addVertex(head);
                    chord.addMember(head);
                    var relation=HeadStemRelation.checkRelation(head,stem.getMedian(),stem.getGlyph(),
                        VerticalSide.TOP,sheet.getScale(),0);
                    require(relation!=null,"printed chord head does not connect to native stem");
                    relation.setManual(true);
                    sig.addEdge(head,stem,relation);
                    chord.invalidateCache();
                }
            } else if (f[0].equals("PITCH")) {
                var h=(HeadInter)node.apply(f[4]);
                var donor=(HeadInter)node.apply(f[5]);
                var b=box(f,6);
                require(h.getStaff()==staff && donor.getStaff()==staff && h.getShape()==Shape.NOTEHEAD_BLACK
                        && h.getBounds().intersects(b), "retuned head changed");
                require(Math.abs(staff.pitchPositionOf(new Point2D.Double(b.getCenterX(),b.getCenterY()))-donor.getPitch())<.6,
                        "retuned head outside source pitch band");
                stacks.add(h.getChord().getMeasure().getStack());
                if (mutate) {
                    h.setGlyph(SourceMissingChordHeads415.glyph(image,b,sheet));
                    h.setBounds(b);
                    h.setPitch(donor.getPitch());
                }
            } else if (f[0].equals("BEAM")) {
                var cids=f[4].split(",");
                var sids=f[5].split(",");
                require(cids.length==sids.length && cids.length>=2,"invalid beam stem count");
                var stems=new ArrayList<StemInter>();
                var chords=new ArrayList<HeadChordInter>();
                for (int i=0;i<cids.length;i++) {
                    var chord=(HeadChordInter)node.apply(cids[i]);
                    var stem=(StemInter)node.apply(sids[i]);
                    require(chord.getStaff()==staff && chord.getStem()==stem
                            && chord.getBeams().isEmpty() && chord.getFlagsNumber()==0, "beam source ownership changed");
                    stems.add(stem);
                    chords.add(chord);
                    stacks.add(chord.getMeasure().getStack());
                }
                require(chords.stream().allMatch(ch->ch.getMeasure()==chords.getFirst().getMeasure()),
                        "beam crosses unrelated bars");
                if (mutate) {
                    var median=new Line2D.Double(Double.parseDouble(f[6]),Double.parseDouble(f[7]),
                                                 Double.parseDouble(f[8]),Double.parseDouble(f[9]));
                    var beam=new BeamInter(0.0,median,Double.parseDouble(f[10]));
                    beam.setManual(true);
                    sig.addVertex(beam);
                    var group=new BeamGroupInter();
                    sig.addVertex(group);
                    group.addMember(beam);
                    for (int k=0;k<stems.size();k++) {
                        var stem=stems.get(k);
                        var rel=new BeamStemRelation();
                        var end=BeamStemRelation.computeExtensionPoint(beam,stem);
                        rel.setExtensionPoint(end);
                        rel.setBeamPortion(BeamStemRelation.computeBeamPortion(beam,stem.getCenter().x,sheet.getScale()));
                        rel.setManual(true);
                        sig.addEdge(beam,stem,rel);
                        chords.get(k).invalidateCache();
                    }
                    group.invalidateCache();
                    require(new HashSet<>(beam.getStems()).equals(new HashSet<>(stems)), "beam did not bind to every proven stem");
                }
            } else {
                var dot=(AugmentationDotInter)node.apply(f[4]);
                Point center=dot.getCenter();
                require(addedHeadBounds.stream().anyMatch(b->center.x>=b.x-Math.ceil(.35*b.width) && center.x<b.getMaxX() && center.y>=b.y && center.y<b.getMaxY()), "dot is not part of a proven recovered head");
                dotCandidates.add(dot);
            }
        }
        if (mutate) {
            for (var fake : falseHeads) {
                var chord=fake.getChord();
                require(chord.getNotes().size()>1,"cannot retire only native chord head");
                fake.remove();
                chord.invalidateCache();
            }
            for (var dot : dotCandidates) dot.remove();
        }
        for (var stack : stacks) {
            var expected=stack.getExpectedDuration();
            require(expected!=null,"printed meter unavailable");
            for (var measure : stack.getMeasures()) measure.resetRhythm();
            stack.resetRhythm();
            new StackRhythm(stack).process(expected);
            if (mutate) require(expected.equals(stack.getSlotsDuration()), "corrected stack duration differs from meter");
            stack.getSystem().getSheet().getStub().setModified(true);
        }
        book.setModified(true);
        book.store(out.resolve("refined.omr"),false);
        book.setExportPathSansExt(out.resolve("result"));
        require(book.export(book.getValidStubs(),book.getScores()), "native score export failed");
        SourceStructure335.writeLedgers(out);
        require(SourceCurvedRest400.sha(input).equals(a[1]) && SourceCurvedRest400.sha(plan).equals(a[4]), "source changed");
        System.out.println("SOURCE_PRIMARY_BEAMS_541 " + a[6] + " stacks=" + stacks.size());
        System.exit(0);
    }
}
