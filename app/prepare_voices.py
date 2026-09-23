# -*- coding: utf-8 -*-
"""
準備 8 種台灣口音的「聲音範本」候選樣本
======================================
來源（皆為開放授權）：
  1. Common Voice 台灣華語（zh-TW，CC0）—— 經由 adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw
     有「性別、年齡層」標記 → 用來挑年輕 / 中年 / 青少年 / 長輩的台灣人聲音
  2. TaigiSpeech（CC BY 4.0）—— 台灣長輩說台語的錄音（多數 54 歲以上）
     → 補足高齡聲音，也適合當台語引擎的樣本

每一種聲音挑 2~3 位說話者，把同一人的幾句話接成 8~14 秒，存成
  voices/candidates/<聲音id>__<來源>_<n>.wav   （+ 同名 .json 說明）
網頁「聲音範本」分頁可以試聽並「選用」；尚未設定的聲音會自動套用第 1 個候選。

用法：%USERPROFILE%\\indextts-py311\\python.exe prepare_voices.py   （start_server.bat 第一次啟動會自動執行）
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
import time
from collections import defaultdict

import numpy as np
import requests
import soundfile as sf

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
from audio import ffmpeg_exe            # noqa: E402
from domain import VoiceLibrary, VOICES_DIR  # noqa: E402

CAND_DIR = os.path.join(VOICES_DIR, "candidates")
SR = 24000
HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
ROWS_API = "https://datasets-server.huggingface.co/rows"
CV_DATASET = "adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw"
TAIGI_DATASET = "TaigiSpeech/TaigiSpeech"
PER_VOICE = 3            # 每種聲音幾個候選
MAX_ROWS = 12000         # Common Voice 最多掃描幾筆
S = requests.Session()
S.headers["User-Agent"] = "tw-tts-voice-prep/1.0"

# 每種聲音想要的（性別, 年齡層優先順序）
WANT = {
    "young_female":  ("female", ["twenties", "thirties"]),
    "young_male":    ("male",   ["twenties", "thirties"]),
    "girl":          ("female", ["teens"]),
    "boy":           ("male",   ["teens"]),
    "middle_female": ("female", ["fourties", "forties", "fifties", "thirties"]),
    "middle_male":   ("male",   ["fourties", "forties", "fifties", "thirties"]),
    "elder_female":  ("female", ["sixties", "seventies", "eighties", "nineties"]),
    "elder_male":    ("male",   ["sixties", "seventies", "eighties", "nineties"]),
}


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------
# 音訊小工具
# ------------------------------------------------------------
def decode(src_bytes: bytes, suffix: str) -> np.ndarray:
    """任何格式 → 24kHz 單聲道 float32，並去掉頭尾靜音。"""
    with tempfile.TemporaryDirectory() as d:
        a, b = os.path.join(d, "in" + suffix), os.path.join(d, "out.wav")
        with open(a, "wb") as f:
            f.write(src_bytes)
        af = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,"
              "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1,areverse")
        subprocess.run([ffmpeg_exe(), "-nostdin", "-y", "-loglevel", "error", "-i", a, "-af", af,
                        "-ac", "1", "-ar", str(SR), b], check=True, stdin=subprocess.DEVNULL, timeout=120,
                       creationflags=0x08000000 if os.name == "nt" else 0)
        y, _ = sf.read(b, dtype="float32")
    return y


def join_clips(clips, target=10.0, limit=14.0) -> np.ndarray:
    gap = np.zeros(int(SR * 0.25), dtype=np.float32)
    out, total = [], 0.0
    for y in clips:
        if total >= target:
            break
        if len(y) < SR * 0.8:          # 太短的不要
            continue
        out += [y, gap]
        total += len(y) / SR + 0.25
    if not out:
        return np.zeros(0, dtype=np.float32)
    y = np.concatenate(out)[: int(SR * limit)]
    peak = float(np.max(np.abs(y))) or 1.0
    return (y / peak * 0.89).astype(np.float32)   # 峰值標準化（約 -1 dBFS）


def save_candidate(voice_id: str, tag: str, y: np.ndarray, source: str, placeholder=False) -> str:
    os.makedirs(CAND_DIR, exist_ok=True)
    name = f"{voice_id}__{tag}.wav"
    sf.write(os.path.join(CAND_DIR, name), y, SR, subtype="PCM_16")
    with open(os.path.join(CAND_DIR, name[:-4] + ".json"), "w", encoding="utf-8") as f:
        json.dump({"source": source, "placeholder": placeholder}, f, ensure_ascii=False)
    log(f"   ✔ {name}  ({len(y)/SR:.1f} 秒)  {source}")
    return name


def get(url, **kw):
    for i in range(4):
        try:
            r = S.get(url, timeout=60, **kw)
            if r.status_code == 429:
                time.sleep(15 * (i + 1)); continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if i == 3:
                raise
            time.sleep(2 * (i + 1))
    raise RuntimeError("伺服器忙碌（HTTP 429），請稍後再試")


# ------------------------------------------------------------
# 1) Common Voice 台灣華語
# ------------------------------------------------------------
def norm_gender(g: str) -> str:
    g = (g or "").lower()
    if g.startswith("female") or g == "f":
        return "female"
    if g.startswith("male") or g == "m":
        return "male"
    return ""


def scan_common_voice():
    speakers = defaultdict(lambda: {"gender": "", "age": "", "clips": []})
    for split in ("train", "test"):
        offset = 0
        while offset < MAX_ROWS:
            try:
                r = get(ROWS_API, params={"dataset": CV_DATASET, "config": "default", "split": split,
                                          "offset": offset, "length": 100})
            except Exception as e:
                log("   （讀取 Common Voice 失敗，略過）", e); break
            rows = r.json().get("rows", [])
            if not rows:
                break
            for it in rows:
                row = it.get("row", {})
                meta = row.get("json") or {}
                if isinstance(meta, str):
                    try: meta = json.loads(meta)
                    except Exception: meta = {}
                g, age = norm_gender(meta.get("gender")), (meta.get("age") or "").lower()
                if not g or not age:
                    continue
                aud = row.get("mp3")
                src = aud[0]["src"] if isinstance(aud, list) and aud else (aud or {}).get("src")
                if not src:
                    continue
                sp = speakers[meta.get("client_id", "?")]
                sp["gender"], sp["age"] = g, age
                sp["clips"].append((src, row.get("txt", "")))
            offset += len(rows)
            if offset % 1000 == 0:
                log(f"   已掃描 {split} {offset} 筆，找到 {len(speakers)} 位有標記的說話者")
            time.sleep(0.2)
            if split == "test" and offset >= 1320:
                break
    return speakers


def build_from_common_voice(speakers, used: set, counts: dict):
    for vid, (gender, ages) in WANT.items():
        pool = []
        for rank, age in enumerate(ages):
            for cid, sp in speakers.items():
                if cid in used or sp["gender"] != gender or sp["age"] != age:
                    continue
                pool.append((rank, -len(sp["clips"]), cid))
        pool.sort()
        n = 0
        for _, _, cid in pool:
            if counts[vid] >= PER_VOICE:
                break
            sp = speakers[cid]
            clips = []
            for url, _txt in sp["clips"][:6]:
                try:
                    clips.append(decode(get(url).content, ".mp3"))
                except Exception:
                    continue
            y = join_clips(clips)
            if len(y) < SR * 5:            # 湊不到 5 秒就換下一位
                continue
            used.add(cid)
            n += 1
            counts[vid] += 1
            save_candidate(vid, f"cv{n}", y, f"Common Voice 台灣華語（CC0）· {sp['age']} · 說話者 {cid[:8]}")


# ------------------------------------------------------------
# 2) TaigiSpeech（台灣長輩台語錄音）
# ------------------------------------------------------------
def find_key(d: dict, *names):
    for k, v in d.items():
        if any(n in k.lower() for n in names):
            return v
    return None


def build_from_taigi(counts: dict):
    base = f"{HF}/datasets/{TAIGI_DATASET}/resolve/main"
    profiles = {}
    for i in range(1, 23):
        pid = f"p{i:03d}"
        try:
            p = get(f"{base}/metadata/{pid}_profile.json").json()
        except Exception:
            continue
        age = find_key(p, "age")
        gender = norm_gender(str(find_key(p, "gender", "sex") or ""))
        if not gender:
            gs = str(find_key(p, "gender", "sex") or "")
            gender = "female" if "女" in gs else "male" if "男" in gs else ""
        try:
            age = int(str(age).strip()[:3].strip("歲 "))
        except Exception:
            age = None
        if gender and age:
            profiles[pid] = (gender, age)
    log(f"   TaigiSpeech 說話者資料：{len(profiles)} 位")

    files = defaultdict(list)
    for split in ("train", "val", "test"):
        try:
            txt = get(f"{base}/data/{split}/metadata.jsonl").text
        except Exception:
            continue
        for line in txt.splitlines():
            try:
                m = json.loads(line)
            except Exception:
                continue
            spk = m.get("speaker_id")
            if spk:
                files[spk].append(f"{base}/data/{split}/{m['file_name']}")

    # 年紀大的優先；60 歲以上 → 高齡；40~59 → 中年（補充）
    order = sorted(profiles.items(), key=lambda kv: -kv[1][1])
    for pid, (gender, age) in order:
        vid = None
        if age >= 60:
            vid = f"elder_{gender}"
        elif 40 <= age < 60:
            vid = f"middle_{gender}"
        if not vid or counts[vid] >= PER_VOICE + (1 if vid.startswith("elder") else 0):
            continue
        urls = files.get(pid, [])
        random.Random(pid).shuffle(urls)
        clips = []
        for u in urls[:8]:
            try:
                clips.append(decode(get(u).content, ".wav"))
            except Exception:
                continue
        y = join_clips(clips)
        if len(y) < SR * 5:
            continue
        counts[vid] += 1
        save_candidate(vid, f"taigi_{pid}", y, f"TaigiSpeech（CC BY 4.0）· {age} 歲 · 台語母語者 {pid}")


# ------------------------------------------------------------
# 3) 童音備援：找不到青少年樣本時，用年輕女聲升調「暫代」
# ------------------------------------------------------------
def build_child_fallback(counts: dict):
    import librosa
    base = sorted(f for f in os.listdir(CAND_DIR) if f.startswith("young_female__") and f.endswith(".wav"))
    if not base:
        return
    y, _ = sf.read(os.path.join(CAND_DIR, base[0]), dtype="float32")
    for vid, steps in (("girl", 4.0), ("boy", 3.0)):
        if counts[vid] > 0:
            continue
        z = librosa.effects.pitch_shift(y, sr=SR, n_steps=steps)
        counts[vid] += 1
        save_candidate(vid, "shift1", z.astype(np.float32),
                       "暫代：由年輕女聲升調產生，建議改用孩子本人的錄音（需家長同意）", placeholder=True)


# ------------------------------------------------------------
def main():
    os.makedirs(CAND_DIR, exist_ok=True)
    counts = defaultdict(int)
    for f in os.listdir(CAND_DIR):      # 已經有的候選就不重做
        if f.endswith(".wav") and "__" in f:
            counts[f.split("__")[0]] += 1
    todo = [v for v in WANT if counts[v] < PER_VOICE]
    if todo:
        log("[1/3] 掃描 Common Voice 台灣華語（需要幾分鐘）…")
        speakers = scan_common_voice()
        log(f"   共 {len(speakers)} 位有性別 / 年齡標記的說話者")
        build_from_common_voice(speakers, set(), counts)
        log("[2/3] 下載 TaigiSpeech 台灣長輩聲音…")
        try:
            build_from_taigi(counts)
        except Exception as e:
            log("   （TaigiSpeech 下載失敗，略過）", e)
        log("[3/3] 童音備援…")
        build_child_fallback(counts)

    # 還沒設定樣本的聲音 → 自動套用第 1 個候選
    lib = VoiceLibrary()
    for t in lib.all():
        if t.ready:
            continue
        cands = sorted(f for f in os.listdir(CAND_DIR) if f.startswith(t.id + "__") and f.endswith(".wav"))
        # 高齡聲音：優先用 TaigiSpeech（確定是台灣長輩）
        cands.sort(key=lambda f: (0 if "taigi" in f and t.id.startswith("elder") else 1, f))
        if not cands:
            log(f"   ⚠ {t.name}：沒有找到合適樣本，請到網頁「聲音範本」自行錄音或上傳")
            continue
        import shutil
        shutil.copyfile(os.path.join(CAND_DIR, cands[0]), os.path.join(VOICES_DIR, t.id + ".wav"))
        with open(os.path.join(CAND_DIR, cands[0][:-4] + ".json"), "r", encoding="utf-8") as f:
            m = json.load(f)
        lib.assign_sample(t.id, t.id + ".wav", m["source"], m.get("placeholder", False))
        log(f"   → {t.name} 使用 {cands[0]}")
    log("VOICES_DONE")


if __name__ == "__main__":
    main()
