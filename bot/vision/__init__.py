"""Computer vision for every agent, built into ABP (OpenCV, with contrib, and OpenCV's model zoo).

    images     where an image comes from (a file, a URL, a data: URL, the screen, a camera) and where results go
    ops        image operations that need no model: info, resize/crop/rotate, colour, blur, edges, threshold, contours,
               colours, histogram, template and feature matching, before/after difference
    zoo        the model zoo's models, downloaded on demand into data/vision/models and verified (SHA-256)
    dnn        which device runs the models (OpenCL on the GPU when there is one, else the CPU)
    pipelines  what the models do: faces (YuNet + SFace), objects (YOLOX, COCO's 80 classes), text (PP-OCRv3 +
               CRNN), QR codes and barcodes (WeChatQRCode, OpenCV's barcode reader), people (human segmentation)
    tools      the vision_* tools ABP's agents use
    api        /api/vision/* for the dashboard, the CLI and MCP

Nothing here needs an OpenCV module to be running: the modules (catalog/opencv*) are OpenCV's own repos for building,
testing and benchmarking; this is vision as a capability ABP always has.
"""
