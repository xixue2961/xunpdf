# 第三方来源

- [Audiveris-Bandscore](https://github.com/hibikiwtnb/audiveris-bandscore/tree/bc314e8503b427e3964b4cb543d2ece4be0938e4) 是 XUNpdf 调用的本地乐谱识别引擎，基于 [Audiveris](https://github.com/Audiveris/audiveris)。上游源代码和原始版权声明适用于相关 Java 修补和文字服务源码。XUNpdf 对文字服务的改动包括本地模型校验及 Windows 路径兼容处理。源代码参见 [本仓库 OCR 服务](ocr/paddle_ocr_server.py) 和 [上游原始服务](https://github.com/hibikiwtnb/audiveris-bandscore/blob/bc314e8503b427e3964b4cb543d2ece4be0938e4/dev/paddleocr/paddle_ocr_server.py)。
- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)、[PaddleX](https://github.com/PaddlePaddle/PaddleX) 和 [PaddlePaddle](https://github.com/PaddlePaddle/Paddle) 提供文字检测及识别，依赖项目采用各自的 Apache-2.0 许可。
- [Verovio](https://github.com/rism-digital/verovio/tree/version-6.4.1) 供 MIDI 生成，按上游 LGPL-3.0 发布。
- [Pillow](https://github.com/python-pillow/Pillow)、[NumPy](https://github.com/numpy/numpy)、[pypdfium2](https://github.com/pypdfium2-team/pypdfium2) 和 [OpenCV](https://github.com/opencv/opencv) 通过 Python 包管理器独立安装，适用各包自身许可。
- 可选的 [homr](https://github.com/liebharc/homr) 供开发中的符头复核，默认源码运行流程无需安装。仓库里的 worker 是接入代码。
