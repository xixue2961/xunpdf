"""中文桌面入口；后台工作通过队列更新界面。"""
import os
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from xunpdf import ROOT, process, read_settings
from score_structure import PRESETS, SINGLE_STAFF_PROFILE, resolve_hint


PROFILES = {
    "钢琴谱": "piano",
    "合唱谱": "choir",
    "其他总谱": "general",
    "单声部谱（无歌词）": SINGLE_STAFF_PROFILE,
}


def launch():
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    window = tk.Tk()
    # Respect Windows DPI; fixed 1.0 made the interface tiny on high-DPI screens.
    import tkinter.font as tkfont
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=12)
    window.title("XUN识谱 · 本地乐谱识别")
    window.geometry(f"{min(1100, window.winfo_screenwidth()-60)}x{min(760, window.winfo_screenheight()-80)}+25+25")
    window.minsize(900, 680)
    window.after(50, lambda: window.state("zoomed"))
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure("TLabel", font=("Microsoft YaHei UI", 12))
    style.configure("TButton", font=("Microsoft YaHei UI", 12), padding=(14, 10))
    style.configure("TEntry", padding=6)
    style.configure("TCombobox", padding=5)
    style.configure("Treeview", font=("Microsoft YaHei UI", 11), rowheight=32)
    style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 11, "bold"))
    style.configure("Primary.TButton", foreground="white", background="#245ab8")
    style.map("Primary.TButton", background=[("active", "#194692"), ("disabled", "#a5b3c9")])
    panel = ttk.Frame(window, padding=18)
    panel.pack(fill="both", expand=True)
    heading = ttk.Frame(panel)
    heading.pack(fill="x")
    ttk.Label(heading, text="XUN识谱", font=("Microsoft YaHei UI", 21, "bold")).pack(side="left")
    preferences = ROOT / "ui-preferences.json"
    try:
        saved_size = int(json.loads(preferences.read_text(encoding="utf-8")).get("font_size", 12))
    except (OSError, ValueError, TypeError):
        saved_size = 12
    font_size = tk.IntVar(value=max(11, min(16, saved_size)))
    def resize_text(delta=0):
        size = max(11, min(16, font_size.get()+delta))
        font_size.set(size)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(size=size)
        for kind in ("TLabel", "TButton", "TCheckbutton", "TEntry", "TCombobox", "TNotebook.Tab", "TLabelframe.Label"):
            style.configure(kind, font=("Microsoft YaHei UI", size))
        style.configure("Treeview", font=("Microsoft YaHei UI", size), rowheight=int(size*2.8))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", size, "bold"))
        logs.configure(font=("Microsoft YaHei UI", size))
        try:
            preferences.write_text(json.dumps({"font_size":size}), encoding="utf-8")
        except OSError:
            pass
    ttk.Button(heading, text="字号 ＋", command=lambda: resize_text(1)).pack(side="right")
    ttk.Button(heading, text="字号 －", command=lambda: resize_text(-1)).pack(side="right", padx=6)
    ttk.Label(panel, text="选择乐谱 → 设置声部与歌词 → 开始识别 → 检查并导出 MusicXML").pack(anchor="w", pady=(5, 13))
    source = tk.StringVar()
    pages = tk.StringVar()
    profile = tk.StringVar(value="合唱谱")
    device = tk.StringVar(value="自动（优先GPU）")
    language = tk.StringVar(value="中文／多语言")
    hints = tk.StringVar()
    meter = tk.StringVar()
    split = tk.BooleanVar(value=False)
    lyrics = tk.BooleanVar(value=True)
    row = ttk.Frame(panel)
    row.pack(fill="x")
    ttk.Entry(row, textvariable=source).pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="选择PDF", command=lambda: source.set(filedialog.askopenfilename(filetypes=[("乐谱PDF", "*.pdf")]) or source.get())).pack(side="left", padx=(7, 0))
    ttk.Button(row, text="查看原谱", command=lambda: os.startfile(source.get()) if Path(source.get()).is_file() else None).pack(side="left", padx=(7, 0))
    export_dir = tk.StringVar(value=read_settings().get("output_dir", str(Path.home()/"Desktop")))
    output_row = ttk.Frame(panel)
    output_row.pack(fill="x", pady=(10, 0))
    ttk.Label(output_row, text="导出到").pack(side="left", padx=(0, 8))
    ttk.Entry(output_row, textvariable=export_dir).pack(side="left", fill="x", expand=True)
    def choose_export():
        selected = filedialog.askdirectory(title="选择XML导出文件夹", initialdir=export_dir.get() if Path(export_dir.get()).is_dir() else str(Path.home()/"Desktop"))
        if selected:
            export_dir.set(selected)
            settings = read_settings(resolve_paths=False)
            settings["output_dir"] = selected
            (ROOT/"settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf8")
    ttk.Button(output_row, text="选择文件夹", command=choose_export).pack(side="left", padx=(7, 0))
    options = ttk.LabelFrame(panel, text="识别设置", padding=10)
    options.pack(fill="x", pady=12)
    options.columnconfigure(1, weight=1)
    ttk.Label(options, text="页码").grid(row=0, column=0, sticky="w")
    ttk.Entry(options, textvariable=pages).grid(row=0, column=1, sticky="ew", padx=8)
    ttk.Label(options, text="例：1-3,5；留空为全部页").grid(row=0, column=2, sticky="w")
    ttk.Label(options, text="谱种").grid(row=1, column=0, sticky="w", pady=8)
    ttk.Combobox(options, textvariable=profile, values=list(PROFILES), state="readonly", width=20).grid(row=1, column=1, sticky="w", padx=8)
    ttk.Combobox(options, textvariable=device, values=["自动（优先GPU）", "CPU", "GPU"], state="readonly", width=23).grid(row=1, column=2)
    ttk.Label(options, text="文字语言").grid(row=2, column=0, sticky="w")
    ttk.Combobox(options, textvariable=language, values=["英文歌词", "中文／多语言"], state="readonly", width=20).grid(row=2, column=1, sticky="w", padx=8)
    lyrics_check = ttk.Checkbutton(options, text="导出歌词", variable=lyrics)
    lyrics_check.grid(row=2, column=2, sticky="w")
    structure = tk.StringVar(value=read_settings().get("score_structure", "SATB＋钢琴（5声部）"))
    ttk.Label(options, text="声部编制").grid(row=3, column=0, sticky="w", pady=8)
    structure_box = ttk.Combobox(options, textvariable=structure, values=list(PRESETS), state="readonly", width=26)
    structure_box.grid(row=3, column=1, sticky="w", padx=8)
    structure_note = ttk.Label(options)
    structure_note.grid(row=3, column=2, sticky="w")
    advanced = ttk.Frame(options)
    advanced.columnconfigure(1, weight=1)
    def toggle_advanced():
        if advanced.winfo_manager():
            advanced.grid_remove()
            advanced_button.configure(text="展开高级设置 ▾")
        else:
            advanced.grid(row=5, column=0, columnspan=3, sticky="ew", pady=8)
            advanced_button.configure(text="收起高级设置 ▴")
    advanced_button = ttk.Button(options, text="展开高级设置 ▾", command=toggle_advanced)
    advanced_button.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
    ttk.Label(advanced, text="声部提示").grid(row=0, column=0, sticky="w", pady=8)
    hint_entry = ttk.Entry(advanced, textvariable=hints)
    hint_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=8)

    def sync_profile(*_):
        active = PROFILES[profile.get()]
        single = active == SINGLE_STAFF_PROFILE
        lyrics_check.configure(state="disabled" if single or active == "piano" else "normal",
                               text="歌词已关闭" if single or active == "piano" else "导出歌词")
        structure_box.configure(state="disabled" if single else "readonly")
        hint_entry.configure(state="disabled" if single else "normal")
        structure_note.configure(text="一条五线谱，可保留同谱表和弦" if single else "按原谱选择；含独唱或打击乐时用自动／高级设置")

    profile.trace_add("write", sync_profile)
    sync_profile()
    ttk.Label(advanced, text="可留空自动识别；合唱示例：Soprano:1:lyrics; Alto:1:lyrics; Tenor:1:lyrics; Bass:1:lyrics").grid(row=1, column=0, columnspan=3, sticky="w")
    ttk.Label(advanced, text="起始拍号").grid(row=2, column=0, sticky="w", pady=(8, 0))
    ttk.Entry(advanced, textvariable=meter, width=10).grid(row=2, column=1, sticky="w", padx=8)
    ttk.Label(advanced, text="仅已知且原页未印时填写；不会覆盖识别到的变拍号").grid(row=2, column=2, sticky="w")
    ttk.Checkbutton(advanced, text="谱组间安全切半、约2倍放大；不锐化、不自动裁标题", variable=split).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
    ttk.Label(panel, text="识别结果需要对照原谱检查。当前 GPU 加速文字识别，音符识别主要使用 CPU。", foreground="#795200", wraplength=890).pack(anchor="w")
    toolbar = ttk.Frame(panel)
    toolbar.pack(fill="x", pady=10)
    events = queue.Queue()
    state = {"busy": False, "cancel": None, "result": None, "midi_running": False}
    progress = ttk.Progressbar(panel, mode="indeterminate")
    progress.pack(fill="x")
    tabs = ttk.Notebook(panel)
    tabs.pack(fill="both", expand=True, pady=(10, 0))
    log_frame = ttk.Frame(tabs)
    issue_frame = ttk.Frame(tabs)
    logs = tk.Text(log_frame, wrap="word", font=("Microsoft YaHei UI", 12), state="disabled")
    issues = ttk.Treeview(issue_frame, columns=("kind", "part", "measure", "detail"), show="headings")
    for column, title, width in [("kind", "疑点", 130), ("part", "声部", 130), ("measure", "小节", 60), ("detail", "说明", 530)]:
        issues.heading(column, text=title)
        issues.column(column, width=width)
    for frame, widget in ((log_frame, logs), (issue_frame, issues)):
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        widget.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
        bar.grid(row=0, column=1, sticky="ns")
        widget.configure(yscrollcommand=bar.set)
    horizontal = ttk.Scrollbar(issue_frame, orient="horizontal", command=issues.xview)
    horizontal.grid(row=1, column=0, sticky="ew")
    issues.configure(xscrollcommand=horizontal.set)
    tabs.add(log_frame, text="进度 / 结果")
    tabs.add(issue_frame, text="待核对疑点")

    def log(text):
        logs.configure(state="normal")
        logs.insert("end", text+"\n")
        logs.see("end")
        logs.configure(state="disabled")

    def begin(retry_args=None, resume=False):
        if state["busy"] or state["midi_running"]:
            return
        selected_source = retry_args.get("source") if retry_args is not None else source.get()
        if not selected_source or not Path(selected_source).is_file():
            messagebox.showinfo("选择原谱", "请先选择PDF。")
            return
        selected_profile = PROFILES[profile.get()]
        args = dict(source=source.get(), pages=pages.get(),
                    profile=selected_profile,
                    device={"自动（优先GPU）": "auto", "CPU": "cpu", "GPU": "gpu"}[device.get()],
                    language="en" if language.get() == "英文歌词" else "multi", split=split.get(),
                    lyrics=lyrics.get() and selected_profile != SINGLE_STAFF_PROFILE,
                    parts_hint=resolve_hint(selected_profile, structure.get(), hints.get()),
                    initial_meter=meter.get())
        if retry_args is not None:
            args = dict(retry_args)
        try:
            if not export_dir.get().strip():
                raise ValueError("请选择导出文件夹。")
            destination = Path(export_dir.get()).expanduser().resolve()
            destination.mkdir(parents=True, exist_ok=True)
            if not destination.is_dir():
                raise ValueError("导出位置必须是文件夹。")
            args["output_dir"] = str(destination)
            settings = read_settings(resolve_paths=False)
            settings["output_dir"] = str(destination)
            settings["score_structure"] = structure.get()
            (ROOT/"settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf8")
        except (OSError, ValueError) as exc:
            messagebox.showerror("导出位置不可用", str(exc))
            return
        state["last_args"] = dict(args)
        state.update(busy=True, cancel=threading.Event())
        start.configure(state="disabled")
        progress.start()
        log("开始新任务；新建独立工程，不覆盖原谱或旧结果。")
        def worker():
            try:
                result = process(**args, resume=resume, emit=lambda text: events.put(("log", text)), cancel=state["cancel"])
                events.put(("done", result))
            except Exception as exc:
                events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    start = ttk.Button(toolbar, text="开始识别", style="Primary.TButton", command=begin)
    start.pack(side="left")
    def retry_last():
        if state["busy"]:
            return
        if state.get("last_args"):
            begin(dict(state["last_args"]), resume=True)
            return
        settings = read_settings()
        jobs = Path(settings.get("jobs_root", ROOT / "jobs"))
        if not jobs.is_absolute():
            jobs = ROOT / jobs
        picked = filedialog.askopenfilename(title="选择上次任务的 manifest.json",
                                           initialdir=jobs, filetypes=[("任务记录", "manifest.json")])
        if not picked:
            return
        try:
            manifest = json.loads(Path(picked).read_text(encoding="utf8"))
            source_path = manifest.get("source_path")
            if not source_path or not Path(source_path).is_file():
                source_path = filedialog.askopenfilename(title="选择这次任务的原始 PDF",
                                                         filetypes=[("PDF", "*.pdf")])
            if source_path:
                begin(dict(manifest["options"], source=source_path, resume_manifest=picked), resume=True)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            messagebox.showerror("无法读取任务记录", str(exc))
    ttk.Button(toolbar, text="继续上次任务", command=retry_last).pack(side="left", padx=7)
    ttk.Button(toolbar, text="停止识别", command=lambda: state["cancel"].set() if state["cancel"] else None).pack(side="left", padx=7)
    def open_output():
        folder = Path(export_dir.get()).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)
    ttk.Button(toolbar, text="打开XML结果目录", command=open_output).pack(side="left")
    result_button = ttk.Button(toolbar, text="打开本次乐谱", state="disabled",
                               command=lambda: os.startfile(state["result"]["output"]) if state["result"] else None)
    result_button.pack(side="left", padx=7)

    def export_current_midi():
        if state["busy"] or state["midi_running"] or not state["result"]:
            return
        from midi_export import export_midi

        source_file = state["result"]["output"]
        state["midi_running"] = True
        midi_button.configure(state="disabled")
        log("正在导出本次识别的 MIDI")

        def export_background():
            try:
                events.put(("midi-done", export_midi(source_file)))
            except Exception as exc:
                events.put(("midi-error", str(exc)))

        threading.Thread(target=export_background, daemon=True).start()

    midi_button = ttk.Button(toolbar, text="导出本次 MIDI", state="disabled", command=export_current_midi)
    midi_button.pack(side="left")
    ttk.Button(advanced, text="打开工程和诊断日志", command=lambda: os.startfile(state["result"]["job"] if state["result"] else ROOT)).grid(row=4, column=0, columnspan=2, sticky="w", pady=8)

    def export_sibelius_drums():
        from sibelius_drum_export import export_sibelius_drum_copy

        try:
            target = export_sibelius_drum_copy(state["result"]["output"])
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法导出 Sibelius 副本", str(exc))
            return
        if target is None:
            messagebox.showinfo("Sibelius 打击乐副本", "本次结果无需转换鼓组键位。")
        else:
            log("Sibelius 打击乐副本：" + str(target))

    drum_copy_button = ttk.Button(
        advanced, text="导出 Sibelius 打击乐副本", state="disabled",
        command=export_sibelius_drums,
    )
    drum_copy_button.grid(row=4, column=2, sticky="e", pady=8)

    def export_sibelius_playback():
        if state["busy"] or not state["result"]:
            return
        from sibelius_playback_export import export_sibelius_playback_copy

        try:
            target = export_sibelius_playback_copy(state["result"]["output"])
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法导出 Sibelius 播放谱", str(exc))
            return
        if target is None:
            messagebox.showinfo("Sibelius 播放谱", "本次结果没有需要展开的双音震音或已验证的反复跳转。")
        else:
            log("Sibelius 播放谱已导出到：" + str(target))

    sibelius_playback_button = ttk.Button(
        advanced, text="导出 Sibelius 播放谱", state="disabled", command=export_sibelius_playback,
    )
    sibelius_playback_button.grid(row=5, column=2, sticky="e", pady=(0, 8))

    def poll():
        while True:
            try:
                kind, value = events.get_nowait()
            except queue.Empty:
                break
            if kind.startswith("midi-"):
                state["midi_running"] = False
                midi_button.configure(state="normal")
                if kind == "midi-done":
                    log("MIDI 已导出到：" + str(value))
                else:
                    messagebox.showerror("MIDI 导出失败", str(value))
            elif kind == "log":
                log(value)
            else:
                state["busy"] = False
                start.configure(state="normal")
                progress.stop()
                if kind == "error":
                    log("任务未完成："+value)
                    retry = messagebox.askretrycancel("任务未完成", value+"\n\n原谱和已有任务记录已保留。重试会用相同设置新建任务，不覆盖旧结果。是否重新识别？")
                    if retry and state.get("last_args"):
                        previous_args = dict(state["last_args"])
                        window.after(0, lambda args=previous_args: begin(args))
                else:
                    state["result"] = value
                    result_button.configure(state="normal")
                    drum_copy_button.configure(state="normal")
                    sibelius_playback_button.configure(state="normal")
                    midi_button.configure(state="normal")
                    log("XML检查稿："+value["output"])
                    log("仍需原谱音乐核对。日志及原始识别保存在："+value["job"])
                    for old in issues.get_children():
                        issues.delete(old)  # 仅删除界面行，不删除磁盘文件。
                    for issue in value["report"]["issues"]:
                        issues.insert("", "end", values=tuple(issue[k] for k in ("kind", "part", "measure", "detail")))
        window.after(150, poll)

    def close():
        if state["midi_running"]:
            window.after(400, close)
            return
        if state["busy"]:
            if not messagebox.askyesno("任务仍在运行", "停止本次识别并保留工程后关闭？"):
                return
            state["cancel"].set()
            window.after(400, wait_close)
        else:
            window.destroy()
    def wait_close():
        if state["busy"]:
            window.after(400, wait_close)
        else:
            window.destroy()
    window.protocol("WM_DELETE_WINDOW", close)
    log("建议先选1页测试，再处理整本。疑点检查不等于逐音准确率评估。")
    resize_text()
    window.bind("<Control-plus>", lambda event: resize_text(1))
    window.bind("<Control-equal>", lambda event: resize_text(1))
    window.bind("<Control-minus>", lambda event: resize_text(-1))
    window.after(150, poll)
    window.mainloop()
