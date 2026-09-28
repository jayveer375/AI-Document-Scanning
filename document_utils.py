import os
import re
import json
import base64
import cv2
import numpy as np

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "pdf"}


def allowed_file(filename):
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def get_mime_type(filename):
    ext = filename.rsplit(".", 1)[1].lower()
    if ext == "jpg":
        ext = "jpeg"
    return f"image/{ext}"


def convert_document_to_images(file_path, max_pages=3):
    """Render PDF pages to PNG images with quality validation."""
    ext = file_path.rsplit(".", 1)[1].lower()
    if ext != "pdf":
        return [file_path]

    import fitz

    try:
        pdf_document = fitz.open(file_path)
        if pdf_document.page_count == 0:
            raise RuntimeError("The uploaded PDF contains no pages.")

        pages_to_render = min(max_pages, pdf_document.page_count)
        rendered_paths = []
        base_name = file_path.rsplit(".", 1)[0]

        for page_idx in range(pages_to_render):
            page = pdf_document[page_idx]
            pixmap = page.get_pixmap(dpi=150)
            page_image_path = f"{base_name}_page_{page_idx + 1}.png"
            pixmap.save(page_image_path)
            rendered_paths.append(page_image_path)

        pdf_document.close()
        return rendered_paths
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not convert uploaded PDF: {exc}")


def extract_json_from_text(text):
    """Extract and parse first JSON object or array from LLM response text."""
    if not text:
        raise ValueError("Empty response text from AI.")

    # Strip code block markers if present
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)

    match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
    if not match:
        raise ValueError("No valid JSON structure found in AI response.")

    return json.loads(match.group(0))


def assess_image_quality(img_path):
    """
    Checks resolution, sharpness (Laplacian variance), and brightness/contrast
    before sending to AI.
    """
    img = cv2.imread(img_path)
    if img is None:
        return {"status": "ERROR", "passed": False, "reason": "Could not read image file."}

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    if w < 300 or h < 300:
        return {
            "status": "REJECT",
            "passed": False,
            "reason": f"Image resolution is too low ({w}x{h} px). Minimum 300x300 px required.",
        }

    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(np.mean(gray))

    if sharpness < 20.0:
        return {
            "status": "REJECT",
            "passed": False,
            "reason": f"Scan is too blurry for reliable character recognition (Sharpness: {sharpness:.1f}).",
        }

    if brightness < 20.0:
        return {
            "status": "REJECT",
            "passed": False,
            "reason": f"Scan is severely underexposed/too dark (Brightness: {brightness:.1f}).",
        }

    return {
        "status": "PASS",
        "passed": True,
        "sharpness": round(sharpness, 1),
        "resolution": f"{w}x{h}",
        "brightness": round(brightness, 1),
    }
