# Vision

ABP sees: objects, faces, text, QR codes and barcodes, people, colours and shapes in any image or on the screen; where a
piece of text or a button is; what changed between two images; and image edits. It runs on this machine with OpenCV
(with contrib) and OpenCV's model zoo, so no image leaves it.

| Where | What |
|---|---|
| Agents | `vision_analyze`, `vision_find`, `vision_compare`, `vision_edit`, `vision_faces`, `vision_status`; `vision_capture` takes a camera frame and always asks first |
| Dashboard | the Vision page: Analyze, Find, Compare, Edit, Models & device |
| CLI | `abp vision status \| analyze <image> \| find --text ... \| compare <a> <b> \| edit <image> <steps> \| fetch <model>` |
| MCP | ABP's MCP server: `vision_analyze`, `vision_find`, `vision_compare`, `vision_edit` |
| HTTP | `/api/vision/*` (bot/dashboard/vision_api.py) |

An image is a file path, an http(s) URL (public addresses only), a `data:` URL, `screen` / `screen:<n>`, or, from the
page and `vision_capture` only, `camera` / `camera:<n>`. Results with a picture (boxes drawn, changes outlined) are saved
in `data/vision/out`. On a screenshot every match also has `screen_center`, the position to click.

## Models

The zoo's models are fetched on first use into `data/vision/models` and checked against their SHA-256 (a file that does
not match is not used). When the opencv-zoo module's checkout has a model (after its `models.fetch`), it is copied from
there. Each model's license is on the Models tab.

| Task | Model | Notes |
|---|---|---|
| faces | YuNet (MIT) + SFace (Apache-2.0) | boxes, five landmarks; "same person" by cosine similarity (the zoo's 0.363) |
| objects | YOLOX-S (Apache-2.0) | COCO's 80 classes |
| text | PP-OCRv3 detection + CRNN (Apache-2.0) | Latin letters, digits and punctuation; lines are split into words so long lines read correctly; each line has a confidence, and lines under 0.6 (usually another script) are left out of `text_joined` |
| codes | WeChat's QR decoder, OpenCV's barcode reader | OpenCV 5 dropped Caffe, so WeChat's CNN detector files cannot load; its decoder runs without them |
| people | PP-HumanSeg (Apache-2.0) | the share of pixels that are people, and their regions |

## Device

`vision.device` in config: `auto` (default), `cpu`, `opencl`, `opencl_fp16`. `auto` is the CPU with the faster of
OpenCV 5's two DNN engines per model: measured here, YOLOX-S takes 81 ms on the new engine and CRNN 14 ms on the
classic one. The GPU is used only when asked for: the new engine ignores device targets, and the wheel's classic OpenCL
kernels do not all compile on AMD's driver and ran slower than the CPU. A faster GPU path is roadmap item CV-F.

## OpenCV's own repos

The 14 repos of github.com/opencv are modules too (catalog overlays, `catalog/opencv*`): building OpenCV from source,
its tests and data, wheels, the benchmarks (cvbenchmark, opencv_benchmarks, the zoo's, COOL-Benchmark on cloud machines),
ONNX conformance, bin picking, vision capsules, and the Jetson and iOS samples. Their checkouts are left exactly as
upstream has them. See docs/modules/ROADMAP.md §5.11.
