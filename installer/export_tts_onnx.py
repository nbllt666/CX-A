# -*- coding: utf-8 -*-
"""TTS 引擎 ONNX 资产导出工具（开发机运行；不随包分发）。

依据：``.trae/documents/20261001_模块0_TTS引擎ORT落地.md``（§2.4）。
产物：``installer/bundled/tts_onnx/{enc_p,flow,dp,dec}.onnx``——
随打包链由 ``build.py::assemble`` 组装到 ``runtime/voice_bridge/tts_onnx/``
（安装器链经 iss ``{#PayloadDir}\\*`` 原样透传）。

导出口径（与实验验证一致，勿改动）：
- 默认 checkpoint（MeloTTS 官方中文模型——与产品 ``cx-open`` 默认链路一致）；
- weight_norm 等价折叠（remove_weight_norm）后导出；
- 位置参数直传（张量作为 ONNX 输入；python 标量固化为常量）；
- 动态轴：文本维 T（enc_p）/ 音频潜变量维 T_out（flow/dec）；
- 兼容修正：IR>11 → 11；opset>23 → 17（ORT 1.23 上限）；
- sdp 不导出（实验结论：其 ORT 化为负收益，保持 torch 原样）。

运行（sidecar 环境；cwd 任意）：
    c:\\CX-A\\runtime\\voice\\python.exe installer\\export_tts_onnx.py
"""

import hashlib
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(HERE, "bundled", "tts_onnx")

os.environ.setdefault("HF_HOME", os.path.join(PROJECT_ROOT, "data", "hf_cache"))
os.environ.setdefault("NLTK_DATA", os.path.join(PROJECT_ROOT, "data", "nltk_data"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import torch  # noqa: E402

_orig_load = torch.load


def _load(*args, **kwargs):
    """放宽 weights_only（MeloTTS 检查点含非 tensor 对象）。"""
    kwargs.setdefault("weights_only", False)
    return _orig_load(*args, **kwargs)


torch.load = _load

import onnx  # noqa: E402
from melo.api import TTS  # noqa: E402
from melo.models import (  # noqa: E402
    DurationPredictor,
    Generator,
    TextEncoder,
    TransformerCouplingBlock,
)

#: 捕获输入用文本（长度分布 5~13 字，覆盖典型句子）。
CAPTURE_TEXTS = ["你好呀，", "今天过得还不错呢。", "早上醒来时阳光正好。"]


def log(message):
    """统一日志（时间戳 + [INFO]）。"""
    print(f"[{time.strftime('%H:%M:%S')}] [INFO] {message}", flush=True)


def strip_weight_norm(module):
    """移除 legacy weight_norm（等价折叠，无损）——ONNX 导出前置。"""
    from torch.nn.utils import remove_weight_norm

    removed = 0
    for sub in module.modules():
        if hasattr(sub, "weight_g"):
            remove_weight_norm(sub)
            removed += 1
    return removed


def capture_inputs(engine, texts):
    """跑若干文本合成，捕获目标子模块首次调用的 (args, kwargs)。"""
    captures = {}
    patches = []

    def wrap(cls, key):
        orig = cls.forward

        def wrapped(self, *args, **kwargs):
            if key not in captures:
                captures[key] = (
                    tuple(a.detach().clone() if torch.is_tensor(a) else a for a in args),
                    {k: (v.detach().clone() if torch.is_tensor(v) else v) for k, v in kwargs.items()},
                )
            return orig(self, *args, **kwargs)

        patches.append((cls, orig))
        cls.forward = wrapped

    wrap(TextEncoder, "enc_p")
    wrap(TransformerCouplingBlock, "flow")
    wrap(DurationPredictor, "dp")
    wrap(Generator, "dec")
    try:
        for text in texts:
            engine.tts_to_file(text, 0, output_path=None, speed=1.0, quiet=True)
    finally:
        for cls, orig in patches:
            cls.forward = orig
    return captures


def export_args(captured):
    """把捕获的 (args, kwargs) 整理为按签名顺序的位置参数（标量固化为常量）。"""
    args, kwargs = captured
    return tuple(list(args) + list(kwargs.values()))


def export_module(module, args, path, input_names, output_names, dynamic_axes):
    """torch.onnx.export（trace 路径，位置参数直传）+ IR/opset 兼容修正。"""
    torch.onnx.export(
        module,
        tuple(args),
        path,
        input_names=input_names,
        output_names=output_names,
        dynamic_axes=dynamic_axes,
        opset_version=17,
        dynamo=False,
    )
    model = onnx.load(path)
    changed = []
    if model.ir_version > 11:
        changed.append(f"ir{model.ir_version}->11")
        model.ir_version = 11
    for opset in model.opset_import:
        if opset.domain in ("", "ai.onnx") and opset.version > 23:
            changed.append(f"opset{opset.version}->17")
            opset.version = 17
    if changed:
        onnx.save(model, path)
    return changed


def sha256_of(path):
    """文件 sha256（十六进制，资产校验用）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    log(f"导出目标目录：{OUT_DIR}")
    log(f"torch={torch.__version__} onnx={onnx.__version__}")

    t0 = time.perf_counter()
    engine = TTS(language="ZH", device="cpu", use_hf=True)
    engine.eval()
    log(f"引擎加载完成（{time.perf_counter() - t0:.1f}s；默认 checkpoint）")

    removed = strip_weight_norm(engine.model)
    log(f"weight_norm 折叠 {removed} 处（数学等价）")

    captures = capture_inputs(engine, CAPTURE_TEXTS)
    log(f"输入捕获完成：{sorted(captures.keys())}")

    specs = (
        (
            "enc_p",
            engine.model.enc_p,
            ["x", "x_lengths", "tone", "language", "bert", "ja_bert", "g"],
            ["x_out", "m_p", "logs_p", "x_mask_out"],
            {"x": {1: "T"}, "tone": {1: "T"}, "language": {1: "T"},
             "bert": {2: "T"}, "ja_bert": {2: "T"},
             "x_out": {2: "T"}, "m_p": {2: "T"}, "logs_p": {2: "T"}, "x_mask_out": {2: "T"}},
        ),
        (
            "flow",
            engine.model.flow,
            ["x", "x_mask", "g"], ["z_out"],
            {"x": {2: "T"}, "x_mask": {2: "T"}, "z_out": {2: "T"}},
        ),
        (
            "dp",
            engine.model.dp,
            ["x", "x_mask", "g"], ["logw"],
            {"x": {2: "T"}, "x_mask": {2: "T"}, "logw": {2: "T"}},
        ),
        (
            "dec",
            engine.model.dec,
            ["z", "g"], ["audio"],
            {"z": {2: "T"}, "audio": {2: "T_out"}},
        ),
    )

    manifest_lines = []
    for name, module, input_names, output_names, dynamic_axes in specs:
        args = export_args(captures[name])
        path = os.path.join(OUT_DIR, f"{name}.onnx")
        t0 = time.perf_counter()
        changed = export_module(module, args, path, input_names, output_names, dynamic_axes)
        size_mb = os.path.getsize(path) / 1024 / 1024
        digest = sha256_of(path)
        log(f"{name}.onnx 导出完成（{size_mb:.1f} MB，{time.perf_counter() - t0:.1f}s，"
            f"兼容修正 {changed or '无'}）")
        manifest_lines.append(f"{name}: size={size_mb:.1f}MB sha256={digest}")

    missing = [n for n, *_ in specs if not os.path.isfile(os.path.join(OUT_DIR, f"{n}.onnx"))]
    if missing:
        log(f"[ERROR] 产物缺失：{missing}")
        return 1
    log("全部资产导出成功。清单：")
    for line in manifest_lines:
        print("  " + line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())