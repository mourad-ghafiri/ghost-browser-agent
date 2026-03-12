"""Screenshot decode and resize (preserving aspect ratio)."""

import base64
import io

from PIL import Image


# LLM gets a resized image (balance between detail and tokens)
LLM_MAX_WIDTH = 1024
LLM_MAX_HEIGHT = 768


def process_screenshot(b64_png: str) -> tuple[str, str]:
    """Decode base64 PNG, return (llm_b64, full_b64).

    llm_b64:  resized JPEG for the LLM (512x384)
    full_b64: full-size PNG for saving to disk / Telegram
    """
    img_data = base64.b64decode(b64_png)
    img = Image.open(io.BytesIO(img_data)).convert("RGB")

    # Full-size PNG for scenarios/Telegram
    full_buf = io.BytesIO()
    img.save(full_buf, format="PNG")
    full_b64 = base64.b64encode(full_buf.getvalue()).decode()

    # Resized for LLM
    llm_img = img.copy()
    llm_img.thumbnail((LLM_MAX_WIDTH, LLM_MAX_HEIGHT), Image.LANCZOS)
    llm_buf = io.BytesIO()
    llm_img.save(llm_buf, format="JPEG", quality=75)
    llm_b64 = base64.b64encode(llm_buf.getvalue()).decode()

    return llm_b64, full_b64
