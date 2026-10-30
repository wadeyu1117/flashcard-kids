#!/usr/bin/env python3
"""
Google Drive Sync & Smart Card Cropper for Kids Flashcard App.
Syncs photos and MP3s from Google Drive folder 1lZgQD2gynBy4saiiZObBVLOnY8vjxfDP,
pairs front/back flashcard sheet photos, refines/detects individual white cards,
crops individual card images, and builds data/catalog.json.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import cv2
import numpy as np

sys.path.insert(0, "/usr/local/google/home/wadeyu/.gemini/config/skills/gki_test_workflow/scripts")
from upload_mtt_and_time import get_gcloud_mtls_session

FOLDER_ID = "1lZgQD2gynBy4saiiZObBVLOnY8vjxfDP"
APP_DIR = "/usr/local/google/home/wadeyu/web_apps/flashcard_kids"
DATA_DIR = os.path.join(APP_DIR, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
CARDS_DIR = os.path.join(DATA_DIR, "cards")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")
TTS_DIR = os.path.join(DATA_DIR, "tts")
MANIFEST_PATH = os.path.join(DATA_DIR, "drive_manifest.json")
ANNOTATIONS_PATH = os.path.join(DATA_DIR, "vision_annotations.json")
CATALOG_PATH = os.path.join(DATA_DIR, "catalog.json")


def ensure_dirs():
    for d in (DATA_DIR, RAW_DIR, CARDS_DIR, AUDIO_DIR, TTS_DIR):
        os.makedirs(d, exist_ok=True)


def word_to_tts_slug(word: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", "_", word.strip().lower()).strip("_")
    if not clean:
        clean = "word"
    h = hashlib.md5(word.strip().lower().encode("utf-8")).hexdigest()[:6]
    return f"{clean}_{h}"


def ensure_tts_files(words):
    """Downloads normal and slow MP3 pronunciation files for all flashcard words."""
    tasks = []
    for w in sorted(set(w.strip() for w in words if w and w.strip())):
        slug = word_to_tts_slug(w)
        q_text = w.replace("/", ", ").strip()
        normal_path = os.path.join(TTS_DIR, f"{slug}.mp3")
        slow_path = os.path.join(TTS_DIR, f"{slug}_slow.mp3")
        if not os.path.exists(normal_path) or os.path.getsize(normal_path) < 500:
            tasks.append((q_text, "1", normal_path))
        if not os.path.exists(slow_path) or os.path.getsize(slow_path) < 500:
            tasks.append((q_text, "0.24", slow_path))

    if not tasks:
        return 0

    def _fetch_tts(item):
        q_text, spd, out_path = item
        url = (
            "https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=en"
            f"&ttsspeed={spd}&q={urllib.parse.quote(q_text)}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        if data and len(data) > 500:
            with open(out_path, "wb") as f:
                f.write(data)
            return True
        return False

    ok_count = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(_fetch_tts, t) for t in tasks]
        for fut in as_completed(futs):
            try:
                if fut.result():
                    ok_count += 1
            except Exception as e:
                print(f"[TTS] Warning: {e}")
    return ok_count


def fetch_drive_manifest():
    """Fetches all non-trashed files from the target Google Drive folder."""
    session = get_gcloud_mtls_session()
    files = []
    page_token = None
    while True:
        params = {
            "q": f"'{FOLDER_ID}' in parents and trashed = false",
            "fields": "nextPageToken, files(id, name, mimeType, createdTime, modifiedTime, size, imageMediaMetadata)",
            "pageSize": 100,
            "orderBy": "createdTime asc",
        }
        if page_token:
            params["pageToken"] = page_token
        resp = session.get("https://www.mtls.googleapis.com/drive/v3/files", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        files.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(files, f, indent=2, ensure_ascii=False)
    return files


def download_missing_files(files):
    """Downloads any new image or audio files from Google Drive in parallel."""
    def _dl(item):
        fid = item["id"]
        name = item["name"]
        mime = item.get("mimeType", "")
        if "audio" in mime or name.lower().endswith(".mp3"):
            safe_name = f"{fid}_{name}"
            out_path = os.path.join(AUDIO_DIR, safe_name)
        else:
            ext = os.path.splitext(name)[1] or ".jpg"
            safe_name = f"{fid}{ext}"
            out_path = os.path.join(RAW_DIR, safe_name)

        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            return fid, out_path, False

        s = get_gcloud_mtls_session()
        r = s.get(f"https://www.mtls.googleapis.com/drive/v3/files/{fid}?alt=media", timeout=60)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)
        return fid, out_path, True

    downloaded_count = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_dl, item) for item in files]
        for fut in as_completed(futs):
            _, _, is_new = fut.result()
            if is_new:
                downloaded_count += 1
    return downloaded_count


def refine_card_box_opencv(img, box_norm):
    """
    Given an image and a normalized box [ymin, xmin, ymax, xmax] (0..1000),
    snaps or pads the box cleanly so the cropped card is crisp and complete.
    """
    h, w = img.shape[:2]
    ymin, xmin, ymax, xmax = box_norm
    # Add a small 1.2% safety margin so handwritten text near edges is never clipped
    pad_y = int(round((ymax - ymin) * 0.02))
    pad_x = int(round((xmax - xmin) * 0.02))
    ymin = max(0, ymin - pad_y)
    xmin = max(0, xmin - pad_x)
    ymax = min(1000, ymax + pad_y)
    xmax = min(1000, xmax + pad_x)

    y0 = max(0, min(h - 2, int(round(ymin * h / 1000.0))))
    x0 = max(0, min(w - 2, int(round(xmin * w / 1000.0))))
    y1 = max(y0 + 2, min(h, int(round(ymax * h / 1000.0))))
    x1 = max(x0 + 2, min(w, int(round(xmax * w / 1000.0))))
    return x0, y0, x1, y1, [ymin, xmin, ymax, xmax]


def classify_pic_vs_word_fallback(img_a, img_b):
    """
    Fallback classifier for brand-new photo pairs not yet in vision_annotations.json:
    Picture cards have higher color saturation variance than handwritten black-on-white word cards.
    """
    def _sat_score(img):
        hsv = cv2.cvtColor(cv2.resize(img, (400, 400)), cv2.COLOR_BGR2HSV)
        # Look at pixels that are not wooden desk
        s = hsv[:, :, 1].astype(np.float32)
        v = hsv[:, :, 2].astype(np.float32)
        return float(np.std(s) + np.percentile(s, 85) * 0.5 - np.mean(v > 200) * 20)

    if _sat_score(img_a) >= _sat_score(img_b):
        return "a", "b"
    return "b", "a"


def auto_detect_cards_fallback(img):
    """
    Fallback OpenCV card detector for brand-new photo pairs uploaded in the future.
    Returns list of [ymin, xmin, ymax, xmax] in 0..1000 normalized coordinates.
    """
    h, w = img.shape[:2]
    scale = 1000.0 / w
    sh, sw = int(round(h * scale)), 1000
    small = cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA)

    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    L = lab[:, :, 0].astype(np.float32)
    B = lab[:, :, 2].astype(np.float32)
    S = hsv[:, :, 1].astype(np.float32)
    woodness = S * 0.7 + np.maximum(0, B - 128) * 1.8 - (L - 128) * 0.5
    wood_u8 = np.clip(woodness + 50, 0, 255).astype(np.uint8)
    _, mask_not_wood = cv2.threshold(wood_u8, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    eroded = cv2.erode(mask_not_wood, np.ones((7, 7), np.uint8), iterations=1)
    cnts, _ = cv2.findContours(eroded, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    img_area = sw * sh
    for c in cnts:
        x, y, bw, bh = cv2.boundingRect(c)
        if bw * bh < img_area * 0.03:
            continue
        ymin = int(round(y * 1000.0 / sh))
        xmin = int(round(x * 1000.0 / sw))
        ymax = int(round((y + bh) * 1000.0 / sh))
        xmax = int(round((x + bw) * 1000.0 / sw))
        boxes.append([ymin, xmin, ymax, xmax])

    # Sort row-by-row (top to bottom, left to right)
    boxes.sort(key=lambda b: (b[0] // 180, b[1]))
    return boxes


def extract_date_info(item_a, item_b):
    """Extracts human-readable date (YYYY-MM-DD) from EXIF or filename."""
    for it in (item_a, item_b):
        exif_t = it.get("imageMediaMetadata", {}).get("time", "")
        if exif_t and len(exif_t) >= 10:
            return exif_t[:10].replace(":", "-")
    for it in (item_a, item_b):
        nm = it.get("name", "")
        if nm.startswith("PXL_") and len(nm) >= 12 and nm[4:12].isdigit():
            d = nm[4:12]
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        # Check millisecond epoch filename like 1760529895946.jpg
        stem = os.path.splitext(nm)[0].split("~")[0]
        if len(stem) == 13 and stem.isdigit():
            try:
                ts = int(stem) / 1000.0
                # Some filenames have shifted timestamps, only use if reasonable year
                dt = datetime.fromtimestamp(ts)
                if 2024 <= dt.year <= 2027:
                    return dt.strftime("%Y-%m-%d")
            except Exception:
                pass
    return item_a.get("createdTime", "")[:10]


def build_catalog(sync_from_drive=True):
    """Main pipeline: optionally syncs from Drive, crops cards, and writes catalog.json."""
    ensure_dirs()
    new_downloads = 0
    if sync_from_drive:
        files = fetch_drive_manifest()
        new_downloads = download_missing_files(files)
    else:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            files = json.load(f)

    annotations_by_pair = {}
    annotations_by_file_id = {}
    if os.path.exists(ANNOTATIONS_PATH):
        with open(ANNOTATIONS_PATH, "r", encoding="utf-8") as f:
            ann_list = json.load(f)
        for ann in ann_list:
            annotations_by_pair[ann["pair_num"]] = ann
            annotations_by_file_id[ann["pic_file_id"]] = ann
            annotations_by_file_id[ann["word_file_id"]] = ann

    # Separate audio files and image files
    audio_tracks = []
    img_files = []
    for item in files:
        mime = item.get("mimeType", "")
        name = item.get("name", "")
        if "audio" in mime or name.lower().endswith(".mp3"):
            safe_name = f"{item['id']}_{name}"
            # Friendly title
            clean_title = os.path.splitext(name)[0].replace("_", " ").strip()
            audio_tracks.append({
                "id": item["id"],
                "name": name,
                "title": clean_title,
                "url": f"data/audio/{safe_name}",
                "created_time": item.get("createdTime", ""),
            })
        elif "image" in mime or name.lower().endswith((".jpg", ".jpeg", ".png")):
            img_files.append(item)

    decks = []
    total_cards = 0

    for idx in range(0, len(img_files) - 1, 2):
        item_a = img_files[idx]
        item_b = img_files[idx + 1]
        pair_num = idx // 2 + 1
        deck_id = f"deck_{pair_num:02d}"
        date_str = extract_date_info(item_a, item_b)

        ann = (
            annotations_by_file_id.get(item_a["id"])
            or annotations_by_file_id.get(item_b["id"])
            or annotations_by_pair.get(pair_num)
        )

        path_a = os.path.join(RAW_DIR, f"{item_a['id']}.jpg")
        path_b = os.path.join(RAW_DIR, f"{item_b['id']}.jpg")
        img_a = cv2.imread(path_a)
        img_b = cv2.imread(path_b)
        if img_a is None or img_b is None:
            continue

        if ann:
            pic_id = ann["pic_file_id"]
            word_id = ann["word_file_id"]
            if pic_id == item_a["id"]:
                pic_img, word_img = img_a, img_b
            else:
                pic_img, word_img = img_b, img_a
                pic_id, word_id = item_b["id"], item_a["id"]
            topic_en = ann.get("topic_en", f"Unit {pair_num}")
            topic_zh = ann.get("topic_zh", f"第 {pair_num} 單元")
            raw_cards = ann.get("cards", [])
        else:
            which_pic, _ = classify_pic_vs_word_fallback(img_a, img_b)
            if which_pic == "a":
                pic_id, word_id = item_a["id"], item_b["id"]
                pic_img, word_img = img_a, img_b
            else:
                pic_id, word_id = item_b["id"], item_a["id"]
                pic_img, word_img = img_b, img_a
            topic_en = f"New Unit ({date_str})"
            topic_zh = f"最新字卡 ({date_str})"
            boxes_p = auto_detect_cards_fallback(pic_img)
            boxes_w = auto_detect_cards_fallback(word_img)
            n_cards = min(len(boxes_p), len(boxes_w))
            raw_cards = [
                {
                    "word": f"Card #{k + 1}",
                    "subtext": "",
                    "zh": "新同步字卡",
                    "pic_box": boxes_p[k],
                    "word_box": boxes_w[k],
                }
                for k in range(n_cards)
            ]

        ph, pw = pic_img.shape[:2]
        wh, ww = word_img.shape[:2]
        deck_cards = []

        for c_idx, c in enumerate(raw_cards, start=1):
            card_id = f"{deck_id}_card_{c_idx:02d}"
            pic_crop_name = f"{card_id}_pic.jpg"
            word_crop_name = f"{card_id}_word.jpg"
            pic_crop_path = os.path.join(CARDS_DIR, pic_crop_name)
            word_crop_path = os.path.join(CARDS_DIR, word_crop_name)

            px0, py0, px1, py1, p_box_norm = refine_card_box_opencv(pic_img, c["pic_box"])
            wx0, wy0, wx1, wy1, w_box_norm = refine_card_box_opencv(word_img, c["word_box"])

            if not os.path.exists(pic_crop_path) or new_downloads > 0:
                crop_p = pic_img[py0:py1, px0:px1]
                cv2.imwrite(pic_crop_path, crop_p, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

            if not os.path.exists(word_crop_path) or new_downloads > 0:
                crop_w = word_img[wy0:wy1, wx0:wx1]
                cv2.imwrite(word_crop_path, crop_w, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

            w_str = c.get("word", "").strip()
            tts_slug = word_to_tts_slug(w_str)
            deck_cards.append({
                "id": card_id,
                "index": c_idx,
                "word": w_str,
                "subtext": c.get("subtext", "").strip(),
                "zh": c.get("zh", "").strip(),
                "pic_crop_url": f"data/cards/{pic_crop_name}",
                "word_crop_url": f"data/cards/{word_crop_name}",
                "tts_url": f"data/tts/{tts_slug}.mp3",
                "tts_slow_url": f"data/tts/{tts_slug}_slow.mp3",
                "pic_box_norm": p_box_norm,
                "word_box_norm": w_box_norm,
            })

        total_cards += len(deck_cards)
        decks.append({
            "id": deck_id,
            "pair_num": pair_num,
            "unit_number": len(img_files) // 2 - pair_num + 1,  # 1 = oldest, 44 = newest
            "date": date_str,
            "title_en": topic_en,
            "title_zh": topic_zh,
            "pic_sheet_url": f"data/raw/{pic_id}.jpg",
            "word_sheet_url": f"data/raw/{word_id}.jpg",
            "pic_aspect": round(pw / float(ph), 4),
            "word_aspect": round(ww / float(wh), 4),
            "card_count": len(deck_cards),
            "cards": deck_cards,
        })

    all_words = [c["word"] for d in decks for c in d["cards"] if c.get("word")]
    ensure_tts_files(all_words)

    catalog = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "folder_id": FOLDER_ID,
        "new_files_downloaded": new_downloads,
        "total_decks": len(decks),
        "total_cards": total_cards,
        "audio_tracks": audio_tracks,
        "decks": decks,
    }
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    # If a GitHub remote is configured, auto-commit and push new changes
    if new_downloads > 0 and os.path.isdir(os.path.join(APP_DIR, ".git")):
        try:
            import subprocess
            subprocess.run(["git", "-C", APP_DIR, "add", "-A"], check=False, timeout=30)
            subprocess.run(
                ["git", "-C", APP_DIR, "commit", "-m", f"Auto-sync {new_downloads} new Drive file(s)"],
                check=False,
                timeout=30,
            )
            subprocess.run(["git", "-C", APP_DIR, "push", "origin", "main"], check=False, timeout=60)
        except Exception as e:
            print(f"[GitPush] Warning: {e}")

    return catalog


if __name__ == "__main__":
    do_sync = "--no-drive" not in sys.argv
    t0 = time.time()
    cat = build_catalog(sync_from_drive=do_sync)
    print(
        f"Catalog built in {time.time() - t0:.1f}s: "
        f"{cat['total_decks']} decks, {cat['total_cards']} cards, "
        f"{len(cat['audio_tracks'])} audio tracks."
    )
