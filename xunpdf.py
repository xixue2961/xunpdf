"""XUNpdf识谱工具：保留原始识别内容的本地MusicXML检查原型。"""

from __future__ import annotations



import argparse

import copy

import hashlib

import json

import os

from pathlib import Path

from score_structure import SINGLE_STAFF_HINT, SINGLE_STAFF_PROFILE

import re

import subprocess

import sys

import threading

import time

import urllib.request

import uuid

import xml.etree.ElementTree as ET

import zipfile

from collections import Counter, defaultdict

from fractions import Fraction



ROOT = Path(__file__).resolve().parent

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):

    sys.stdout.reconfigure(encoding="utf-8")

CONFIG = ROOT / "settings.json"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)





def read_settings(*, resolve_paths=True):
    path = CONFIG if CONFIG.is_file() else ROOT / "settings.example.json"
    settings = json.loads(path.read_text(encoding="utf-8-sig"))
    if resolve_paths:
        for key in ("engine_root", "jobs_root", "piano_core_cache_root", "output_dir"):
            if key in settings:
                settings[key] = str(ROOT / Path(settings[key]).expanduser())
    return settings





def save_json(path, value):

    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")





def parse_pages(text, count):

    """页码明确且有序；不接受重复页、倒序或越界。"""

    if not text.strip():

        return list(range(1, count + 1))

    pages = []

    for token in re.split(r"[,，\s]+", text.strip()):

        if not token:

            continue

        if not re.fullmatch(r"\d+(?:-\d+)?", token):

            raise ValueError("页码格式应为 1-3,5 或留空处理全部页。")

        ends = [int(v) for v in token.split("-")]

        a, b = ends[0], ends[-1]

        if not 1 <= a <= b <= count:

            raise ValueError(f"页码超出范围或倒序：{token}（共{count}页）")

        pages.extend(range(a, b + 1))

    if pages != sorted(set(pages)):

        raise ValueError("请按从小到大填写页码，不重复选择同一页。")

    return pages





def safe_cut(image):

    """只在整行几乎无墨迹的谱组间切分；不做标题裁切或擦除。"""

    import numpy as np

    gray = np.asarray(image.convert("L"))

    ink = gray < 220

    h, w = ink.shape

    # 不忽略左右边缘：括线和跨系统线也是不能切开的内容。

    blank = ink.sum(axis=1) <= max(1, w // 2000)

    minimum = max(10, round(h * .008))

    runs, start = [], None

    lo, hi = int(h * .30), int(h * .70)

    for y in range(lo, hi + 1):

        if y < hi and blank[y]:

            if start is None:

                start = y

        elif start is not None:

            if y - start >= minimum:

                runs.append((start, y))

            start = None

    if not runs:

        return None

    a, b = max(runs, key=lambda r: (r[1]-r[0], -abs((r[0]+r[1])/2-h/2)))

    mid = (a + b) // 2

    # A split must separate two content regions, never create an empty sheet.

    # Keeping the full page preserves faint marks, footers and all source pixels.

    if not ink[:mid].any() or not ink[mid:].any():

        # Pure white regions are recorded and omitted by preparation below.

        # Faint non-white content is retained by keeping the page whole.

        if gray[:mid].min() != 255 and gray[mid:].min() != 255:

            return None

    return mid





def prepare_pdf(source, job, pages_text="", split=True, sharpen=False, emit=print, cut_y=None):

    import pypdfium2 as pdfium

    from PIL import Image, ImageFilter

    source = Path(source).resolve()

    doc = pdfium.PdfDocument(source)

    frames, mapping, blank_regions = [], [], []

    try:

        pages = parse_pages(pages_text, len(doc))

        if cut_y is not None and len(pages) != 1:

            raise ValueError("手动切分坐标仅用于单页，并须事先检查原图。")

        for number in pages:

            emit(f"准备原谱第{number}页")

            page = doc[number-1]

            objects = list(page.get_objects())

            embedded = None

            page_size = page.get_size()

            # 仅单张、轴向、覆盖绝大多数页的原图可直接提取。

            # 有文字/矢量/多图/旋转时渲染全页，不丢掉这些内容。

            if len(objects) == 1 and isinstance(objects[0], pdfium.PdfImage):

                obj = objects[0]

                x0, y0, x1, y1 = obj.get_pos()

                matrix = obj.get_matrix()

                if ((x1-x0)*(y1-y0) >= .85*page_size[0]*page_size[1]

                        and abs(matrix.b) < .001 and abs(matrix.c) < .001

                        and matrix.a > 0 and matrix.d > 0 and page.get_rotation() == 0):

                    embedded = [x0, y0, x1, y1]

            from source_page_overlays import suppress_page_overlays

            removed_overlays = suppress_page_overlays(page) if embedded is None else []
            if removed_overlays:
                emit(f"第{number}页识别图已忽略 {len(removed_overlays)} 个独立水印或页脚对象")

            bitmap = objects[0].get_bitmap() if embedded else page.render(scale=2)

            native = bitmap.to_pil().copy()

            if native.mode == "RGBA":

                white = Image.new("RGBA", native.size, "white")

                native = Image.alpha_composite(white, native)

            original = native.convert("L")

            bitmap.close()

            page.close()

            original.save(job / f"original-{number:03d}.png")

            mid = safe_cut(original) if split else None

            if cut_y is not None:

                if not 0 < cut_y < original.height:

                    raise ValueError("手动切分坐标超出原始图像")

                mid = cut_y

            boxes = [(0, 0, original.width, original.height)] if mid is None else [

                (0, 0, original.width, mid), (0, mid, original.width, original.height)]

            for index, box in enumerate(boxes):

                crop = original.crop(box)

                if crop.getextrema() == (255, 255):

                    blank_regions.append(dict(pdf_page=number, crop=list(box), original_size=list(original.size), original_sha256=hashlib.sha256((job/f"original-{number:03d}.png").read_bytes()).hexdigest(), reason="all_pixels_strict_white_255"))

                    emit(f"原谱第{number}页的纯白区域已保留在原图中，不作为识别图块")

                    continue

                scale = min(2., (19_500_000 / (crop.width*crop.height)) ** .5)

                out = crop.resize((int(crop.width*scale), int(crop.height*scale)), Image.Resampling.LANCZOS)

                if sharpen:

                    out = out.filter(ImageFilter.UnsharpMask(radius=1, percent=70, threshold=3))

                unit = len(frames)+1

                out.save(job / f"unit-{unit:03d}.png")

                frames.append(out)

                mapping.append(dict(unit=unit, pdf_page=number, half=index+1, crop=list(box),

                                    original_size=list(original.size), scale=scale,

                                    size=list(out.size), sharpen=sharpen,

                                    split_method="explicit_reviewed_cut" if cut_y is not None else "blank_gap" if mid else "whole",

                                    source_mode="native_image" if embedded else "page_render",

                                    pdf_image_bounds=embedded, pdf_page_size=list(page_size)))

        # 同一book保持跨块/跨页拍号及声部上下文，不逐块独立拼接。

        # PNG保存会留下encoderconfig；TIFF追加帧时不会自动重置该属性。

        # 显式清理每帧编码参数，避免PNG参数误传给TIFF编码器。

        save_json(job / "blank-regions.json", blank_regions)

        if not frames:

            raise ValueError("所选区域均为纯白，未启动识别；原图已保留。")

        for frame in frames:

            frame.encoderconfig = ()

        frames[0].save(job / "input.tif", save_all=True, append_images=frames[1:], compression="raw")

        save_json(job / "source-map.json", mapping)

        return mapping

    finally:

        for im in frames:

            im.close()

        doc.close()





def read_score(path):

    path = Path(path)

    if path.suffix.lower() == ".mxl":

        with zipfile.ZipFile(path) as archive:

            container = ET.fromstring(archive.read("META-INF/container.xml"))

            entry = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))

            return ET.fromstring(archive.read(entry))

    return ET.parse(path).getroot()





def music_fingerprint(root):

    """导出前后逐元素验证：音符、休止、声部游标和音乐结构不能悄悄改变。"""

    return [(p.get("id"), m.get("number"), [ET.tostring(e, encoding="unicode")

            for e in m if e.tag in ("note", "backup", "forward", "attributes", "barline")])

            for p in root.findall("part") for m in p.findall("measure")]





def normalize_export(root, lyrics=True, clean_positions=True):

    result = copy.deepcopy(root)

    # 比较时只忽略用户选择的歌词内容/排版字段，其他音乐结构逐元素不变。

    def clear_lyrics(tree):

        for note in tree.iter("note"):

            for lyric in list(note.findall("lyric")):

                if not lyrics:

                    note.remove(lyric)

                elif clean_positions:

                    for item in lyric.iter():

                        for key in ("default-x", "default-y", "relative-x", "relative-y"):

                            item.attrib.pop(key, None)

    clear_lyrics(result)

    expected = copy.deepcopy(root)

    clear_lyrics(expected)

    assert music_fingerprint(expected) == music_fingerprint(result)

    if result.get("version") == "4.0.3":
        result.set("version", "4.0")

    ident = result.find("identification")

    if ident is None:

        ident = ET.Element("identification")

        index = next((i for i, e in enumerate(result) if e.tag in ("defaults", "credit", "part-list")), len(result))

        result.insert(index, ident)

    for source in ident.findall("source"):

        source.text = "用户本地原谱"

    encoding = ident.find("encoding")

    if encoding is not None:

        ET.SubElement(encoding, "software").text = "XUNpdf 0.1 / Audiveris-Bandscore (待音乐核对)"

    if clean_positions:
        from source_layout_credits import normalize_source_layout_credits
        normalize_source_layout_credits(result)
    return result





def apply_confirmed_start_meter(root, meter):

    """仅把用户显式确认的起始拍号写入缺失的首小节，不触碰后续变拍。"""

    if not meter:

        return 0

    beats, denominator = meter.split("/")

    if int(beats) <= 0 or int(denominator) not in (1, 2, 4, 8, 16, 32, 64, 128):

        raise ValueError("拍号分子须为正数，分母须为常用二进制音符单位")

    count = 0

    for part in root.findall("part"):

        first = part.find("measure")

        if first is None:

            continue

        attrs = first.find("attributes")

        if attrs is None:

            attrs = ET.Element("attributes")

            first.insert(0, attrs)

        if attrs.find("time") is not None:

            continue

        t = ET.Element("time")

        ET.SubElement(t, "beats").text = beats

        ET.SubElement(t, "beat-type").text = denominator

        index = next((i for i, e in enumerate(attrs) if e.tag not in ("footnote", "level", "divisions", "key")), len(attrs))

        attrs.insert(index, t)

        count += 1

    return count





def audit(root):

    """结构疑点检查，不把无警告当作准确率，不修补音乐。"""

    from piano_checks import continuous_cross_staff_voices

    names = {p.get("id"): p.findtext("part-name", "") for p in root.findall("part-list/score-part")}

    issues, changes, counts, all_meters = [], [], Counter(), defaultdict(dict)

    sizes = {}

    def flag(kind, part, measure, detail, level="核对"):

        issues.append(dict(kind=kind, part=names.get(part, part), part_id=part,

                           measure=measure, detail=detail, level=level))

    for part in root.findall("part"):

        pid = part.get("id")

        sizes[pid] = len(part.findall("measure"))

        divisions, meter, signature = 1, None, None

        ties = Counter()
        staff_count = 1

        for measure in part.findall("measure"):

            num = measure.get("number")

            for attrs in measure.findall("attributes"):
                if attrs.find("staves") is not None:
                    staff_count = int(attrs.findtext("staves"))

                if attrs.find("divisions") is not None:

                    divisions = int(attrs.findtext("divisions"))

                    if divisions <= 0:

                        raise ValueError(f"{pid}小节{num}无效时值单位{divisions}")

                t = attrs.find("time")

                if t is not None:

                    if t.find("senza-misura") is not None:

                        meter, signature = None, "自由节奏"

                    else:

                        beats, types = t.findall("beats"), t.findall("beat-type")

                        if len(beats) != len(types) or not beats:

                            raise ValueError(f"{pid}小节{num}拍号结构不完整")

                        meter = sum((Fraction(sum(map(int, b.text.split("+")))*4, int(d.text))

                                     for b, d in zip(beats, types)), Fraction())

                        signature = "+".join(f"{b.text}/{d.text}" for b, d in zip(beats, types))

                    if not changes or changes[-1] != dict(part=pid, measure=num, time=signature):

                        changes.append(dict(part=pid, measure=num, time=signature))

            all_meters[num][pid] = signature

            if signature is None:

                flag("拍号未知", pid, num, "没有可继承的拍号；未擅自假设4/4")

            if meter is None and signature == "自由节奏":

                flag("自由节奏", pid, num, "保留原时值，不检查是否凑满拍数", "提示")

            cursor, last_start = Fraction(), Fraction()

            ends = defaultdict(Fraction)
            timelines = defaultdict(list)

            syllables = set()

            verse_counts = defaultdict(Counter)

            printed_dynamics = {e.tag.lower() for d in measure.findall('direction/direction-type/dynamics') for e in d}

            for item in measure:

                if item.tag in ("backup", "forward"):

                    delta = Fraction(int(item.findtext("duration", "0")), divisions)

                    cursor += delta if item.tag == "forward" else -delta

                    if cursor < 0:

                        flag("时间游标越界", pid, num, f"游标为{cursor}拍")

                elif item.tag == "note":

                    counts["notes"] += 1

                    counts["pitched"] += item.find("pitch") is not None

                    counts["rests"] += item.find("rest") is not None

                    if item.find("grace") is not None:

                        continue

                    duration = Fraction(int(item.findtext("duration", "0")), divisions)

                    chord = item.find("chord") is not None

                    start = last_start if chord else cursor

                    if not chord:

                        last_start = start

                        cursor += duration

                    voice = (item.findtext("staff", "1"), item.findtext("voice", "1"))

                    ends[voice] = max(ends[voice], start+duration)
                    if staff_count > 1 and not chord:
                        timelines[voice[1]].append((start, start + duration, voice[0], item.find("pitch") is not None))

                    if duration <= 0:

                        flag("时值异常", pid, num, f"声部{voice}时值{duration}")

                    for lyric in item.findall("lyric"):

                        counts["lyrics"] += 1

                        text = lyric.findtext('text', '')

                        verse_counts[voice][lyric.get('number', '1')] += 1

                        hanzi = [c for c in text if '\u3400' <= c <= '\u9fff']

                        if len(hanzi) > 1:

                            flag('多汉字挂单音候选', pid, num, f'{text}；需对原谱核音节分配，不自动拆字')

                        if text.strip().lower() in printed_dynamics:

                            flag('疑似力度混入歌词', pid, num, f'{text}；同小节也有对应力度，须对原谱区分')

                        if any(c in text for c in ('□', '�')):

                            flag('歌词未识别字形', pid, num, text)

                        token = (voice, start, lyric.get("number", "1"))

                        if token in syllables:

                            flag("歌词重叠候选", pid, num, f"同声部同起音重复歌词：{lyric.findtext('text', '')}")

                        syllables.add(token)

                        if item.find("rest") is not None:

                            flag("歌词挂到休止符", pid, num, lyric.findtext("text", ""))

                        if any(t.get("type") == "stop" for t in item.findall("tie")):

                            flag("延音续音带歌词", pid, num, lyric.findtext("text", ""))

                        if len(lyric.findtext("text", "")) > 18:

                            flag("长歌词候选", pid, num, lyric.findtext("text", ""))

                    pitch = ET.tostring(item.find("pitch")) if item.find("pitch") is not None else b"unpitched"

                    key = (voice, pitch)

                    types = {t.get("type") for t in item.findall("tie")}

                    if "stop" in types:

                        if ties[key]:

                            ties[key] -= 1

                        else:

                            flag("延音线缺起点", pid, num, f"声部{voice}")

                    if "start" in types:

                        ties[key] += 1

            for lyric_voice, verses in verse_counts.items():

                if len(verses) > 1 and min(verses.values()) == 1 and max(verses.values()) >= 3:

                    flag('歌词段号碎片候选', pid, num, f'声部{lyric_voice}各段音节数{dict(verses)}；可能是真实多段，须对原谱确认')

            if meter is not None:
                complete = continuous_cross_staff_voices(timelines, meter)
                for voice, end in ends.items():

                    if end > meter:

                        flag("小节超长", pid, num, f"谱表/声部{voice}末端{end}拍，拍号容量{meter}拍；未截音")

                    elif end < meter and voice[1] not in complete and measure.get("implicit") != "yes":

                        flag("声部不足候选", pid, num, f"谱表/声部{voice}末端{end}拍，容量{meter}拍；可能是分部/弱起/漏识，未补休止", "提示")

        if any(ties.values()):

            flag("延音线缺终点", pid, "末尾", f"{sum(ties.values())}条；选页片段边界也可能造成")

    if len(set(sizes.values())) > 1:

        flag("声部小节数不一致", "整谱", "全部", str(sizes))

    for num, meters in all_meters.items():

        if len(set(meters.values())) > 1:

            flag("声部拍号不一致", "整谱", num, str(meters))

    tempo = len(root.findall(".//sound[@tempo]"))

    if tempo == 0:

        flag("无播放速度", "整谱", "全部", "未识别出可播放速度；不填猜测BPM", "提示")

    return dict(status="待原谱音乐核对", counts=dict(counts), parts=names, measure_counts=sizes,

                time_signatures=changes, playable_tempos=tempo, issues=issues,

                limits=["结构检查不能发现所有漏音、错音、歌词错位", "无警告不代表识谱正确", "选页可造成跨边界连线缺失"])





def environment(settings):

    engine = Path(settings["engine_root"])

    env = os.environ.copy()

    env.update(PYTHONUTF8="1", OMP_NUM_THREADS="4", MKL_NUM_THREADS="4",

               PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True", PADDLE_PDX_MODEL_SOURCE="bos",

               PADDLE_PDX_CACHE_HOME=str(engine / "cache/paddlex"),

               HF_HOME=str(engine / "cache/huggingface"), MODELSCOPE_CACHE=str(engine / "cache/modelscope"),

               PADDLE_HOME=str(engine / "cache/paddle"))

    return env





def run_process(args, log_path, env, cancel, timeout=1800, emit=print):

    with open(log_path, "ab") as log:
        start_offset = log.tell()

        proc = subprocess.Popen(list(map(str, args)), stdout=log, stderr=log, stdin=subprocess.DEVNULL,

                                env=env, creationflags=NO_WINDOW)

        start = last = time.monotonic()

        try:

            while proc.poll() is None:

                if cancel.is_set():

                    raise InterruptedError("任务已停止，中间结果保留。")

                if time.monotonic()-start > timeout:

                    raise TimeoutError("识别超时，中间结果与日志已保留。")

                if time.monotonic()-last >= 25:

                    emit(f"识别处理中，已运行{int(time.monotonic()-start)}秒；日志：{log_path.name}")

                    last = time.monotonic()

                cancel.wait(.3)

            if proc.returncode:
                from runtime_resources import MemoryBudgetError, explain_failure
                log.flush()
                with Path(log_path).open("rb") as failed_run:
                    failed_run.seek(start_offset)
                    text = failed_run.read().decode("utf8", errors="replace")
                detail = explain_failure(text, proc.returncode)
                if detail:
                    raise MemoryBudgetError(detail + str(log_path))
                raise RuntimeError(f"识别进程返回{proc.returncode}，请查看{log_path}")

        finally:

            if proc.poll() is None:

                proc.terminate()  # 只结束本次启动的子进程，不扫描或停止其他程序。

                try:

                    proc.wait(timeout=10)

                except subprocess.TimeoutExpired:

                    proc.kill()

                    proc.wait()





def ensure_ocr(settings, job, device, language, cancel, emit):

    engine = Path(settings["engine_root"])

    variants = ["gpu", "cpu"] if device == "auto" else [device]

    for variant in variants:

        python = engine / ("ocr-gpu-env" if variant == "gpu" else "ocr-env") / "Scripts/python.exe"

        if not python.exists():

            if device != "auto":

                raise FileNotFoundError(f"缺少{variant}文字识别环境")

            continue

        import socket

        with socket.socket() as sock:

            sock.bind(("127.0.0.1", 0))

            port = sock.getsockname()[1]

        url = f"http://127.0.0.1:{port}"

        rec = "en_PP-OCRv5_mobile_rec" if language == "en" else "PP-OCRv5_server_rec"

        for model in ("PP-OCRv5_mobile_det", rec, "PP-OCRv5_server_rec"):

            if not (engine / "cache/paddlex/official_models" / model).is_dir():

                raise FileNotFoundError(f"本地模型缺失：{model}，请先完成安装准备")

        emit(f"启动{variant.upper()}本地文字识别；音符内核仍用CPU")

        log = open(job / f"ocr-{variant}.log", "ab")

        proc = subprocess.Popen([str(python), "-u", str(ROOT / "ocr/paddle_ocr_server.py"),

                                 "--port", str(port), "--device", "gpu:0" if variant == "gpu" else "cpu",

                                 "--rec", rec], env=environment(settings), stdout=log, stderr=log,

                                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)

        log.close()

        # Loopback health checks must bypass machine HTTP proxy settings.
        import http.client
        health_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        ready = False

        try:

            deadline = time.monotonic()+180

            last = time.monotonic()

            while proc.poll() is None and time.monotonic() < deadline:

                if cancel.is_set():

                    raise InterruptedError("启动已取消")

                try:

                    with health_opener.open(url+"/health", timeout=1) as response:

                        ready = response.read().startswith(b"ok ")

                    if ready:

                        return proc, url, variant

                except (OSError, http.client.HTTPException):

                    pass

                if time.monotonic()-last >= 25:

                    emit(f"仍在加载{variant.upper()}模型；首次启动可能较慢")

                    last = time.monotonic()

                cancel.wait(.5)

        finally:

            if not ready:

                stop_owned(proc)

        if device != "auto":

            raise RuntimeError(f"{variant} OCR启动失败，请查看日志；没有输出无文字的假成功结果")

        emit(f"{variant}不可用，尝试CPU")

    raise RuntimeError("没有可用的本地OCR环境")





def stop_owned(proc):

    if proc is not None and proc.poll() is None:

        proc.terminate()

        try:

            proc.wait(timeout=10)

        except subprocess.TimeoutExpired:

            proc.kill()

            proc.wait()





def ensure_initial_ocr(settings, job, device, language, cancel, emit):
    """Give the music engine its usual memory budget before loading it."""
    from runtime_resources import check_memory, MemoryBudgetError, wait_for_released_memory

    requested = device
    while True:
        proc, url, actual = ensure_ocr(settings, job, requested, language, cancel, emit)
        handed_off = False
        try:
            check_memory(job, "after_ocr_start")
            handed_off = True
            return proc, url, actual
        except MemoryBudgetError:
            cpu = Path(settings["engine_root"]) / "ocr-env/Scripts/python.exe"
            if requested != "auto" or actual != "gpu" or not cpu.is_file():
                raise
        finally:
            if not handed_off:
                stop_owned(proc)

        emit("GPU 文字识别启动后，留给乐谱识别的内存不足。已释放本任务 GPU 服务，尝试本地 CPU 文字识别。")
        wait_for_released_memory(job, cancel, timeout=8, phase="before_ocr_cpu_fallback")
        requested = "cpu"


def process(source, pages="", profile="piano", device="auto", language="en", split=True,

            sharpen=False, lyrics=True, parts_hint="", initial_meter="", emit=print, cancel=None,

            cut_y=None, threshold=None, output_dir=None, resume=False, resume_manifest=None):

    if profile == SINGLE_STAFF_PROFILE:
        lyrics = False
        parts_hint = SINGLE_STAFF_HINT

    settings = read_settings()

    if output_dir is not None:

        settings["output_dir"] = str(Path(output_dir).expanduser().resolve())

    cancel = cancel or threading.Event()

    source = Path(source).resolve()

    if source.suffix.lower() != ".pdf" or not source.is_file():

        raise ValueError("请选择存在的PDF文件")

    jobs_root = Path(settings.get("jobs_root", str(ROOT / "jobs")))
    if not jobs_root.is_absolute():
        jobs_root = ROOT / jobs_root
    options = dict(pages=pages, profile=profile, device=device, language=language, split=split,
                   sharpen=sharpen, lyrics=lyrics, parts_hint=parts_hint, initial_meter=initial_meter,
                   cut_y=cut_y, threshold=threshold)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    checkpoint = None
    if resume or resume_manifest is not None:
        from recognition_checkpoint import find_symbols
        checkpoint = (
            find_symbols(jobs_root, digest, options, preferred_manifest=resume_manifest)
            if resume_manifest is not None else find_symbols(jobs_root, digest, options)
        )
        if checkpoint is None:
            if resume_manifest is not None:
                raise ValueError("所选任务及其前序任务没有匹配且完整的符号识别断点。")
            emit("未找到匹配且完整的识别断点，本次从原谱开始。")

    job = jobs_root / (time.strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6])
    job.mkdir(parents=True)
    manifest = dict(state="准备中", input_name=source.name, source_path=str(source),
                    sha256=digest, options=options)
    if checkpoint is not None:
        manifest["resumed_from"] = checkpoint[0].name
    save_json(job / "manifest.json", manifest)

    proc = None
    from runtime_resources import MemoryBudgetError, check_memory
    from recognition_checkpoint import restore_symbols, save_symbols

    try:
        check_memory(job, "before_pdf")
        if checkpoint is None:
            mapping = prepare_pdf(source, job, pages, split, sharpen, emit, cut_y=cut_y)
        else:
            mapping = restore_symbols(checkpoint, job)
            emit(f"已恢复 {checkpoint[0].name} 的符号识别结果，继续小节、节奏和歌词复核。")

        if cancel.is_set():

            raise InterruptedError("已停止")

        if checkpoint is None:
            proc, url, actual = ensure_initial_ocr(settings, job, device, language, cancel, emit)
        else:
            url, actual = "http://127.0.0.1:1", checkpoint[1]["device"]
        engine = Path(settings["engine_root"])

        java = engine / settings["java_relative"] / "bin/java.exe"

        classpath = str(engine / "Audiveris-Bandscore/lib/*")

        if settings.get("engine_classpath_prefix"):

            prefix = Path(settings["engine_classpath_prefix"])

            classpath = str(prefix if prefix.is_absolute() else ROOT / prefix) + os.pathsep + classpath

        args = [java, "-Xms128m", "-Xmx1536m", "-XX:ActiveProcessorCount=4", "--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED",

                "--enable-native-access=ALL-UNNAMED", "-cp", classpath, "Audiveris",

                "-batch", "-step", "SYMBOLS", "-save", "-swap", "-output", job / "raw"]

        constants = {"org.audiveris.omr.text.paddle.PaddleOCR.serverUrl": url,

                     "org.audiveris.omr.sheet.ProcessingSwitches.lyrics": str(lyrics and profile != "piano").lower()}

        if threshold is not None:

            if not 1 <= threshold <= 254:

                raise ValueError("二值化阈值必须为1到254")

            constants["org.audiveris.omr.image.FilterDescriptor.defaultKind"] = "GLOBAL"

            constants["org.audiveris.omr.image.GlobalDescriptor.defaultThreshold"] = str(threshold)

        if profile == "piano" and not parts_hint.strip():

            parts_hint = "Piano:2"

        if parts_hint.strip():

            constants["org.audiveris.omr.score.PartCollation.partsHint"] = parts_hint.strip()

        if initial_meter.strip():

            if not re.fullmatch(r"\d+/\d+", initial_meter.strip()):

                raise ValueError("起始拍号格式应为4/4；未知请留空，不强设4/4")

            constants["org.audiveris.omr.sheet.rhythm.PageRhythm.defaultTimeSignature"] = initial_meter.strip()

        for key, value in constants.items():

            args.extend(["-constant", f"{key}={value}"])

        args.extend(["--", job / "input.tif"])

        manifest.update(state="识别中", actual_device=actual, units=len(mapping), constants=constants)

        save_json(job / "manifest.json", manifest)

        if checkpoint is None:
            emit(f"识别{len(mapping)}个图块，同一整谱保留跨页上下文")

            run_process(args, job / "engine.log", environment(settings), cancel, emit=emit)

            log = (job / "engine.log").read_text(encoding="utf-8", errors="replace")

            if re.search(r"PaddleOCR (?:server error|recognition failed|server not reachable)|Too large image", log):

                raise RuntimeError("识别日志包含OCR失败或图像超限，结果只保留在工程，不导出假成功XML")

        save_symbols(job, actual)

        # Preserve symbols before native loading/linking can discard real dots.
        stop_owned(proc)
        proc = None
        from runtime_resources import wait_for_released_memory
        wait_for_released_memory(job, cancel, emit)
        emit("符号识别完成，已释放本任务文字识别服务内存")
        from source_native_preflight import complete_native_source, complete_upstream_source
        if settings.get("engine_classpath_prefix"):
            native_mxl, native_source, native_preflight = complete_native_source(
                job, settings, actual, environment(settings), cancel, run_process, args, emit)
        else:
            native_mxl, native_source, native_preflight = complete_upstream_source(
                job, environment(settings), cancel, run_process, args, emit)
        manifest["source_native_preflight_state"] = native_preflight["state"]
        log = (job / "engine.log").read_text(encoding="utf-8", errors="replace")
        raw = read_score(native_mxl)
        if not raw.findall("part/measure/note"):
            raise RuntimeError("没有识别到音符；请检查原谱与日志")

        save_json(job / "audit-raw.json", audit(raw))
        from source_meter_context import refine_source_meter
        raw, context_source, source_meter = refine_source_meter(raw, job, settings, actual, environment(settings), cancel, run_process, emit, source_job=native_source)
        manifest["source_meter_context_state"] = source_meter["state"]

        from score_structure import validate_preset

        validate_preset(raw, parts_hint)

        from source_context import refine_source_clefs
        raw, source_context = refine_source_clefs(raw, job, settings, actual, parts_hint, environment(settings), cancel, run_process, emit, source_job=context_source)
        manifest["source_context_state"] = source_context["state"]
        from source_tempo_context import refine_source_tempos, refine_word_tempos
        raw, source_tempo = refine_source_tempos(raw, job, settings, actual, parts_hint, environment(settings), cancel, run_process, emit, source_job=context_source)
        manifest["source_tempo_state"] = source_tempo["state"]
        from source_symbol_context import refine_source_symbols
        raw, source_symbols = refine_source_symbols(raw, job, settings, actual, parts_hint, environment(settings), cancel, run_process, emit, source_job=context_source)
        manifest["source_symbol_context_state"] = source_symbols["state"]
        from source_key_context import refine_source_keys
        raw, source_keys = refine_source_keys(raw, job, settings, actual, parts_hint, environment(settings), cancel, run_process, emit, source_job=context_source)
        manifest["source_key_context_state"] = source_keys["state"]
        from source_tuplet_context import refine_source_tuplets
        raw, source_tuplets = refine_source_tuplets(raw, job, settings, actual, parts_hint, environment(settings), cancel, run_process, emit, source_job=context_source)
        manifest["source_tuplet_context_state"] = source_tuplets["state"]
        from source_piano_context import refine_source_piano
        raw, source_piano = refine_source_piano(raw, job, settings, actual, parts_hint, environment(settings), cancel, run_process, emit, source_job=context_source)
        manifest["source_piano_context_state"] = source_piano["state"]
        from source_rhythm_context import refine_source_rhythm
        raw, source_rhythm = refine_source_rhythm(raw, job, settings, actual, parts_hint, environment(settings), cancel, run_process, emit, source_job=context_source)
        manifest["source_rhythm_context_state"] = source_rhythm["state"]
        from source_expression_context import refine_source_expression
        raw, source_expression = refine_source_expression(raw, job, settings, actual, parts_hint, environment(settings), cancel, run_process, emit, source_job=context_source)
        manifest["source_expression_context_state"] = source_expression["state"]
        from source_lyrics_context import refine_source_lyrics
        if lyrics and profile != "piano" and language != "en":
            raw, source_lyrics = refine_source_lyrics(raw, job, settings, actual, parts_hint, environment(settings), cancel, run_process, emit, requested_device=device, source_job=context_source)
        else:
            source_lyrics = dict(state="disabled", changes=[], pending=[])
            save_json(job / "source-lyrics-context.json", source_lyrics)
        manifest["source_lyrics_context_state"] = source_lyrics["state"]
        from source_multirest_context import refine_multirest_export
        raw, multirest_report = refine_multirest_export(raw, job, settings, actual, environment(settings),
                                                        cancel, run_process, emit, source_job=context_source)
        manifest["source_multirest_state"] = multirest_report["state"]
        from source_rest_lane_context import refine_source_rest_lanes
        raw, source_rest_lanes = refine_source_rest_lanes(raw, job, emit)
        manifest["source_rest_lane_state"] = source_rest_lanes["state"]
        cooked = normalize_export(raw, lyrics=lyrics and profile != "piano")

        from musicxml_compat import normalize_chord_beams

        chord_compatibility = normalize_chord_beams(cooked)

        save_json(job / "musicxml-compatibility.json", chord_compatibility)

        added_meter = apply_confirmed_start_meter(cooked, initial_meter.strip())

        from musicxml_ties import normalize_tie_chain_order
        tie_compatibility = normalize_tie_chain_order(cooked)
        save_json(job / "musicxml-ties.json", tie_compatibility)
        from source_long_tuplet_context import refine_long_tuplets
        cooked, source_long_tuplets = refine_long_tuplets(cooked, job, settings, actual, environment(settings),
                                                         cancel, run_process, emit, source_job=job)
        manifest["source_long_tuplet_state"] = source_long_tuplets["state"]
        from source_thirty_second_tuplet_context import refine_thirty_second_tuplets
        cooked, source_thirty_second = refine_thirty_second_tuplets(
            cooked, job, job / "source-thirty-second-tuplets", ROOT, settings, actual,
            environment(settings), cancel, run_process, emit)
        save_json(job / "source-thirty-second-tuplets.json", source_thirty_second)
        manifest["source_thirty_second_tuplet_state"] = source_thirty_second["state"]
        from source_paired_staff_context import refine_paired_staff
        cooked, source_paired_staff = refine_paired_staff(
            cooked, job, settings, environment(settings), cancel, run_process, emit)
        manifest["source_paired_staff_state"] = source_paired_staff["state"]
        from source_octave_articulation_context import refine_octave_articulations
        cooked, source_octave_articulations = refine_octave_articulations(
            cooked, job, settings, environment(settings), cancel, run_process, emit)
        manifest["source_octave_articulation_state"] = source_octave_articulations["state"]
        from source_vocal_export_context import refine_vocal_export
        cooked, source_vocal_export = refine_vocal_export(
            cooked, job, settings, environment(settings), cancel, run_process, emit)
        manifest["source_vocal_export_state"] = source_vocal_export["state"]
        from source_drum_context import refine_source_drums
        cooked, source_drum_export = refine_source_drums(
            cooked, job, settings, environment(settings), cancel, run_process, emit)
        manifest["source_drum_export_state"] = source_drum_export["state"]
        cooked, word_tempos = refine_word_tempos(
            cooked, job, context_source, settings, actual, environment(settings), cancel, run_process, emit, source_tempo
        )
        source_tempo["word_tempos"] = word_tempos
        save_json(job / "source-tempo.json", source_tempo)
        from source_boundary_piano_context import refine_boundary_piano
        cooked, boundary_piano = refine_boundary_piano(
            cooked, job, job / "source-boundary-piano", ROOT, settings, actual,
            environment(settings), cancel, run_process, emit)
        save_json(job / "source-boundary-piano.json", boundary_piano)
        manifest["source_boundary_piano_state"] = boundary_piano["state"]
        from source_system_tremolo_context import refine_system_tremolo
        cooked, system_tremolo = refine_system_tremolo(
            cooked, job, job / "source-system-tremolo", ROOT,
            ROOT / "rhythm-core/double-whole", settings, actual, environment(settings),
            cancel, run_process, emit)
        save_json(job / "source-system-tremolo.json", system_tremolo)
        manifest["source_system_tremolo_state"] = system_tremolo["state"]
        from source_vocal_boundary_context import refine_vocal_boundary
        cooked, vocal_boundary = refine_vocal_boundary(
            cooked, job, job / "source-vocal-boundary", ROOT,
            ROOT / "rhythm-core/vocal-boundary", settings, actual, environment(settings),
            cancel, run_process, emit)
        save_json(job / "source-vocal-boundary.json", vocal_boundary)
        manifest["source_vocal_boundary_state"] = vocal_boundary["state"]
        from source_paired_duration import refine_paired_duration
        try:
            cooked, paired_duration = refine_paired_duration(cooked, job)
        except (OSError, ValueError, KeyError, ArithmeticError, TypeError, ET.ParseError) as exc:
            paired_duration = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-paired-duration.json", paired_duration)
        manifest["source_paired_duration_state"] = paired_duration["state"]
        if paired_duration["changes"]:
            emit(f'按原图恢复钢琴双手时值，共 {len(paired_duration["changes"])} 小节')
        from source_repeated_beams_context import refine_repeated_beams
        cooked, repeated_beams = refine_repeated_beams(
            cooked, job, job / "source-repeated-beams", ROOT, settings, actual,
            environment(settings), cancel, run_process, emit)
        save_json(job / "source-repeated-beams.json", repeated_beams)
        manifest["source_repeated_beams_state"] = repeated_beams["state"]
        from source_piano_linear_rhythm_context import refine_linear_piano
        cooked, linear_piano = refine_linear_piano(
            cooked, job, job / "source-linear-piano", ROOT / "rhythm-core/linear-piano",
            ROOT, settings, actual, environment(settings), cancel, run_process, emit)
        save_json(job / "source-linear-piano.json", linear_piano)
        manifest["source_linear_piano_state"] = linear_piano["state"]
        from source_linear_vocal_context import refine_linear_vocal
        cooked, linear_vocal = refine_linear_vocal(
            cooked, job, job / "source-linear-piano", linear_piano, ROOT, settings, actual,
            environment(settings), cancel, run_process, emit)
        save_json(job / "source-linear-vocal.json", linear_vocal)
        manifest["source_linear_vocal_state"] = linear_vocal["state"]
        from source_vocal_consensus_rhythm import recover as recover_source_vocal_rhythm
        try:
            cooked, consensus_vocal = recover_source_vocal_rhythm(cooked, job)
        except (OSError, ValueError, KeyError, ArithmeticError, TypeError, ET.ParseError) as exc:
            consensus_vocal = dict(changes=[], reason=str(exc))
        save_json(job / "source-vocal-consensus-rhythm.json", consensus_vocal)
        manifest["source_vocal_consensus_repairs"] = len(consensus_vocal["changes"])
        if consensus_vocal["changes"]:
            emit(f'按原图恢复 {len(consensus_vocal["changes"])} 处人声符尾与附点')
        from source_rolled_hollow_context import refine_rolled_hollow
        cooked, rolled_hollow = refine_rolled_hollow(
            cooked, job, ROOT, settings, environment(settings), cancel, run_process, emit)
        save_json(job / "source-rolled-hollow.json", rolled_hollow)
        from source_piano_paired_context import refine_piano_paired_marks
        cooked, paired_marks = refine_piano_paired_marks(
            cooked, job, ROOT, settings, environment(settings), cancel, run_process, emit)
        save_json(job / "source-piano-paired-marks.json", paired_marks)
        from source_paired_packed_lyrics import recover as recover_paired_packed_lyrics
        try:
            cooked, paired_packed_lyrics = recover_paired_packed_lyrics(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
            paired_packed_lyrics = dict(changes=[], reason=str(exc))
        save_json(job / "source-paired-packed-lyrics.json", paired_packed_lyrics)
        from source_piano_portato_ties import recover as recover_piano_portato_ties
        try:
            cooked, piano_portato_ties = recover_piano_portato_ties(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
            piano_portato_ties = dict(changes=[], reason=str(exc))
        save_json(job / "source-piano-portato-ties.json", piano_portato_ties)
        if piano_portato_ties["changes"]:
            emit(f'按原谱去除 {len(piano_portato_ties["changes"])} 处误判为延音线的保持音记号')
        from source_stemless_tremolo_projection import recover as recover_stemless_tremolos
        try:
            cooked, stemless_tremolos = recover_stemless_tremolos(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
            stemless_tremolos = dict(changes=[], reason=str(exc))
        save_json(job / "source-stemless-tremolos.json", stemless_tremolos)
        if stemless_tremolos["changes"]:
            emit(f'按原谱恢复 {len(stemless_tremolos["changes"])} 小节钢琴双手震音')
        from source_stemmed_tremolo_projection import recover as recover_stemmed_tremolos
        try:
            cooked, stemmed_tremolos = recover_stemmed_tremolos(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
            stemmed_tremolos = dict(changes=[], reason=str(exc))
        save_json(job / "source-stemmed-tremolos.json", stemmed_tremolos)
        if stemmed_tremolos["changes"]:
            emit(f'按原谱恢复 {len(stemmed_tremolos["changes"])} 小节双手震音和后半拍位')
        from source_triplet_column_projection import recover as recover_triplet_columns
        try:
            cooked, triplet_columns = recover_triplet_columns(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            triplet_columns = dict(changes=[], reason=str(exc))
        save_json(job / "source-triplet-columns.json", triplet_columns)
        if triplet_columns["changes"]:
            emit(f'按原谱数字和双手对位恢复 {len(triplet_columns["changes"])} 小节三连音')
        vocal_split_clock = dict(state="disabled", changes=[])
        if lyrics and profile != "piano":
            from source_vocal_split_clock import recover as recover_vocal_split_clock
            try:
                cooked, vocal_split_clock = recover_vocal_split_clock(cooked, job)
            except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, AssertionError, zipfile.BadZipFile, ET.ParseError) as exc:
                vocal_split_clock = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-vocal-split-clock.json", vocal_split_clock)
        if vocal_split_clock["changes"]:
            emit(f'按原谱对齐 {len(vocal_split_clock["changes"])} 处人声分声部拍位')
        vocal_breath_column = dict(state="disabled", changes=[])
        if lyrics and profile != "piano":
            from source_vocal_breath_column import recover as recover_vocal_breath_column
            try:
                cooked, vocal_breath_column = recover_vocal_breath_column(cooked, job)
            except (OSError, ValueError, IndexError, KeyError, TypeError, StopIteration, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
                vocal_breath_column = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-vocal-breath-column.json", vocal_breath_column)
        if vocal_breath_column["changes"]:
            emit(f'按原谱恢复 {len(vocal_breath_column["changes"])} 个声部呼吸记号')
        from source_piano_horizontal_accent import recover as recover_piano_horizontal_accent
        try:
            cooked, piano_horizontal_accent = recover_piano_horizontal_accent(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            piano_horizontal_accent = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-piano-horizontal-accent.json", piano_horizontal_accent)
        if piano_horizontal_accent["changes"]:
            emit(f'按原谱恢复 {len(piano_horizontal_accent["changes"])} 处钢琴重音组')
        from source_piano_parallel_hollow import recover as recover_piano_parallel_hollow
        try:
            cooked, piano_parallel_hollow = recover_piano_parallel_hollow(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, StopIteration, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            piano_parallel_hollow = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-piano-parallel-hollow.json", piano_parallel_hollow)
        if piano_parallel_hollow["changes"]:
            emit(f'按原谱恢复 {len(piano_parallel_hollow["changes"])} 处钢琴漏失和弦、临时记号和重音')
        from source_tuplet_word_dedup import recover as recover_tuplet_word_duplicates
        try:
            cooked, tuplet_word_duplicates = recover_tuplet_word_duplicates(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            tuplet_word_duplicates = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-tuplet-word-dedup.json", tuplet_word_duplicates)
        if tuplet_word_duplicates["changes"]:
            emit(f'去掉 {len(tuplet_word_duplicates["changes"])} 个与真实三连音重合的重复文字')
        from source_mixed_tremolo_projection import recover as recover_mixed_tremolos
        try:
            cooked, mixed_tremolos = recover_mixed_tremolos(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            mixed_tremolos = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-mixed-tremolo.json", mixed_tremolos)
        if mixed_tremolos["changes"]:
            emit(f'按原谱恢复 {len(mixed_tremolos["changes"])} 处混合震音与另一手的延音时值')
        from source_tremolo_false_clef import recover as recover_tremolo_false_clefs
        try:
            cooked, tremolo_false_clefs = recover_tremolo_false_clefs(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, AssertionError, zipfile.BadZipFile, ET.ParseError) as exc:
            tremolo_false_clefs = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-tremolo-false-clefs.json", tremolo_false_clefs)
        if tremolo_false_clefs["changes"]:
            emit(f'已按原谱撤销 {len(tremolo_false_clefs["changes"])} 处误作谱号的震音线')
        from source_parallel_beam_clock import recover as recover_parallel_beam_clock
        try:
            cooked, parallel_beam_clock = recover_parallel_beam_clock(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            parallel_beam_clock = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-parallel-beam-clock.json", parallel_beam_clock)
        if parallel_beam_clock["changes"]:
            emit(f'已按两手原谱符杠恢复 {len(parallel_beam_clock["changes"])} 处钢琴对齐')
        from source_parallel_tail_ties import recover as recover_parallel_tail_ties
        try:
            cooked, parallel_tail_ties = recover_parallel_tail_ties(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            parallel_tail_ties = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-parallel-tail-ties.json", parallel_tail_ties)
        if parallel_tail_ties["changes"]:
            emit(f'已按原谱复核 {len(parallel_tail_ties["changes"])} 处钢琴短音与跨小节延音')
        from source_parallel_rest_gap import recover as recover_parallel_rest_gaps
        try:
            cooked, parallel_rest_gaps = recover_parallel_rest_gaps(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            parallel_rest_gaps = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-parallel-rest-gaps.json", parallel_rest_gaps)
        if parallel_rest_gaps["changes"]:
            emit(f'已按原谱补回 {len(parallel_rest_gaps["changes"])} 处钢琴双手休止对位')
        from source_parallel_rest_reflow import recover as recover_parallel_rest_reflow
        try:
            cooked, parallel_rest_reflow = recover_parallel_rest_reflow(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, StopIteration, zipfile.BadZipFile, ET.ParseError) as exc:
            parallel_rest_reflow = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-parallel-rest-reflow.json", parallel_rest_reflow)
        if parallel_rest_reflow["changes"]:
            emit(f'已按原谱休止与双手同列校正 {len(parallel_rest_reflow["changes"])} 个钢琴小节')
        vocal_terminal_head = dict(state="disabled", changes=[])
        if lyrics and profile != "piano":
            from source_vocal_terminal_head import recover as recover_vocal_terminal_head
            try:
                cooked, vocal_terminal_head = recover_vocal_terminal_head(cooked, job)
            except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
                vocal_terminal_head = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-vocal-terminal-head.json", vocal_terminal_head)
        if vocal_terminal_head["changes"]:
            emit(f'已按原谱恢复 {len(vocal_terminal_head["changes"])} 处歌词末字和漏识音符')
        from source_parallel_dotted_ties import recover as recover_parallel_dotted_ties
        try:
            cooked, parallel_dotted_ties = recover_parallel_dotted_ties(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            parallel_dotted_ties = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-parallel-dotted-ties.json", parallel_dotted_ties)
        if parallel_dotted_ties["changes"]:
            emit(f'按原谱恢复 {len(parallel_dotted_ties["changes"])} 处钢琴附点和弦、延音与双手拍位')
        from source_parallel_beam_bridge import recover as recover_parallel_beam_bridge
        try:
            cooked, parallel_beam_bridge = recover_parallel_beam_bridge(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, AttributeError, zipfile.BadZipFile, ET.ParseError) as exc:
            parallel_beam_bridge = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-parallel-beam-bridge.json", parallel_beam_bridge)
        if parallel_beam_bridge["changes"]:
            emit(f'已按原谱恢复 {len(parallel_beam_bridge["changes"])} 处断开的钢琴符杠和伴奏休止')
        lyric_glyph_columns = dict(state="disabled", changes=[])
        if lyrics and profile != "piano":
            from source_lyric_glyph_columns import recover as recover_lyric_glyph_columns
            try:
                cooked, lyric_glyph_columns = recover_lyric_glyph_columns(cooked, job)
            except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
                lyric_glyph_columns = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-lyric-glyph-columns.json", lyric_glyph_columns)
        if lyric_glyph_columns["changes"]:
            emit(f'已按原图字列对齐 {len(lyric_glyph_columns["changes"])} 组歌词')
        from source_duplicated_tempo import recover as recover_duplicated_tempo
        try:
            cooked, duplicated_tempo = recover_duplicated_tempo(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            duplicated_tempo = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-duplicated-tempo.json", duplicated_tempo)
        if duplicated_tempo["changes"]:
            emit(f'已按原谱重复标记与小节线恢复 {len(duplicated_tempo["changes"])} 处播放速度')
        from source_parallel_printed_wedge import recover as recover_parallel_printed_wedge
        try:
            cooked, parallel_printed_wedge = recover_parallel_printed_wedge(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            parallel_printed_wedge = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-parallel-printed-wedge.json", parallel_printed_wedge)
        if parallel_printed_wedge["changes"]:
            emit(f'已按原图恢复 {len(parallel_printed_wedge["changes"])} 条漏识的钢琴下谱表渐强线')
        from source_single_staff_tremolo import recover as recover_single_staff_tremolo
        try:
            cooked, single_staff_tremolo = recover_single_staff_tremolo(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            single_staff_tremolo = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-single-staff-tremolo.json", single_staff_tremolo)
        if single_staff_tremolo["changes"]:
            emit(f'根据原图恢复 {len(single_staff_tremolo["changes"])} 小节单手交替震音')
        from source_slur_fragment_marks import recover as recover_slur_fragment_marks
        try:
            cooked, slur_fragment_marks = recover_slur_fragment_marks(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            slur_fragment_marks = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-slur-fragment-marks.json", slur_fragment_marks)
        if slur_fragment_marks["changes"]:
            emit(f'按原图清除 {len(slur_fragment_marks["changes"])} 处连线误识的记号')
        from source_repeated_staccato import recover as recover_repeated_staccato
        try:
            cooked, repeated_staccato = recover_repeated_staccato(cooked, job)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile, ET.ParseError) as exc:
            repeated_staccato = dict(state="unresolved", changes=[], reason=str(exc))
        save_json(job / "source-repeated-staccato.json", repeated_staccato)
        if repeated_staccato["changes"]:
            emit(f'按原谱补回 {len(repeated_staccato["changes"])} 处钢琴跳音点')
        from source_partial_chord_ties import recover as recover_partial_chord_ties
        from source_verified_measure_sequence import recover as recover_measure_sequence

        from source_piano_watermark_ties import recover as recover_piano_watermark_ties
        from source_terminal_beam_voice import recover as recover_terminal_beam_voice
        from source_upper_chord_flats import recover as recover_upper_chord_flats
        from source_wide_bass_tremolo import recover as recover_wide_bass_tremolo
        from source_bass_octave_retained import recover as recover_bass_octave
        from source_cross_system_ottava import recover as recover_cross_system_ottava
        from source_upper_vocal_ties import recover as recover_upper_vocal_ties
        from source_orphaned_piano_lane import recover as recover_orphaned_piano_lane
        from source_paired_piano_accents import recover as recover_paired_piano_accents
        from source_tempo_labels import recover as recover_tempo_labels
        from source_aligned_eighth_rests import recover as recover_aligned_eighth_rests
        from source_parallel_vocal_dots import recover as recover_parallel_vocal_dots
        from source_printed_piano_rests import recover as recover_printed_piano_rests
        from source_gray_voice_over_half_rest import recover as recover_gray_voice_over_half_rest
        from source_repeated_piano_rest_lanes import recover as recover_repeated_piano_rest_lanes
        from source_accidental_clock_collision import recover as recover_accidental_clock_collision
        from source_octave_quarter_triplet_clock import recover as recover_octave_quarter_triplet_clock
        from source_parallel_beam_ink import recover as recover_parallel_beam_ink
        from source_paired_slashed_grace import recover as recover_paired_slashed_grace
        from source_vector_isolated_grace import recover as recover_vector_isolated_grace
        from source_vector_chord_tremolo import recover as recover_vector_chord_tremolo
        from source_vector_whole_chord_key import recover as recover_vector_whole_chord_key
        from source_vector_beamed_piano_bar import recover as recover_vector_beamed_piano_bar
        from source_vector_beamed_piano_bar import recover_octave_accidentals
        from source_vector_missing_bass_clef import recover as recover_vector_missing_bass_clef
        from source_vector_tied_bass_system import recover as recover_vector_tied_bass_system
        from source_vector_sustained_chords import recover as recover_vector_sustained_chords
        from source_vector_inner_chord import recover as recover_vector_inner_chord
        from source_vector_repeated_accompaniment import recover as recover_vector_repeated_accompaniment
        from source_vector_octave_ending import recover as recover_vector_octave_ending
        from source_vector_bass_octave_rest import recover as recover_vector_bass_octave_rest
        from source_vector_dotted_tied_chords import recover as recover_vector_dotted_tied_chords
        from source_vector_chord_tie_chain import recover as recover_vector_chord_tie_chain
        from source_vector_fermata_transition import recover as recover_vector_fermata_transition
        from source_vector_key_cancellation import recover as recover_vector_key_cancellation
        from source_vector_crossbar_slurs import recover as recover_vector_crossbar_slurs
        from source_vector_vocal_whole_ties import recover as recover_vector_vocal_whole_ties
        from source_vector_vocal_bass import recover as recover_vector_vocal_bass
        from source_vector_split_measure import recover as recover_vector_split_measure
        from source_divisi_hollow_head import recover as recover_divisi_hollow_head

        def review_late_stage(name, current, fn):
            try:
                output, detail = fn(current, job)
            except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError, AttributeError, zipfile.BadZipFile, ET.ParseError) as exc:
                output, detail = current, dict(state="unresolved", changes=[], reason=str(exc))
            save_json(job / ("source-" + name + ".json"), detail)
            return output, detail

        cooked, partial_chord_ties = review_late_stage("partial-chord-ties", cooked, recover_partial_chord_ties)
        if partial_chord_ties["changes"]:
            emit(f'按原谱恢复 {len(partial_chord_ties["changes"])} 条和弦延音线')
        lyric_roles_late = dict(state="disabled", changes=[], packed=[], directions=[])
        vocal_marks_late = dict(state="disabled", changes=[])
        if lyrics and profile != "piano":
            from source_misclassified_lyrics import recover as recover_misclassified_lyrics
            from source_vocal_horizontal_accents import recover as recover_vocal_horizontal_accents
            cooked, lyric_roles_late = review_late_stage("misclassified-lyrics", cooked, recover_misclassified_lyrics)
            cooked, vocal_marks_late = review_late_stage("vocal-horizontal-accents", cooked, recover_vocal_horizontal_accents)
            if lyric_roles_late["changes"] or lyric_roles_late.get("packed"):
                emit(f'按原谱补回 {len(lyric_roles_late["changes"])} 个误归类字，拆分 {len(lyric_roles_late.get("packed", []))} 组并字歌词')
        late_piano = {}
        for name, recover in (
            ("upper-chord-flats", recover_upper_chord_flats),
            ("piano-watermark-ties", recover_piano_watermark_ties),
            ("terminal-beam-voice", recover_terminal_beam_voice),
            ("wide-bass-tremolo", recover_wide_bass_tremolo),
            ("bass-octave", recover_bass_octave),
            ("cross-system-ottava", recover_cross_system_ottava),
            ("orphaned-piano-lane", recover_orphaned_piano_lane),
            ("paired-piano-accents", recover_paired_piano_accents),
        ):
            cooked, late_piano[name] = review_late_stage(name, cooked, recover)
        cooked, upper_vocal_ties = review_late_stage("upper-vocal-ties", cooked, recover_upper_vocal_ties)
        if upper_vocal_ties["changes"]:
            emit(f'按原图恢复 {len(upper_vocal_ties["changes"])} 条上方人声延音线')
        cooked, tempo_labels = review_late_stage("tempo-labels", cooked, recover_tempo_labels)
        cooked, aligned_eighth_rests = review_late_stage("aligned-eighth-rests", cooked, recover_aligned_eighth_rests)
        cooked, parallel_vocal_dots = review_late_stage("parallel-vocal-dots", cooked, recover_parallel_vocal_dots)
        cooked, printed_piano_rests = review_late_stage("printed-piano-rests", cooked, recover_printed_piano_rests)
        cooked, late_piano["gray-voice-over-half-rest"] = review_late_stage("gray-voice-over-half-rest", cooked, recover_gray_voice_over_half_rest)
        cooked, late_piano["repeated-piano-rest-lanes"] = review_late_stage("repeated-piano-rest-lanes", cooked, recover_repeated_piano_rest_lanes)
        cooked, late_piano["accidental-clock-collision"] = review_late_stage("accidental-clock-collision", cooked, recover_accidental_clock_collision)
        def recover_beam_and_pitch(current, stage_job):
            return recover_parallel_beam_ink(current, stage_job, source)
        cooked, late_piano["parallel-beam-ink"] = review_late_stage("parallel-beam-ink", cooked, recover_beam_and_pitch)

        def recover_quarter_triplet_clock(current, stage_job):
            return recover_octave_quarter_triplet_clock(current, stage_job, settings, environment(settings), cancel, run_process)
        cooked, late_piano["bracketed-quarter-triplets"] = review_late_stage("bracketed-quarter-triplets-clock", cooked, recover_quarter_triplet_clock)
        cooked, late_piano["paired-slashed-grace"] = review_late_stage("paired-slashed-grace", cooked, recover_paired_slashed_grace)

        def recover_grace_from_pdf(current, stage_job):
            return recover_vector_isolated_grace(current, stage_job, source)
        cooked, late_piano["vector-isolated-grace"] = review_late_stage("vector-isolated-grace", cooked, recover_grace_from_pdf)

        def recover_chord_tremolos_from_pdf(current, stage_job):
            return recover_vector_chord_tremolo(current, stage_job, source)
        cooked, late_piano["vector-chord-tremolo"] = review_late_stage("vector-chord-tremolo", cooked, recover_chord_tremolos_from_pdf)

        def recover_whole_chords_from_pdf(current, stage_job):
            return recover_vector_whole_chord_key(current, stage_job, source)
        cooked, late_piano["vector-whole-chord-key"] = review_late_stage("vector-whole-chord-key", cooked, recover_whole_chords_from_pdf)

        def recover_beamed_piano_from_pdf(current, stage_job):
            return recover_vector_beamed_piano_bar(current, stage_job, source)
        cooked, late_piano["vector-beamed-piano-bar"] = review_late_stage("vector-beamed-piano-bar", cooked, recover_beamed_piano_from_pdf)

        def recover_octave_accidentals_from_pdf(current, stage_job):
            return recover_octave_accidentals(current, stage_job, source)
        cooked, late_piano["vector-octave-accidentals"] = review_late_stage("vector-octave-accidentals", cooked, recover_octave_accidentals_from_pdf)

        def recover_bass_register_from_pdf(current, stage_job):
            return recover_vector_missing_bass_clef(current, stage_job, source)
        cooked, late_piano["vector-missing-bass-clef"] = review_late_stage("vector-missing-bass-clef", cooked, recover_bass_register_from_pdf)

        def recover_tied_bass_from_pdf(current, stage_job):
            return recover_vector_tied_bass_system(current, stage_job, source)
        cooked, late_piano["vector-tied-bass-system"] = review_late_stage("vector-tied-bass-system", cooked, recover_tied_bass_from_pdf)

        def recover_sustained_chords_from_pdf(current, stage_job):
            return recover_vector_sustained_chords(current, stage_job, source)
        cooked, late_piano["vector-sustained-chords"] = review_late_stage("vector-sustained-chords", cooked, recover_sustained_chords_from_pdf)

        def recover_inner_chords_from_pdf(current, stage_job):
            return recover_vector_inner_chord(current, stage_job, source)
        cooked, late_piano["vector-inner-chord"] = review_late_stage("vector-inner-chord", cooked, recover_inner_chords_from_pdf)

        def recover_repeated_accompaniment_from_pdf(current, stage_job):
            return recover_vector_repeated_accompaniment(current, stage_job, source)
        cooked, late_piano["vector-repeated-accompaniment"] = review_late_stage("vector-repeated-accompaniment", cooked, recover_repeated_accompaniment_from_pdf)

        def recover_octave_ending_from_pdf(current, stage_job):
            return recover_vector_octave_ending(current, stage_job, source)
        cooked, late_piano["vector-octave-ending"] = review_late_stage("vector-octave-ending", cooked, recover_octave_ending_from_pdf)

        def recover_bass_octave_rest_from_pdf(current, stage_job):
            return recover_vector_bass_octave_rest(current, stage_job, source)
        cooked, late_piano["vector-bass-octave-rest"] = review_late_stage("vector-bass-octave-rest", cooked, recover_bass_octave_rest_from_pdf)

        def recover_dotted_tied_chords_from_pdf(current, stage_job):
            return recover_vector_dotted_tied_chords(current, stage_job, source)
        cooked, late_piano["vector-dotted-tied-chords"] = review_late_stage("vector-dotted-tied-chords", cooked, recover_dotted_tied_chords_from_pdf)

        def recover_chord_tie_chain_from_pdf(current, stage_job):
            return recover_vector_chord_tie_chain(current, stage_job, source)
        cooked, late_piano["vector-chord-tie-chain"] = review_late_stage("vector-chord-tie-chain", cooked, recover_chord_tie_chain_from_pdf)

        def recover_fermata_transition_from_pdf(current, stage_job):
            return recover_vector_fermata_transition(current, stage_job, source)
        cooked, late_piano["vector-fermata-transition"] = review_late_stage("vector-fermata-transition", cooked, recover_fermata_transition_from_pdf)
        from source_printed_quarter_tempo import recover as recover_printed_quarter_tempo

        def recover_printed_tempo_from_pdf(current, stage_job):
            return recover_printed_quarter_tempo(current, stage_job, source)

        cooked, late_piano["printed-quarter-tempo"] = review_late_stage(
            "printed-quarter-tempo", cooked, recover_printed_tempo_from_pdf
        )


        def recover_key_cancellation_from_pdf(current, stage_job):
            return recover_vector_key_cancellation(current, stage_job, source)
        cooked, late_piano["vector-key-cancellation"] = review_late_stage("vector-key-cancellation", cooked, recover_key_cancellation_from_pdf)

        def recover_crossbar_slurs_from_pdf(current, stage_job):
            return recover_vector_crossbar_slurs(current, stage_job, source)
        cooked, late_piano["vector-crossbar-slurs"] = review_late_stage("vector-crossbar-slurs", cooked, recover_crossbar_slurs_from_pdf)

        def recover_vocal_whole_ties_from_pdf(current, stage_job):
            return recover_vector_vocal_whole_ties(current, stage_job, source)
        cooked, vocal_whole_ties = review_late_stage("vector-vocal-whole-ties", cooked, recover_vocal_whole_ties_from_pdf)

        def recover_vocal_bass_from_pdf(current, stage_job):
            return recover_vector_vocal_bass(current, stage_job, source)
        cooked, vector_vocal_bass = review_late_stage("vector-vocal-bass", cooked, recover_vocal_bass_from_pdf)

        from source_aligned_bass_ink import recover as recover_aligned_bass_ink
        from source_vocal_repeat_unison import recover as recover_vocal_repeat_unison

        def recover_aligned_bass_from_pdf(current, stage_job):
            return recover_aligned_bass_ink(current, stage_job, source)

        cooked, aligned_bass_ink = review_late_stage("aligned-bass-ink", cooked, recover_aligned_bass_from_pdf)

        vector_repeat_vocal = dict(state="disabled", changes=[])
        if lyrics and profile != "piano":
            def recover_repeat_vocal_from_pdf(current, stage_job):
                return recover_vocal_repeat_unison(current, stage_job, source)

            cooked, vector_repeat_vocal = review_late_stage("repeat-vocal", cooked, recover_repeat_vocal_from_pdf)

        from source_grace_links import recover as recover_grace_links

        def recover_grace_links_from_pdf(current, stage_job):
            return recover_grace_links(current, stage_job, source)

        cooked, grace_links = review_late_stage("grace-links", cooked, recover_grace_links_from_pdf)
        from source_lyric_false_marcato import recover as recover_lyric_false_marcato

        def recover_lyric_marcato_from_pdf(current, stage_job):
            return recover_lyric_false_marcato(current, stage_job, source)

        cooked, lyric_false_marcato = review_late_stage("lyric-false-marcato", cooked, recover_lyric_marcato_from_pdf)
        vector_vocal_lyrics = dict(state="disabled", changes=[])
        if lyrics and profile != "piano":
            from source_vector_vocal_lyrics import recover as recover_vector_vocal_lyrics
            def recover_vocal_lyrics_from_pdf(current, stage_job):
                return recover_vector_vocal_lyrics(current, stage_job, source)
            cooked, vector_vocal_lyrics = review_late_stage("vector-vocal-lyrics", cooked, recover_vocal_lyrics_from_pdf)
        vocal_attack_ink = dict(state="disabled", changes=[])
        if lyrics and profile != "piano":
            from source_vocal_attack_ink import recover as recover_vocal_attack_ink

            def recover_vocal_attack_from_pdf(current, stage_job):
                return recover_vocal_attack_ink(current, stage_job, source)

            cooked, vocal_attack_ink = review_late_stage("vocal-attack-ink", cooked, recover_vocal_attack_from_pdf)
            repaired = {(row["part"], row["measure"]) for row in vocal_attack_ink["changes"]}
            if repaired:
                vector_vocal_lyrics["pending"] = [
                    row for row in vector_vocal_lyrics.get("pending", [])
                    if (row["part"], row["measure"]) not in repaired
                ]
                save_json(job / "source-vector-vocal-lyrics.json", vector_vocal_lyrics)
        from source_vector_coda_dynamics import recover as recover_vector_coda_dynamics

        def recover_coda_dynamics_from_pdf(current, stage_job):
            return recover_vector_coda_dynamics(current, stage_job, source)

        cooked, late_piano["vector-coda-dynamics"] = review_late_stage("vector-coda-dynamics", cooked, recover_coda_dynamics_from_pdf)
        from source_vector_dotted_tied_chords import recover_vocal_endings

        def recover_vocal_endings_from_pdf(current, stage_job):
            return recover_vocal_endings(current, stage_job, source)

        cooked, late_piano["vector-vocal-dotted-endings"] = review_late_stage(
            "vector-vocal-dotted-endings", cooked, recover_vocal_endings_from_pdf
        )
        from source_vector_double_flats import recover as recover_vector_double_flats

        def recover_double_flats_from_pdf(current, stage_job):
            return recover_vector_double_flats(current, stage_job, source)

        cooked, late_piano["vector-double-flats"] = review_late_stage(
            "vector-double-flats", cooked, recover_double_flats_from_pdf
        )
        from source_vector_coda_dynamics import terminal_crescendos

        def recover_terminal_crescendos_from_pdf(current, stage_job):
            return terminal_crescendos(current, stage_job, source)

        cooked, late_piano["vector-terminal-crescendos"] = review_late_stage(
            "vector-terminal-crescendos", cooked, recover_terminal_crescendos_from_pdf
        )
        cooked, divisi_hollow = review_late_stage("divisi-hollow-head", cooked, recover_divisi_hollow_head)
        from source_opus_bass_articulations import recover as recover_opus_bass_articulations

        def recover_opus_bass_from_pdf(current, stage_job):
            return recover_opus_bass_articulations(current, stage_job, source)

        cooked, opus_bass_articulations = review_late_stage(
            "opus-bass-articulations", cooked, recover_opus_bass_from_pdf
        )
        cooked, measure_sequence = review_late_stage("verified-measure-sequence", cooked, recover_measure_sequence)
        if measure_sequence["changes"]:
            emit('已按原谱实体小节顺序校正导出编号')
        def recover_split_from_pdf(current, stage_job):
            return recover_vector_split_measure(current, stage_job, source)
        cooked, split_measure = review_late_stage("vector-split-measure", cooked, recover_split_from_pdf)
        from source_playback_jumps import recover as recover_playback_jumps

        def recover_jumps_from_pdf(current, stage_job):
            return recover_playback_jumps(current, stage_job, source, split_measure)

        cooked, playback_jumps = review_late_stage("playback-jumps", cooked, recover_jumps_from_pdf)
        report = audit(cooked)
        report["source_playback_jumps"] = playback_jumps
        report["source_partial_chord_ties"] = partial_chord_ties
        report["source_misclassified_lyrics"] = lyric_roles_late
        report["source_vocal_horizontal_accents"] = vocal_marks_late
        report["source_verified_measure_sequence"] = measure_sequence
        report["source_vector_split_measure"] = split_measure
        report["source_late_piano"] = late_piano
        report["source_opus_bass_articulations"] = opus_bass_articulations
        report["source_upper_vocal_ties"] = upper_vocal_ties
        report["source_tempo_labels"] = tempo_labels
        report["source_vector_vocal_whole_ties"] = vocal_whole_ties
        report["source_vector_vocal_bass"] = vector_vocal_bass
        report["source_vector_vocal_lyrics"] = vector_vocal_lyrics
        report["source_vocal_attack_ink"] = vocal_attack_ink
        report["source_aligned_eighth_rests"] = aligned_eighth_rests
        report["source_parallel_vocal_dots"] = parallel_vocal_dots
        report["source_printed_piano_rests"] = printed_piano_rests
        report["source_divisi_hollow"] = divisi_hollow
        report["source_repeated_staccato"] = repeated_staccato
        report["source_slur_fragment_marks"] = slur_fragment_marks
        report["source_vocal_terminal_head"] = vocal_terminal_head
        report["source_single_staff_tremolo"] = single_staff_tremolo
        report["source_parallel_printed_wedge"] = parallel_printed_wedge
        report["source_duplicated_tempo"] = duplicated_tempo
        report["source_lyric_glyph_columns"] = lyric_glyph_columns
        report["source_parallel_beam_bridge"] = parallel_beam_bridge
        report["source_parallel_dotted_ties"] = parallel_dotted_ties
        report["source_parallel_rest_reflow"] = parallel_rest_reflow
        report["source_parallel_rest_gap"] = parallel_rest_gaps
        report["source_parallel_tail_ties"] = parallel_tail_ties
        report["source_parallel_beam_clock"] = parallel_beam_clock
        report["source_tremolo_false_clef"] = tremolo_false_clefs
        report["source_mixed_tremolo"] = mixed_tremolos
        report["source_tuplet_word_dedup"] = tuplet_word_duplicates
        report["source_piano_parallel_hollow"] = piano_parallel_hollow
        report["source_piano_horizontal_accent"] = piano_horizontal_accent
        report["source_vocal_breath_column"] = vocal_breath_column
        report["source_vocal_split_clock"] = vocal_split_clock
        report["source_triplet_columns"] = triplet_columns
        report["source_stemmed_tremolos"] = stemmed_tremolos
        report["source_stemless_tremolos"] = stemless_tremolos
        report["source_piano_portato_ties"] = piano_portato_ties
        report["source_paired_packed_lyrics"] = paired_packed_lyrics
        report["source_piano_paired_marks"] = paired_marks
        report["source_rolled_hollow"] = rolled_hollow
        report["source_repeated_beams"] = repeated_beams
        report["source_linear_piano"] = linear_piano
        report["source_linear_vocal"] = linear_vocal
        report["source_vocal_consensus_rhythm"] = consensus_vocal
        report["source_paired_duration"] = paired_duration
        report["source_long_tuplets"] = source_long_tuplets
        report["source_thirty_second_tuplets"] = source_thirty_second
        report["source_paired_staff"] = source_paired_staff
        report["source_octave_articulations"] = source_octave_articulations
        report["source_vocal_export"] = source_vocal_export
        report["source_drum_export"] = source_drum_export
        report["source_boundary_piano"] = boundary_piano
        report["source_system_tremolo"] = system_tremolo
        report["source_vocal_boundary"] = vocal_boundary
        if source_long_tuplets["pending"] or source_long_tuplets["state"] == "unresolved":
            report["issues"].append(dict(kind="钢琴连音待核", part="钢琴", part_id="钢琴",
                                        measure="详见报告", level="提示", detail="详见 source-long-tuplets-context.json。"))
        from source_wedge_integrity import audit_wedge_integrity
        wedge_integrity = audit_wedge_integrity(cooked, job)
        report["wedge_integrity"] = dict(state=wedge_integrity["state"], paired_wedges=wedge_integrity["paired_wedges"], export_issues=len(wedge_integrity["export_issues"]), pending_diagnostics=len(wedge_integrity["native_pending_diagnostics"]), report="wedge-integrity.json")
        if wedge_integrity["state"] != "paired":
            report["issues"].append(dict(kind="渐强渐弱线端点待核", part="整谱", part_id="整谱", measure="详见报告", level="重点核对", detail="部分原谱渐强渐弱线缺少可用的节奏端点，未输出不完整长线；导出配对检查和原生待核记录见wedge-integrity.json。"))
        report["source_native_preflight"] = dict(state=native_preflight["state"], retired_void_heads=native_preflight["retired_void_heads"], early_added_times=native_preflight["early_added_times"], early_retired_time_heads=native_preflight["early_retired_time_heads"], pending=len(native_preflight["pending"]), report="source-native-preflight.json")
        if native_preflight["pending"]:
            report["issues"].append(dict(kind="原图前置校验待核", part="整谱", part_id="整谱", measure="详见报告", level="重点核对", detail="部分符头、拍号或关联关系证据不足，保留原对象；详见source-native-preflight.json。"))
        report["source_meter_context"] = dict(state=source_meter["state"], added_times=source_meter["added_times"], retired_time_heads=source_meter["retired_time_heads"], pending=len(source_meter["pending"]), report="source-meter-context.json")
        if source_meter["state"] in ("unresolved", "not_assessed", "needs_review", "partially_restored"):
            report["issues"].append(dict(kind="原图拍号待核", part="整谱", part_id="整谱", measure="详见报告", level="重点核对", detail="部分拍号或数字与音符冲突尚缺完整证据；原档保留，详见source-meter-context.json。"))
        report["source_lyrics_context"] = dict(state=source_lyrics["state"], added_characters=len(source_lyrics.get("changes", [])), aligned_syllables=len(source_lyrics.get("syllable_changes", [])), pending=len(source_lyrics.get("pending", [])), report="source-lyrics-context.json")
        if source_lyrics["state"] in ("unresolved", "not_assessed", "needs_review", "partially_restored"):
            report["issues"].append(dict(kind="歌词与力度归属待核", part="人声", part_id="人声", measure="详见报告", level="重点核对", detail="部分歌词或力度记号的原图归属证据不足，已保留待核；详见source-lyrics-context.json。"))

        report["source_expression_context"] = dict(state=source_expression["state"], articulation_removals=len(source_expression.get("articulation_removals", [])), wedge_additions=len(source_expression.get("wedge_additions", [])), pending=len(source_expression.get("pending", [])), report="source-expression-context.json")
        if source_expression["state"] in ("unresolved", "not_assessed", "needs_review", "partially_restored"):
            report["issues"].append(dict(kind="演奏记号待核", part="整谱", part_id="整谱", measure="详见报告", level="重点核对", detail="部分演奏记号缺少唯一原图证据，保留此前内容；详见source-expression-context.json。"))
        report["source_rhythm_context"] = dict(state=source_rhythm["state"], changes=len(source_rhythm.get("changes", [])), pending=len(source_rhythm.get("pending", [])), report="source-rhythm-context.json")
        if source_rhythm["state"] in ("unresolved", "not_assessed", "needs_review", "partially_restored"):
            report["issues"].append(dict(kind="符尾或附点待核", part="整谱", part_id="整谱", measure="详见报告", level="重点核对", detail="部分多拍小节缺少唯一的原图节奏证据，保留原识别内容；详见source-rhythm-context.json。"))
        report["source_piano_context"] = dict(state=source_piano["state"], pitch_increase=source_piano["pitches_after"]-source_piano["pitches_before"], pending=len(source_piano.get("pending", [])), report="source-piano-context.json")
        if source_piano["state"] in ("unresolved", "not_assessed", "needs_review", "partially_restored"):
            report["issues"].append(dict(kind="钢琴结构待核", part="钢琴", part_id="钢琴", measure="详见报告", level="重点核对", detail="部分原图恢复尚未通过完整校验，已保留此前结果；请结合source-piano-context.json核对。"))
        report["source_tuplet_context"] = dict(state=source_tuplets["state"], groups=source_tuplets.get("groups", 0), pending=len(source_tuplets["pending"]), report="source-tuplet-context.json")
        if source_tuplets["state"] in ("unresolved", "not_assessed", "needs_review", "partially_restored"):
            report["issues"].append(dict(kind="钢琴连音待核", part="整谱", part_id="整谱", measure="详见报告", level="重点核对", detail="部分连音缺少完整原图或节奏证据；已保留原识别结果，请结合source-tuplet-context.json核对。"))
        report["source_key_context"] = dict(state=source_keys["state"], changed=len(source_keys.get("key_changes", [])), pitch_updates=len(source_keys.get("pitch_alter_changes", [])), pending=len(source_keys.get("pending", [])), scope="原图支持的行首缺失调号", report="source-key-context.json")
        if source_keys["state"] in ("unresolved", "not_assessed", "needs_review", "partially_restored"):
            report["issues"].append(dict(kind="调号继承待核", part="整谱", part_id="整谱", measure="详见报告", level="重点核对", detail="部分行首调号没有足够原图证据或未完成校验；尚不能确认其是否为C调，请结合原谱和source-key-context.json核对。"))
        report["source_symbol_context"] = dict(state=source_symbols["state"], removed=len(source_symbols.get("removed", [])), scope="跨行表情文字与踏板符号冲突", report="source-symbol-context.json")
        if source_symbols["state"] in ("unresolved", "not_assessed"):
            report["issues"].append(dict(kind="文字与符号待核", part="整谱", part_id="整谱", measure="详见报告", level="提示", detail="原图文字与音乐符号的复核尚未完成；已保留此前结果，请结合source-symbol-context.json核对。"))
        report["tie_compatibility"] = dict(changed=len(tie_compatibility["changes"]), unresolved=len(tie_compatibility["unresolved"]), report="musicxml-ties.json")
        if tie_compatibility["unresolved"]:
            report["issues"].append(dict(kind="延音线待核", part="整谱", part_id="整谱", measure="详见报告", level="提示", detail="部分延音线涉及反复或端点不明确，尚未自动调整；请结合原谱和musicxml-ties.json核对。"))
        report["source_word_tempos"] = dict(state=word_tempos["state"], restored=len(word_tempos["changes"]))
        if word_tempos["state"] == "unresolved":
            report["issues"].append(dict(kind="文字速度待核", part="整谱", part_id="整谱", measure="详见报告",
                                         level="重点核对", detail=word_tempos["reason"]))
        report["source_tempo"] = dict(state=source_tempo["state"], changed=len(source_tempo.get("changes", [])), pending=source_tempo.get("review_count", 0), scope="已检出的速度标记", report="source-tempo.json")
        if source_tempo["state"] in ("unresolved", "needs_review", "partially_restored", "not_assessed"):
            report["issues"].append(dict(kind="速度标记待核", part="整谱", part_id="整谱", measure="详见报告", level="重点核对", detail="部分速度标记尚未通过原图或导出位置校验；已保留此前相应结果，请结合source-tempo.json核对。"))
        report["source_context"] = dict(state=source_context["state"], restored=source_context.get("native_clefs_restored", 0), octave_updates=len(source_context.get("octave_changes", [])), report="source-context.json")
        if source_context["state"] == "unresolved":
            report["issues"].append(dict(kind="谱号复核未完成", part="整谱", part_id="整谱", measure="全部", level="重点核对", detail="原图谱号复核未通过完整一致性检查；请结合source-context.json核对，当前导出保留此前识别结果。"))

        report["musicxml_compatibility"] = dict(changed_chords=len(chord_compatibility["changes"]), unresolved=len(chord_compatibility["unresolved"]), report="musicxml-compatibility.json")

        report["confirmed_start_meter_added"] = added_meter

        report["source_mapping"] = "source-map.json；当前为图块映射，尚未实现逐音符原页定位"

        if profile == "piano" and len(raw.findall("part")) != 1:

            report["issues"].insert(0, dict(kind="钢琴声部分裂", part="整谱", part_id="整谱", measure="全部",

                                           level="重点核对", detail="预期一个钢琴大谱表，实际识别成多个乐器声部；不能当作可用结果"))

        for match in re.finditer(r"(?m)^.*WARN.*$", log):

            if any(word in match.group() for word in ("staves", "clef", "logical", "hint")):

                report["issues"].append(dict(kind="引擎结构警告", part="整谱", part_id="整谱", measure="日志",

                                             level="重点核对", detail=match.group()[-250:]))

        save_json(job / "audit.json", report)

        dest_dir = Path(settings["output_dir"])

        dest_dir.mkdir(parents=True, exist_ok=True)

        name = re.sub(r'[<>:"/\\|?*]', "_", source.stem)[:85]

        output = dest_dir / f"{name}_待核对_{job.name}.musicxml"

        from source_repeated_accompaniment_context import recover as recover_repeated_accompaniment
        cooked, repeated_accompaniment = recover_repeated_accompaniment(
            cooked, job, settings, actual, environment(settings), cancel, run_process, emit)
        save_json(job / "source-repeated-accompaniment.json", repeated_accompaniment)
        report["source_repeated_accompaniment"] = repeated_accompaniment

        from source_vocal_consistency_context import recover as recover_vocal_consistency
        cooked, vocal_consistency = recover_vocal_consistency(
            cooked, job, context_source, settings, environment(settings), cancel, run_process, emit)
        save_json(job / "source-vocal-consistency.json", vocal_consistency)
        report["source_vocal_consistency"] = vocal_consistency

        from source_vector_vocal_flag import recover as recover_vector_vocal_flag
        def recover_vocal_flag_from_pdf(current, stage_job):
            return recover_vector_vocal_flag(current, stage_job, source)
        cooked, vocal_flags = review_late_stage("vector-vocal-flag", cooked, recover_vocal_flag_from_pdf)
        report["source_vector_vocal_flag"] = vocal_flags
        if vocal_flags["changes"]:
            repaired = {(c["part"], c["measure"]) for c in vocal_flags["changes"]}
            report["issues"] = [i for i in report["issues"]
                                if i["kind"] != "小节超长" or (i["part_id"], str(i["measure"])) not in repaired]
            emit(f'已从原谱校正 {len(repaired)} 处人声八分符尾和歌词对位')

        from piano_checks import inspect_piano_tuplets, split_resolved_tuplet_findings

        tuplet_findings, restored_findings = split_resolved_tuplet_findings(
            inspect_piano_tuplets(context_source), source_tuplets
        )
        from source_tuplet_final_review import confirm_exported_tuplets
        try:
            tuplet_findings, confirmed_findings = confirm_exported_tuplets(
                cooked, job, context_source, tuplet_findings)
        except (OSError, ValueError, IndexError, KeyError, TypeError, ArithmeticError,
                AttributeError, zipfile.BadZipFile, ET.ParseError):
            confirmed_findings = []
        save_json(job / "piano-tuplet-review.json", dict(
            rule="native-number-beam-group-with-final-verification", findings=tuplet_findings,
            restored=restored_findings, confirmed_in_export=confirmed_findings
        ))
        report["piano_tuplet_review"] = dict(
            count=len(tuplet_findings), restored=len(restored_findings),
            confirmed_in_export=len(confirmed_findings), report="piano-tuplet-review.json"
        )

        piano_ids = [p.get("id") for p in cooked.findall("part-list/score-part") if p.findtext("part-name", "").lower() == "piano"]

        for finding in tuplet_findings:

            report["issues"].append(dict(kind="钢琴连音漏识疑点", part="Piano", part_id=piano_ids[0] if len(piano_ids)==1 else "钢琴", measure=finding["source_sheet"].split("/")[0]+"/谱组"+finding["system"], level="重点核对", detail="原图位置的数字"+finding["digit"]+"与"+str(len(finding["chords"]))+"个发音的符杠组对应，但缺少完整连音关联；需复核原图并重新计算时值，当前未自动改拍。"))

        if lyrics and profile != "piano":
            from source_direction_lyric_cleanup import cleanup

            cooked, lyric_direction_cleanup = cleanup(cooked)
            save_json(job / "source-lyric-direction-cleanup.json", lyric_direction_cleanup)
            report["source_lyric_direction_cleanup"] = {
                "duplicates": len(lyric_direction_cleanup["changes"]),
                "pending": len(lyric_direction_cleanup["pending"]),
            }
            from source_vector_direction_lyric_echo import recover as recover_vector_lyric_echo

            def recover_lyric_echo_from_pdf(current, stage_job):
                return recover_vector_lyric_echo(current, stage_job, source)

            cooked, echo_cleanup = review_late_stage("vector-direction-lyric-echo", cooked, recover_lyric_echo_from_pdf)
            report["source_vector_lyric_echo"] = echo_cleanup
        report["source_vector_repeat_vocal"] = vector_repeat_vocal
        report["source_aligned_bass_ink"] = aligned_bass_ink
        report["source_grace_links"] = grace_links
        report["source_lyric_false_marcato"] = lyric_false_marcato

        from source_printed_measure_numbers import omit_engraving_labels
        cooked, engraving_labels = review_late_stage(
            "engraving-label-policy", cooked, omit_engraving_labels
        )
        report["source_engraving_labels"] = engraving_labels
        if engraving_labels["changes"]:
            emit(f'已忽略 {len(engraving_labels["changes"])} 处原谱版面编号文字')


        if profile == SINGLE_STAFF_PROFILE:
            validate_preset(cooked, parts_hint)
            cooked = normalize_export(cooked, lyrics=False, clean_positions=False)

        ET.indent(cooked, space="  ")

        from final_export_checks import write_review_export
        final_checks = write_review_export(cooked, output)
        save_json(job / "final-export-checks.json", final_checks)
        final_names = {p.get("id"): p.findtext("part-name", "") for p in cooked.findall("part-list/score-part")}
        known_issues = {(v.get("kind"), v.get("part_id"), str(v.get("measure"))) for v in report["issues"]}
        for finding in final_checks["issues"]:
            identity = (finding["kind"], finding["part"], str(finding.get("measure", "全部")))
            if identity not in known_issues:
                report["issues"].append(dict(kind=identity[0], part=final_names.get(identity[1], identity[1]), part_id=identity[1], measure=identity[2], level="重点核对", detail="最终导出校验：" + finding.get("text", finding["kind"]) + "；保留原内容，需结合原谱核对。"))
                known_issues.add(identity)
        report["final_export_checks"] = dict(issues=len(final_checks["issues"]), report="final-export-checks.json")
        save_json(job / "audit.json", report)


        manifest.update(state="初稿已导出，待音乐核对", output=str(output), warnings=len(report["issues"]))

        emit(f"已导出检查稿；发现{len(report['issues'])}项结构疑点/提示，不代表全部错误数量")

        return dict(output=str(output), job=str(job), report=report)

    except MemoryBudgetError as exc:
        manifest.update(state="因内存不足暂停", error=str(exc))
        raise
    except Exception as exc:

        manifest.update(state="已停止" if isinstance(exc, InterruptedError) else "失败", error=str(exc))

        raise

    finally:

        stop_owned(proc)

        save_json(job / "manifest.json", manifest)





def main():

    parser = argparse.ArgumentParser()

    parser.add_argument("--input")

    parser.add_argument("--pages", default="")

    parser.add_argument("--profile", choices=["piano", "choir", "general", SINGLE_STAFF_PROFILE], default="piano")

    parser.add_argument("--device", choices=["auto", "cpu", "gpu"], default="auto")

    parser.add_argument("--parts-hint", default="")

    parser.add_argument("--meter", default="")

    parser.add_argument("--language", choices=["en", "multi"], default="en")

    parser.add_argument("--whole-page", action="store_true")

    parser.add_argument("--sharpen", action="store_true")

    parser.add_argument("--cut-y", type=int, help="单页原图已目视核对的切分y坐标，不擦除内容")

    parser.add_argument("--threshold", type=int, help="实验：全局二值化阈值；默认由引擎自适应")

    parser.add_argument("--audit", help="仅检查已有XML/MXL，不改文件")

    args = parser.parse_args()

    if args.audit:

        print(json.dumps(audit(read_score(args.audit)), ensure_ascii=False, indent=2))

    elif args.input:

        result = process(args.input, args.pages, args.profile, args.device, args.language,

                         not args.whole_page, args.sharpen, parts_hint=args.parts_hint, initial_meter=args.meter,

                         cut_y=args.cut_y, threshold=args.threshold)

        print(json.dumps({k: v for k, v in result.items() if k != "report"}, ensure_ascii=False))

    else:

        from gui import launch

        launch()





if __name__ == "__main__":

    main()

