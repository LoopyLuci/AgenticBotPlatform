"""Which device and DNN engine run vision's models. config: vision.device = auto | cpu | opencl | opencl_fp16.

auto is the CPU, with the faster of OpenCV 5's two engines for each model. Measured on this project's machine
(Ryzen + Radeon RX 7900 XTX, opencv-contrib-python-headless 5.0.0, 2026-09-30): YOLOX-S 81 ms on the new engine vs
152 ms on the classic one; CRNN 14 ms on the classic engine vs 42 ms on the new one. The new engine ignores device
targets, and the wheel's classic OpenCL kernels do not all compile on AMD's driver (its "activations" kernel uses
std::pow) and ran slower than the CPU (YOLOX 126 ms), so the GPU is used only when asked for (opencl), through the
classic engine. A model that fails on the GPU is retried on the CPU once and stays there. A faster GPU path is
CV-F's job (an OpenCV built here, or ONNX Runtime with a GPU execution provider)."""
from __future__ import annotations

import threading

import cv2

_TARGETS = {"cpu": cv2.dnn.DNN_TARGET_CPU, "opencl": cv2.dnn.DNN_TARGET_OPENCL,
            "opencl_fp16": cv2.dnn.DNN_TARGET_OPENCL_FP16}
# the faster engine on the CPU, per model (see above); the rest use ENGINE_AUTO (the new engine)
_CPU_ENGINE = {"text_recognize": cv2.dnn.ENGINE_CLASSIC}
_cpu_only: set[str] = set()                  # models that failed on the GPU in this process
_lock = threading.Lock()


def configured() -> str:
    try:
        from bot.config import config
        v = str(((config.current or {}).get("vision") or {}).get("device") or "auto").lower()
    except Exception:  # noqa: BLE001
        v = "auto"
    return v if v in ("auto", *_TARGETS) else "auto"


def gpu_available() -> bool:
    try:
        if not cv2.ocl.haveOpenCL():
            return False
        dev = cv2.ocl.Device.getDefault()
        return bool(dev.available()) and dev.type() & 4 != 0          # CL_DEVICE_TYPE_GPU
    except cv2.error:
        return False


def target(model_key: str = "") -> int:
    want = configured()
    if model_key in _cpu_only or want in ("auto", "cpu") or not gpu_available():
        return _TARGETS["cpu"]
    return _TARGETS[want]


def engine(model_key: str = "") -> int:
    if target(model_key) != _TARGETS["cpu"]:
        return cv2.dnn.ENGINE_CLASSIC                # the only engine that honours a GPU target
    return _CPU_ENGINE.get(model_key, cv2.dnn.ENGINE_AUTO)


def read_net(path: str, model_key: str) -> "cv2.dnn.Net":
    net = cv2.dnn.readNet(path, "", "", engine(model_key))
    net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
    if target(model_key) != _TARGETS["cpu"]:
        net.setPreferableTarget(target(model_key))
    return net


def mark_cpu_only(model_key: str) -> None:
    with _lock:
        _cpu_only.add(model_key)


def status() -> dict:
    dev = ""
    try:
        dev = cv2.ocl.Device.getDefault().name() if gpu_available() else ""
    except cv2.error:
        pass
    return {"configured": configured(), "running_on": "GPU (OpenCL)" if configured().startswith("opencl") and dev
            else "CPU", "gpu": dev or None, "opencv": cv2.__version__, "cpu_only_models": sorted(_cpu_only),
            "choices": ["auto", *_TARGETS]}
