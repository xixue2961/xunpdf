"""对重要音乐结构不被改写、页码边界和审查器做回归。"""
import unittest
import xml.etree.ElementTree as ET
from xunpdf import audit, music_fingerprint, normalize_export, parse_pages, safe_cut, prepare_pdf, apply_confirmed_start_meter


def score():
    return ET.fromstring('''<score-partwise version="4.0"><part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list><part id="P1">
    <measure number="1"><attributes><divisions>12</divisions><time><beats>3</beats><beat-type>4</beat-type></time></attributes>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>12</duration><voice>1</voice><type>quarter</type><lyric default-x="6" default-y="-40"><syllabic>begin</syllabic><text>hel</text></lyric></note>
    <note><pitch><step>D</step><octave>4</octave></pitch><duration>24</duration><voice>1</voice><type>half</type></note></measure>
    <measure number="2"><attributes><time><beats>6</beats><beat-type>8</beat-type></time></attributes>
    <note><pitch><step>E</step><octave>4</octave></pitch><duration>48</duration><voice>1</voice><type>whole</type><notations><slur type="start" number="1"/></notations></note></measure>
    </part></score-partwise>''')


class CoreTests(unittest.TestCase):
    def test_page_ranges(self):
        self.assertEqual(parse_pages("1-2,4", 5), [1,2,4])
        for invalid in ("0", "2-1", "6", "1,1", "3,1", "abc", "1.5"):
            with self.assertRaises(ValueError):
                parse_pages(invalid, 5)

    def test_preserves_rhythm_notes_slurs(self):
        raw = score()
        out = normalize_export(raw)
        self.assertEqual(len(out.findall(".//note")), 3)
        self.assertEqual(out.findtext(".//measure[@number='2']/note/duration"), "48")
        self.assertIsNotNone(out.find(".//slur"))
        self.assertEqual(out.find(".//lyric").attrib, {})
        self.assertEqual(raw.find(".//lyric").get("default-x"), "6")
        self.assertEqual(out.findtext(".//lyric/syllabic"), "begin")

    def test_audit_does_not_pad_or_trim(self):
        raw = score()
        before = music_fingerprint(raw)
        report = audit(raw)
        self.assertTrue(any(i["kind"] == "小节超长" and i["measure"] == "2" for i in report["issues"]))
        self.assertEqual(report["time_signatures"][-1]["time"], "6/8")
        self.assertEqual(before, music_fingerprint(raw))

    def test_unknown_and_additive_meter(self):
        raw = score()
        raw.find(".//time/beats").text = "2+3"
        self.assertEqual(audit(raw)["time_signatures"][0]["time"], "2+3/4")
        raw.find(".//attributes").remove(raw.find(".//time"))
        self.assertTrue(any(i["kind"] == "拍号未知" for i in audit(raw)["issues"]))

    def test_confirmed_meter_not_override_changes(self):
        raw = score()
        self.assertEqual(apply_confirmed_start_meter(raw, '4/4'),0)
        attrs=raw.find('.//attributes')
        attrs.remove(attrs.find('time'))
        self.assertEqual(apply_confirmed_start_meter(raw,'3/4'),1)
        self.assertEqual(raw.findtext('.//measure[@number="2"]/attributes/time/beats'),'6')
        self.assertEqual(raw.findtext('.//measure[@number="1"]/attributes/time/beats'),'3')

    def test_cut_does_not_cross_ink(self):
        from PIL import Image, ImageDraw
        im = Image.new("L", (500,1000), 255)
        draw = ImageDraw.Draw(im)
        draw.rectangle((30,100,470,450), fill=0)
        draw.rectangle((30,550,470,900), fill=0)
        self.assertTrue(450 < safe_cut(im) < 550)
        draw.line((5,0,5,999), fill=0, width=3)
        self.assertIsNone(safe_cut(im))

    def test_pdf_two_frames_after_png(self):
        import tempfile
        from pathlib import Path
        import pypdfium2 as p
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            image = Image.new('RGB', (500,1000), 'white')
            draw = ImageDraw.Draw(image)
            draw.rectangle((20,100,480,430), fill='black')
            draw.rectangle((20,570,480,900), fill='black')
            source = folder/'fixture.pdf'
            image.save(source, 'PDF')
            mapping = prepare_pdf(source, folder, emit=lambda s: None)
            self.assertEqual(len(mapping), 2)
            self.assertEqual(mapping[0]['source_mode'], 'native_image')
            with Image.open(folder/'input.tif') as tif:
                self.assertEqual(tif.n_frames, 2)
            # 两块必须完整覆盖原图，不裁标题、歌词或页边。
            self.assertEqual(mapping[0]['crop'][1], 0)
            self.assertEqual(mapping[0]['crop'][3], mapping[1]['crop'][1])
            self.assertEqual(mapping[1]['crop'][3], mapping[1]['original_size'][1])


if __name__ == "__main__":
    unittest.main()
