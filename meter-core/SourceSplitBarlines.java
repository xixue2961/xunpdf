package org.audiveris.omr.score;

import java.awt.Rectangle;
import java.awt.image.BufferedImage;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.*;
import javax.imageio.ImageIO;
import org.audiveris.omr.glyph.Shape;
import org.audiveris.omr.sheet.Book;
import org.audiveris.omr.sheet.PartBarline;
import org.audiveris.omr.sheet.Staff;
import org.audiveris.omr.sheet.rhythm.MeasureStack;
import org.audiveris.omr.sig.inter.StaffBarlineInter;
import org.audiveris.omr.sig.inter.AbstractBeamInter;
import org.audiveris.omr.sig.inter.HeadChordInter;
import org.audiveris.omr.sig.relation.BeamStemRelation;

public class SourceSplitBarlines {
    record Target(int page, int system, int stack, int x) {}
    record Proof(Rectangle bounds, String rasterHash) {}

    static void require(boolean condition, String message) {
        if (!condition) {
            throw new IllegalArgumentException(message);
        }
    }

    static String sha(Path file) throws Exception {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(file)));
    }

    static Set<Integer> chordIds(MeasureStack stack) {
        Set<Integer> ids = new HashSet<>();
        for (var measure : stack.getMeasures()) {
            measure.getHeadChords().forEach(chord -> ids.add(chord.getId()));
            measure.getRestChords().forEach(rest -> ids.add(rest.getId()));
        }
        return ids;
    }

    static boolean narrowPrintedStroke(BufferedImage image, Staff staff, Target target, Rectangle box) {
        double space = staff.getMeanInterline();
        if (box.x < 0 || box.y < 0 || box.getMaxX() > image.getWidth() || box.getMaxY() > image.getHeight()
                || box.width < 1 || box.width > .35 * space || Math.abs(box.getCenterX() - target.x()) > 3
                || Math.abs(box.y - staff.getFirstLine().yAt(target.x())) > 2
                || Math.abs(box.getMaxY() - staff.getLastLine().yAt(target.x())) > 3) {
            System.out.printf("box page=%d staff=%d %s spacing=%.1f top=%.1f bottom=%.1f%n", target.page(), staff.getId(), box, space, staff.getFirstLine().yAt(target.x()), staff.getLastLine().yAt(target.x()));
            return false;
        }
        int margin = Math.max(2, (int)Math.round(.04 * box.height));
        int height = box.height - 2 * margin;
        for (int x = box.x; x < box.getMaxX(); x++) {
            int dark = 0;
            for (int y = box.y + margin; y < box.getMaxY() - margin; y++) {
                int rgb = image.getRGB(x, y);
                int gray = (299 * ((rgb >> 16) & 255) + 587 * ((rgb >> 8) & 255) + 114 * (rgb & 255)) / 1000;
                if (image.getType() == BufferedImage.TYPE_BYTE_GRAY) {
                    gray = image.getRaster().getSample(x, y, 0);
                }
                if (gray < 155) {
                    dark++;
                }
            }
            if (dark < .94 * height) {
                System.out.printf("ink page=%d staff=%d x=%d dark=%d/%d type=%d bands=%d%n", target.page(), staff.getId(), x, dark, height, image.getType(), image.getRaster().getNumBands());
                return false;
            }
        }
        return true;
    }

    public static void main(String[] args) throws Exception {
        ((ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME)).setLevel(ch.qos.logback.classic.Level.WARN);
        Path input = Path.of(args[0]), rasters = Path.of(args[1]), plan = Path.of(args[2]), out = Path.of(args[3]);
        require(sha(input).equals(args[4]), "Native archive changed");
        Map<Target, Map<Integer, Proof>> targets = new LinkedHashMap<>();
        for (String row : Files.readAllLines(plan)) {
            String[] f = row.split("\\t");
            require(f.length == 10, "Bad boundary proof");
            int[] n = Arrays.stream(f).limit(9).mapToInt(Integer::parseInt).toArray();
            Target target = new Target(n[0], n[1], n[2], n[3]);
            Proof proof = new Proof(new Rectangle(n[5], n[6], n[7], n[8]), f[9]);
            require(targets.computeIfAbsent(target, ignored -> new LinkedHashMap<>()).put(n[4], proof) == null,
                    "Duplicate staff proof");
        }
        var book = Book.loadBook(input);
        Set<Page> changedPages = new LinkedHashSet<>();
        List<String> ledger = new ArrayList<>();
        ledger.add("unit\tsystem\tstack\tx\tstate\treason");
        for (var entry : targets.entrySet()) {
            Target found = entry.getKey();
            Map<Integer, Proof> proofs = entry.getValue();
            var system = book.getStub(found.page()).getSheet().getSystems().stream()
                    .filter(s -> s.getId() == found.system()).findFirst().orElseThrow();
            var stack = system.getStacks().stream().filter(s -> s.getIdValue() == found.stack()).findFirst().orElseThrow();
            String label = found.page()+"\t"+found.system()+"\t"+found.stack()+"\t"+found.x()+"\t";
            if (stack.getSlots() != null && !stack.getSlots().isEmpty()) {
                ledger.add(label + "unresolved\talready_rhythmic");
                continue;
            }
            Path raster = rasters.resolve(String.format("unit-%03d.png", found.page()));
            String hash = sha(raster);
            var image = ImageIO.read(raster.toFile());
            boolean complete = system.getStaves().size() == proofs.size() && system.getStaves().size() >= 3
                    && system.getStaves().stream().allMatch(staff -> {
                        Proof proof = proofs.get(staff.getId());
                        return proof != null && hash.equals(proof.rasterHash())
                                && narrowPrintedStroke(image, staff, found, proof.bounds());
                    });
            if (!complete) {
                ledger.add(label + "unresolved\tinsufficient_printed_strokes");
                continue;
            }
            Set<Integer> expected = chordIds(stack);
            if (expected.isEmpty()) {
                ledger.add(label + "unresolved\tno_musical_content");
                continue;
            }
            // Allow only a false beam attachment that terminates on the confirmed printed bar.
            Map<AbstractBeamInter, List<HeadChordInter>> beamChords = new HashMap<>();
            for (var measure : stack.getMeasures()) {
                for (var chord : measure.getHeadChords()) {
                    chord.getBeams().forEach(beam ->
                            beamChords.computeIfAbsent(beam, ignored -> new ArrayList<>()).add(chord));
                }
            }
            Set<BeamStemRelation> falseLinks = new LinkedHashSet<>();
            boolean actualBeamCrosses = false;
            for (var association : beamChords.entrySet()) {
                var beam = association.getKey();
                var chords = association.getValue();
                if (chords.stream().allMatch(c -> c.getCenter().x < found.x())
                        || chords.stream().noneMatch(c -> c.getCenter().x < found.x())) {
                    continue;
                }
                if (chords.size() != 2 || beam.getGrade() >= .35) {
                    actualBeamCrosses = true;
                    break;
                }
                BeamStemRelation barlineLink = null;
                for (var chord : chords) {
                    var stem = chord.getStem();
                    var staff = chord.getTopStaff();
                    if (stem == null || staff == null || stem.getStaff() != staff) {
                        continue;
                    }
                    var print = proofs.get(staff.getId()).bounds();
                    double space = staff.getMeanInterline();
                    var median = stem.getMedian();
                    var beamBounds = beam.getBounds();
                    boolean touchesPrintedStroke = Math.abs(median.getX1() - print.getCenterX()) <= 2
                            && Math.abs(median.getX2() - print.getCenterX()) <= 2
                            && Math.abs(median.getY2() - median.getY1()) >= 5 * space;
                    boolean endsAtBoundary = Math.min(Math.abs(beamBounds.getMinX() - found.x()),
                            Math.abs(beamBounds.getMaxX() - found.x())) <= .25 * space;
                    if (touchesPrintedStroke && endsAtBoundary) {
                        barlineLink = (BeamStemRelation) system.getSig().getRelation(beam, stem, BeamStemRelation.class);
                    }
                }
                if (barlineLink == null) {
                    actualBeamCrosses = true;
                    break;
                }
                falseLinks.add(barlineLink);
            }
            if (actualBeamCrosses) {
                ledger.add(label + "unresolved\tbeam_crosses_boundary");
                continue;
            }
            falseLinks.forEach(system.getSig()::removeEdge);
            List<PartBarline> partLines = new ArrayList<>();
            for (var part : system.getParts()) {
                PartBarline section = new PartBarline();
                for (Staff staff : part.getStaves()) {
                    var barline = new StaffBarlineInter(Shape.THIN_BARLINE, .8);
                    barline.setStaff(staff);
                    barline.setBounds(proofs.get(staff.getId()).bounds());
                    system.getSig().addVertex(barline);
                    section.addStaffBarline(barline);
                }
                partLines.add(section);
            }
            MeasureStack left = stack.splitAtBarline(partLines);
            Set<Integer> a = chordIds(left), b = chordIds(stack);
            require(!a.isEmpty() && !b.isEmpty() && Collections.disjoint(a, b), "Invalid physical note partition");
            a.addAll(b);
            require(a.equals(expected), "Split changed musical symbols");
            changedPages.add(system.getPage());
            ledger.add(label + "split\tprinted_all_staves;false_beam_links=" + falseLinks.size());
        }
        for (Page page : changedPages) {
            page.numberMeasures();
        }
        for (var stub : book.getStubs()) {
            if (stub.hasSheet()) {
                stub.setModified(true);
            }
        }
        book.setModified(true);
        Files.createDirectories(out);
        book.store(out.resolve("input.omr"), false);
        Files.write(out.resolve("splits.tsv"), ledger);
        require(sha(input).equals(args[4]), "Original archive modified");
        System.exit(0);
    }
}



