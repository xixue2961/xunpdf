# XUNpdf 识谱

XUNpdf 在 Windows 本机把五线谱 PDF 转成可在 Sibelius 等制谱软件里继续编辑的 MusicXML，也可以生成 MIDI。界面有钢琴、合唱、通用和单声部无歌词模式。识别前可选页，任务中断后可以从匹配的符号识别断点继续。

程序先把 PDF 页面转成图片，再用 Audiveris-Bandscore 找音符、谱表和小节线，用 PaddleOCR 在本机识别文字。随后结合原谱位置复核歌词、节奏和记谱细节，最后导出文件。MIDI 由 Verovio 在本机生成。电脑可以只用 CPU，也可以用 GPU 做文字识别。安装依赖和首次下载模型需要联网，准备好本地模型后可以离线转换。引擎保留小节结构，Sibelius 导入后自动显示小节编号。

## Windows 源码运行

已知运行环境为 Windows x64、Python 3.12 和 Java 25。先准备 Python、Git 和 [Temurin JDK 25](https://adoptium.net/temurin/releases/?version=25)。以下命令从仓库根目录在 PowerShell 运行。

~~~powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item settings.example.json settings.json
~~~

从 [Audiveris-Bandscore v5.12.0-bandscore.1](https://github.com/hibikiwtnb/audiveris-bandscore/releases/tag/v5.12.0-bandscore.1) 下载 Windows x64 版。将解压目录放成下列结构。Java 可用绝对路径配置，修改 settings.json 的 java_relative 即可。自定义输出和任务目录也在该文件配置。

~~~text
runtime/engine/
  Audiveris-Bandscore/lib/
  java/bin/java.exe
  ocr-env/Scripts/python.exe
  cache/paddlex/official_models/
~~~

安装 CPU 文字识别依赖，然后预热模型。预热时可能下载数据，等待出现服务就绪信息，再按 Ctrl+C 退出。脚本中的服务只默认监听本机。

~~~powershell
py -3.12 -m venv runtime/engine/ocr-env
.\runtime\engine\ocr-env\Scripts\python.exe -m pip install -r requirements-ocr-cpu.txt
$env:PADDLE_PDX_CACHE_HOME = Join-Path $PWD 'runtime\engine\cache\paddlex'
.\runtime\engine\ocr-env\Scripts\python.exe ocr\paddle_ocr_server.py --device cpu
~~~

双击 Start-XUNpdf.cmd 打开界面。也可以用命令行识别单声部，或只检查已有 MusicXML 文件。MIDI 由界面单独导出，也可以运行 midi_export.py。

~~~powershell
.\.venv\Scripts\python.exe xunpdf.py --input '我的谱.pdf' --profile single-staff --device cpu
.\.venv\Scripts\python.exe xunpdf.py --audit '我的识谱结果.musicxml'
.\.venv\Scripts\python.exe midi_export.py '我的识谱结果.musicxml'
.\.venv\Scripts\python.exe -m unittest test_core
~~~

有兼容独显时，可在 runtime/engine/ocr-gpu-env 单独建立环境，按 [PaddlePaddle 安装文档](https://www.paddlepaddle.org.cn/install/quick) 选择与驱动相容的 GPU 运行库，再安装 requirements-ocr-cpu.txt 中其余依赖。界面可选自动回退 CPU。源码发布的 GPU 安装流程仍需在其他机器复验。

## 发布范围

本仓库包含程序源码、基础回归测试和现存的 Java 修补源码。默认运行说明使用公开版 Audiveris-Bandscore。开发机部分额外的原生引擎增强需要单独构建，本仓库还没有完整的自动构建流程，复杂谱的结果可能与开发机部署不同。校对时请重点检查复杂钢琴和弦、多声部节奏、重复跳转及歌词。结构检查可以定位疑点，无法覆盖所有漏音、错音和歌词错位。

Windows 便携包、Java 和 Python 运行环境、OCR 模型及个人乐谱由各自来源另行准备。任务日志留在本地 jobs 目录，导出文件默认存到 exports。

## 许可证

XUNpdf 采用 AGPL-3.0-or-later。第三方来源和许可见 [THIRD_PARTY.md](THIRD_PARTY.md)。
