"""On-device image generation (no API key, no quota, no watermark).

Uses a small distilled diffusion model (default: sd-turbo) on Apple GPU (MPS), CUDA, or CPU.
Optional: needs `pip install -r requirements-local.txt`. If torch/diffusers are missing,
`generate()` simply returns None and the bot falls back to its other paths.
"""
import asyncio
import io
import logging
import os

log = logging.getLogger("ali_ai.localgen")

ENABLED = os.getenv("IMAGE_LOCAL", "on").lower() != "off"
MODEL_ID = os.getenv("IMAGE_LOCAL_MODEL", "stabilityai/sd-turbo")
SIZE = int(os.getenv("IMAGE_LOCAL_SIZE", "512"))
STEPS = int(os.getenv("IMAGE_LOCAL_STEPS", "4"))

_pipe = None
_load_failed = False
_lock = asyncio.Lock()  # one generation at a time; the GPU/CPU is the bottleneck


def _load():
    global _pipe, _load_failed
    if _pipe is not None or _load_failed or not ENABLED:
        return _pipe
    try:
        import torch
        from diffusers import AutoPipelineForText2Image

        device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if device != "cpu" else torch.float32
        kwargs = {"variant": "fp16"} if device != "cpu" else {}
        log.info("Loading local image model %s on %s ...", MODEL_ID, device)
        pipe = AutoPipelineForText2Image.from_pretrained(MODEL_ID, torch_dtype=dtype, **kwargs)
        pipe.to(device)
        pipe.set_progress_bar_config(disable=True)
        _pipe = pipe
        log.info("Local image model ready.")
    except Exception:
        _load_failed = True
        log.exception("Local image generation unavailable")
    return _pipe


def _run(prompt: str) -> bytes | None:
    pipe = _load()
    if pipe is None:
        return None
    img = pipe(prompt, num_inference_steps=STEPS, guidance_scale=0.0, width=SIZE, height=SIZE).images[0]
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


async def preload() -> None:
    """Warm the model at startup so the first /image is fast."""
    if ENABLED:
        await asyncio.to_thread(_load)


async def generate(prompt: str) -> bytes | None:
    if not ENABLED or _load_failed:
        return None
    async with _lock:
        try:
            return await asyncio.to_thread(_run, prompt)
        except Exception:
            log.exception("Local image generation failed")
            return None
