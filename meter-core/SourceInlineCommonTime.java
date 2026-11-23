package org.audiveris.omr.score;
import java.awt.Rectangle;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;
import org.audiveris.omr.glyph.Shape;
import org.audiveris.omr.sheet.Book;
import org.audiveris.omr.sheet.Staff;
import org.audiveris.omr.sig.inter.TimeWholeInter;

public class SourceInlineCommonTime {
    record Target(int page, int system, int stack) {}
    record Proof(Rectangle bounds, String rasterHash) {}

    public static void main(String[] args) throws Exception {
        ((ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME))
                .setLevel(ch.qos.logback.classic.Level.WARN);
        Path input = Path.of(args[0]), rasterDir = Path.of(args[1]), plan = Path.of(args[2]), output = Path.of(args[3]);
        String digest = SourceSplitBarlines.sha(input);
        SourceSplitBarlines.require(args[4].equals(digest), "Native input hash changed");
        Map<Target, Map<Integer, Proof>> groups = new LinkedHashMap<>();
        for (String line : Files.readAllLines(plan)) {
            var row = line.split("\\t");
            SourceSplitBarlines.require(row.length == 9, "Bad common-time proof");
            int[] v = Arrays.stream(row).limit(8).mapToInt(Integer::parseInt).toArray();
            var key = new Target(v[0], v[1], v[2]);
            var box = new Rectangle(v[4], v[5], v[6], v[7]);
            var last = groups.computeIfAbsent(key, ignored -> new LinkedHashMap<>()).put(v[3], new Proof(box, row[8]));
            SourceSplitBarlines.require(last == null, "Duplicate common-time staff proof");
        }
        var book = Book.loadBook(input);
        List<String> ledger = new ArrayList<>();
        ledger.add("unit\tsystem\tstack\tstate\tstaff_count");
        for (var group : groups.entrySet()) {
            Target key = group.getKey();
            var sheet = book.getStub(key.page()).getSheet();
            var system = sheet.getSystems().stream().filter(s -> s.getId() == key.system()).findFirst().orElseThrow();
            var stacks = system.getStacks();
            var stack = stacks.stream().filter(s -> s.getIdValue() == key.stack()).findFirst().orElseThrow();
            int index = stacks.indexOf(stack);
            var staves = system.getStaves();
            var proof = group.getValue();
            String rasterHash = SourceSplitBarlines.sha(rasterDir.resolve(String.format("unit-%03d.png", key.page())));
            var prior = index > 0 ? stacks.get(index - 1).getTimeSignature() : null;
            boolean confirmedTimeChange = prior != null && (prior.getNumerator() != 4 || prior.getDenominator() != 4)
                    && stack.getTimeSignature() == null && staves.size() == proof.size() && staves.size() >= 3;
            int count = 0;
            if (confirmedTimeChange && staves.stream().allMatch(staff -> matches(staff, proof.get(staff.getId()),
                    stack.getLeft(), rasterHash))) {
                for (Staff staff : staves) {
                    Rectangle box = proof.get(staff.getId()).bounds();
                    var whole = new TimeWholeInter(null, Shape.COMMON_TIME, .8);
                    whole.setStaff(staff);
                    whole.setBounds(box);
                    system.getSig().addVertex(whole);
                    stack.addInter(whole);
                    count++;
                }
                sheet.getStub().setModified(true);
            }
            ledger.add(key.page()+"\t"+key.system()+"\t"+key.stack()+"\t"+(count>0?"restored":"unresolved")+"\t"+count);
        }
        Files.createDirectories(output);
        book.setModified(true);
        book.store(output.resolve("input.omr"), false);
        Files.write(output.resolve("common-time.tsv"), ledger);
        SourceSplitBarlines.require(digest.equals(SourceSplitBarlines.sha(input)), "Original native file changed");
        System.exit(0);
    }

    private static boolean matches(Staff staff, Proof proof, int left, String rasterHash) {
        if (proof == null || !proof.rasterHash().equals(rasterHash)) {
            return false;
        }
        var box = proof.bounds();
        double space = staff.getMeanInterline();
        var middle = (staff.getFirstLine().yAt(box.x) + staff.getLastLine().yAt(box.x)) / 2.0;
        return left - .25 * space <= box.x && box.x <= left + 1.2 * space
                && Math.abs(box.getCenterY() - middle) <= .4 * space
                && box.width >= .9 * space && box.width <= 2.7 * space
                && box.height >= 1.5 * space && box.height <= 2.5 * space;
    }
}
