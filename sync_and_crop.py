#!/usr/bin/env python3
"""
Google Drive Sync, Gemini Vision OCR & Smart Card Cropper for Kids Flashcard App.
Syncs photos and MP3s from Google Drive folder 1lZgQD2gynBy4saiiZObBVLOnY8vjxfDP,
auto-recognizes handwritten English flashcards + Taiwan Traditional Chinese meanings via Gemini Vision,
crops individual card images, generates normal & slow TTS MP3s, builds data/catalog.json,
and optionally pushes updates directly to GitHub Pages.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import cv2
import numpy as np

FOLDER_ID = "1lZgQD2gynBy4saiiZObBVLOnY8vjxfDP"
GDRIVE_CLI = "/google/bin/releases/gemini-agents-gdrive/gdrive"
CRED_PATH = os.path.expanduser("~/.config/flashcard_autosync/credentials.json")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
CARDS_DIR = os.path.join(DATA_DIR, "cards")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")
TTS_DIR = os.path.join(DATA_DIR, "tts")
MANIFEST_PATH = os.path.join(DATA_DIR, "drive_manifest.json")
ANNOTATIONS_PATH = os.path.join(DATA_DIR, "vision_annotations.json")
CATALOG_PATH = os.path.join(DATA_DIR, "catalog.json")

VISION_MODELS = [
    "gemini-3-flash-preview",
    "gemini-3.5-flash",
    "gemini-flash-latest",
    "gemini-3.1-flash-lite",
]


def ensure_dirs():
    for d in (DATA_DIR, RAW_DIR, CARDS_DIR, AUDIO_DIR, TTS_DIR):
        os.makedirs(d, exist_ok=True)


def load_credentials():
    if os.path.exists(CRED_PATH):
        try:
            with open(CRED_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


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
    """Fetches all non-trashed files from the target Google Drive folder, preserving prior order."""
    existing = []
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
    existing_by_id = {x["id"]: x for x in existing}

    out = subprocess.check_output(
        [GDRIVE_CLI, "readonly", "ls", FOLDER_ID, "--max", "500", "--json"],
        text=True,
        timeout=45,
    )
    live = json.loads(out)
    live_ids = {x["id"] for x in live}
    merged = [x for x in existing if x["id"] in live_ids]
    new_items = [x for x in live if x["id"] not in existing_by_id]

    for it in new_items:
        try:
            info_out = subprocess.check_output(
                [GDRIVE_CLI, "readonly", "info", it["id"], "--json"],
                text=True,
                timeout=20,
            )
            info_obj = json.loads(info_out)
            it["createdTime"] = info_obj.get("createdTime", it.get("modifiedTime", ""))
        except Exception:
            it["createdTime"] = it.get("modifiedTime", "")

    new_items.sort(key=lambda x: (x.get("createdTime", ""), x.get("name", "")))
    merged.extend(new_items)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    return merged, len(new_items)


def download_missing_files(files):
    """Downloads any new image or audio files from Google Drive."""
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

        subprocess.check_call([GDRIVE_CLI, "readonly", "download", fid, "--out", out_path], timeout=90)
        return fid, out_path, True

    downloaded_count = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(_dl, item) for item in files]
        for fut in as_completed(futs):
            _, _, is_new = fut.result()
            if is_new:
                downloaded_count += 1
    return downloaded_count


def detect_white_cards_opencv(img):
    """Detects individual white flashcards on wooden desk in [ymin, xmin, ymax, xmax] (0..1000)."""
    h, w = img.shape[:2]
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    L = lab[:, :, 0].astype(np.float32)
    B = lab[:, :, 2].astype(np.float32)
    S = hsv[:, :, 1].astype(np.float32)
    mask = ((L > 150) & (S < 65) & (B < 145)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in cnts:
        x, y, bw, bh = cv2.boundingRect(c)
        if bw * bh < (w * h * 0.03):
            continue
        ymin = int(round(y * 1000.0 / h))
        xmin = int(round(x * 1000.0 / w))
        ymax = int(round((y + bh) * 1000.0 / h))
        xmax = int(round((x + bw) * 1000.0 / w))
        boxes.append([ymin, xmin, ymax, xmax])
    boxes.sort(key=lambda b: (b[0] // 200, b[1]))
    return boxes


def annotate_pair_with_gemini(img_a, img_b, id_a, id_b, pair_num):
    """Uses Gemini Vision to recognize handwritten English words, Chinese meanings, and card boxes."""
    creds = load_credentials()
    api_key = creds.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None

    def _encode_jpg(img, max_dim=1280):
        h, w = img.shape[:2]
        scale = min(1.0, max_dim / float(max(h, w)))
        resized = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1.0 else img
        _, buf = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), 86])
        return base64.b64encode(buf.tobytes()).decode("ascii")

    b64_a = _encode_jpg(img_a)
    b64_b = _encode_jpg(img_b)

    prompt = (
        "You are analyzing a pair of English flashcard sheet photos on a wooden desk for a Taiwanese "
        "elementary school student.\n"
        "Image A and Image B are the two sides (Picture side vs Handwritten English Word side) of the "
        "SAME grid of cards laid out in matching positions (top-to-bottom rows, left-to-right columns).\n\n"
        "Return ONLY valid JSON with this exact object schema:\n"
        "{\n"
        '  "which_is_pic": "A" or "B",\n'
        '  "topic_en": "Concise English topic title",\n'
        '  "topic_zh": "Concise Traditional Chinese (Taiwan) topic title",\n'
        '  "cards": [\n'
        "    {\n"
        '      "word": "Main English word/phrase in Title Case",\n'
        '      "subtext": "Optional parenthetical/secondary English note on the card, or empty string",\n'
        '      "zh": "Natural Traditional Chinese (Taiwan) meaning matching both picture and word",\n'
        '      "pic_box": [ymin, xmin, ymax, xmax],\n'
        '      "word_box": [ymin, xmin, ymax, xmax]\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Coordinates [ymin, xmin, ymax, xmax] must be integers normalized 0..1000 tightly bounding each "
        "entire white index card on the Picture sheet (pic_box) and Word sheet (word_box), ordered row-by-row "
        "from top-left to bottom-right."
    )

    payload = {
        "contents": [{
            "parts": [
                {"text": "Image A:"},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64_a}},
                {"text": "Image B:"},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64_b}},
                {"text": prompt},
            ]
        }],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
    }

    parsed = None
    for model in VISION_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                txt = res["candidates"][0]["content"]["parts"][0]["text"]
                data = json.loads(txt)
                if isinstance(data, list) and data:
                    data = data[0]
                if isinstance(data, dict) and data.get("cards"):
                    parsed = data
                    print(f"[Vision] Recognized {len(data['cards'])} cards using {model}: {data.get('topic_zh')}")
                    break
        except Exception as e:
            print(f"[Vision] {model} warning: {e}")

    if not parsed:
        return None

    which_pic = str(parsed.get("which_is_pic", "A")).strip().upper()
    if which_pic == "A":
        pic_id, word_id = id_a, id_b
        pic_img, word_img = img_a, img_b
    else:
        pic_id, word_id = id_b, id_a
        pic_img, word_img = img_b, img_a

    cards = parsed["cards"]
    cv_pic = detect_white_cards_opencv(pic_img)
    cv_word = detect_white_cards_opencv(word_img)

    # Snap to OpenCV tight white-card contours when card counts match cleanly
    if len(cv_pic) == len(cards):
        for i, c in enumerate(cards):
            c["pic_box"] = cv_pic[i]
    if len(cv_word) == len(cards):
        for i, c in enumerate(cards):
            c["word_box"] = cv_word[i]

    return {
        "pair_num": pair_num,
        "deck_id": f"deck_{pair_num:02d}",
        "pic_file_id": pic_id,
        "word_file_id": word_id,
        "topic_en": parsed.get("topic_en", f"Unit {pair_num}"),
        "topic_zh": parsed.get("topic_zh", f"第 {pair_num} 單元"),
        "cards": cards,
    }


def refine_card_box_opencv(img, box_norm):
    h, w = img.shape[:2]
    ymin, xmin, ymax, xmax = box_norm
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
    def _sat_score(img):
        hsv = cv2.cvtColor(cv2.resize(img, (400, 400)), cv2.COLOR_BGR2HSV)
        s = hsv[:, :, 1].astype(np.float32)
        v = hsv[:, :, 2].astype(np.float32)
        return float(np.std(s) + np.percentile(s, 85) * 0.5 - np.mean(v > 200) * 20)

    return ("a", "b") if _sat_score(img_a) >= _sat_score(img_b) else ("b", "a")


def extract_date_info(item_a, item_b):
    for it in (item_a, item_b):
        exif_t = it.get("imageMediaMetadata", {}).get("time", "")
        if exif_t and len(exif_t) >= 10:
            return exif_t[:10].replace(":", "-")
    for it in (item_a, item_b):
        nm = it.get("name", "")
        if nm.startswith("PXL_") and len(nm) >= 12 and nm[4:12].isdigit():
            d = nm[4:12]
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        stem = os.path.splitext(nm)[0].split("~")[0]
        if len(stem) == 13 and stem.isdigit():
            try:
                ts = int(stem) / 1000.0
                dt = datetime.fromtimestamp(ts)
                if 2024 <= dt.year <= 2027:
                    return dt.strftime("%Y-%m-%d")
            except Exception:
                pass
    return item_a.get("createdTime", "")[:10]


def update_html_deck_count(total_decks):
    for rel in ("index.html", os.path.join("static", "index.html")):
        p = os.path.join(APP_DIR, rel)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                html = f.read()
            updated = re.sub(
                r'(<span id="modal-deck-count">)\d+(</span>)',
                rf"\g<1>{total_decks}\g<2>",
                html,
            )
            if updated != html:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(updated)


def push_to_github_if_changed(commit_msg):
    if not os.path.isdir(os.path.join(APP_DIR, ".git")):
        return False
    status = subprocess.check_output(["git", "-C", APP_DIR, "status", "--porcelain"], text=True).strip()
    if not status:
        return False

    creds = load_credentials()
    gh_token = creds.get("github_token") or os.environ.get("GITHUB_TOKEN", "")
    gh_user = creds.get("github_user", "wadeyu1117")
    if not gh_token:
        print("[GitPush] No github_token configured; skipping push.")
        return False

    subprocess.check_call(["git", "-C", APP_DIR, "add", "-A"], timeout=30)
    subprocess.check_call(["git", "-C", APP_DIR, "commit", "-m", commit_msg], timeout=30)
    b64 = base64.b64encode(f"{gh_user}:{gh_token}".encode("utf-8")).decode("ascii")
    subprocess.check_call(
        [
            "git",
            "-C",
            APP_DIR,
            "-c",
            f"http.https://github.com/.extraheader=AUTHORIZATION: basic {b64}",
            "push",
            "origin",
            "main",
        ],
        timeout=90,
    )
    print(f"[GitPush] Successfully pushed to GitHub Pages: {commit_msg}")
    return True


def build_catalog(sync_from_drive=True, push_changes=False):
    """Main pipeline: syncs from Drive, OCRs new pairs via Gemini, crops cards, and writes catalog.json."""
    ensure_dirs()
    new_manifest_items = 0
    new_downloads = 0
    if sync_from_drive:
        files, new_manifest_items = fetch_drive_manifest()
        new_downloads = download_missing_files(files)
    else:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            files = json.load(f)

    ann_list = []
    annotations_by_pair = {}
    annotations_by_file_id = {}
    if os.path.exists(ANNOTATIONS_PATH):
        with open(ANNOTATIONS_PATH, "r", encoding="utf-8") as f:
            ann_list = json.load(f)
        for ann in ann_list:
            annotations_by_pair[ann["pair_num"]] = ann
            annotations_by_file_id[ann["pic_file_id"]] = ann
            annotations_by_file_id[ann["word_file_id"]] = ann

    max_pair_num = max([a.get("pair_num", 0) for a in ann_list], default=44)
    annotations_dirty = False

    audio_tracks = []
    img_files = []
    for item in files:
        mime = item.get("mimeType", "")
        name = item.get("name", "")
        if "audio" in mime or name.lower().endswith(".mp3"):
            safe_name = f"{item['id']}_{name}"
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
    newly_added_topics = []

    for idx in range(0, len(img_files) - 1, 2):
        item_a = img_files[idx]
        item_b = img_files[idx + 1]
        pair_num = idx // 2 + 1
        date_str = extract_date_info(item_a, item_b)

        path_a = os.path.join(RAW_DIR, f"{item_a['id']}.jpg")
        path_b = os.path.join(RAW_DIR, f"{item_b['id']}.jpg")
        img_a = cv2.imread(path_a)
        img_b = cv2.imread(path_b)
        if img_a is None or img_b is None:
            continue

        ann = (
            annotations_by_file_id.get(item_a["id"])
            or annotations_by_file_id.get(item_b["id"])
            or (annotations_by_pair.get(pair_num) if pair_num <= 44 else None)
        )

        # Brand-new pair uploaded to Google Drive -> auto-annotate with Gemini Vision!
        if ann is None:
            max_pair_num += 1
            ann = annotate_pair_with_gemini(img_a, img_b, item_a["id"], item_b["id"], max_pair_num)
            if ann is not None:
                ann_list.append(ann)
                annotations_by_pair[ann["pair_num"]] = ann
                annotations_by_file_id[ann["pic_file_id"]] = ann
                annotations_by_file_id[ann["word_file_id"]] = ann
                annotations_dirty = True
                newly_added_topics.append(f"Unit #{ann['pair_num']} {ann['topic_zh']}")

        stable_pair_num = ann["pair_num"] if ann and "pair_num" in ann else max(pair_num, max_pair_num)
        deck_id = ann.get("deck_id", f"deck_{stable_pair_num:02d}") if ann else f"deck_{stable_pair_num:02d}"

        if ann:
            pic_id = ann["pic_file_id"]
            word_id = ann["word_file_id"]
            if pic_id == item_a["id"]:
                pic_img, word_img = img_a, img_b
            else:
                pic_img, word_img = img_b, img_a
                pic_id, word_id = item_b["id"], item_a["id"]
            topic_en = ann.get("topic_en", f"Unit {stable_pair_num}")
            topic_zh = ann.get("topic_zh", f"第 {stable_pair_num} 單元")
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
            boxes_p = detect_white_cards_opencv(pic_img)
            boxes_w = detect_white_cards_opencv(word_img)
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

            if not os.path.exists(pic_crop_path) or new_downloads > 0 or annotations_dirty:
                crop_p = pic_img[py0:py1, px0:px1]
                cv2.imwrite(pic_crop_path, crop_p, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

            if not os.path.exists(word_crop_path) or new_downloads > 0 or annotations_dirty:
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

        unit_num = (45 - stable_pair_num) if stable_pair_num <= 44 else stable_pair_num
        total_cards += len(deck_cards)
        decks.append({
            "id": deck_id,
            "pair_num": stable_pair_num,
            "unit_number": unit_num,
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

    if annotations_dirty:
        with open(ANNOTATIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(ann_list, f, indent=2, ensure_ascii=False)

    decks.sort(key=lambda d: d["unit_number"], reverse=True)
    all_words = [c["word"] for d in decks for c in d["cards"] if c.get("word")]
    new_tts = ensure_tts_files(all_words)

    has_meaningful_update = (new_manifest_items > 0 or new_downloads > 0 or annotations_dirty or new_tts > 0)
    prev_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not has_meaningful_update and os.path.exists(CATALOG_PATH):
        try:
            prev_cat = json.load(open(CATALOG_PATH, "r", encoding="utf-8"))
            if prev_cat.get("total_decks") == len(decks) and prev_cat.get("total_cards") == total_cards:
                prev_updated_at = prev_cat.get("updated_at", prev_updated_at)
        except Exception:
            pass

    catalog = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S") if has_meaningful_update else prev_updated_at,
        "folder_id": FOLDER_ID,
        "new_files_downloaded": new_downloads,
        "total_decks": len(decks),
        "total_cards": total_cards,
        "audio_tracks": audio_tracks,
        "decks": decks,
    }
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    update_html_deck_count(len(decks))

    if push_changes and has_meaningful_update:
        summary = ", ".join(newly_added_topics) if newly_added_topics else f"{new_downloads} new Drive file(s)"
        push_to_github_if_changed(f"Auto-sync Google Drive flashcards: {summary}")

    return catalog


if __name__ == "__main__":
    do_sync = "--no-drive" not in sys.argv
    do_push = "--push" in sys.argv
    t0 = time.time()
    cat = build_catalog(sync_from_drive=do_sync, push_changes=do_push)
    print(
        f"Catalog built in {time.time() - t0:.1f}s: "
        f"{cat['total_decks']} decks, {cat['total_cards']} cards, "
        f"{len(cat['audio_tracks'])} audio tracks."
    )
