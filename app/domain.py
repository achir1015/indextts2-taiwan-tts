# -*- coding: utf-8 -*-
"""
領域模型（Domain Model）
========================
用「錄音室」來比喻整個系統：

  VoiceTemplate   = 一位「配音員」的聲音樣本（年輕女音、高齡男音…）
  VoiceLibrary    = 「配音員名冊」，記錄每位配音員的樣本檔在哪裡
  SynthesisOrder  = 一張「配音工單」：要唸什麼字、找哪位配音員、什麼情緒、什麼語速
  SynthesisResult = 「成品」：產生出來的 MP3 與相關資訊

引擎（engines.py）就是「錄音師」，拿工單 + 配音員樣本，做出成品。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VOICES_DIR = os.path.join(APP_DIR, "voices")
OUTPUTS_DIR = os.path.join(APP_DIR, "outputs")
CATALOG_PATH = os.path.join(VOICES_DIR, "voices.json")


# ------------------------------------------------------------
# 語言（國語 / 台語）
# ------------------------------------------------------------
LANG_MANDARIN = "zh"   # 台灣國語（IndexTTS2）
LANG_HOKKIEN = "nan"   # 台灣閩南語（選配引擎）

# ------------------------------------------------------------
# 情緒：IndexTTS2 的情緒向量順序固定為
# [開心, 生氣, 悲傷, 害怕, 厭惡, 低落, 驚訝, 平靜]
# ------------------------------------------------------------
EMOTIONS: Dict[str, int] = {
    "開心": 0, "生氣": 1, "悲傷": 2, "害怕": 3,
    "厭惡": 4, "低落": 5, "驚訝": 6, "平靜": 7,
}


# ------------------------------------------------------------
# 預設的 8 位配音員（id 固定，網頁與準備腳本都依這個 id 對應）
# ------------------------------------------------------------
DEFAULT_TEMPLATES = [
    # id,            顯示名稱,   性別,     年齡層
    ("young_female",  "年輕女音", "female", "young"),
    ("young_male",    "年輕男音", "male",   "young"),
    ("girl",          "女童音",   "female", "child"),
    ("boy",           "男童音",   "male",   "child"),
    ("middle_female", "中年女音", "female", "middle"),
    ("middle_male",   "中年男音", "male",   "middle"),
    ("elder_female",  "高齡女音", "female", "elder"),
    ("elder_male",    "高齡男音", "male",   "elder"),
]


@dataclass
class VoiceTemplate:
    """一位配音員。ref_wav 為相對於 voices/ 的檔名。"""
    id: str
    name: str
    gender: str
    age_group: str
    ref_wav: Optional[str] = None     # 例如 "young_female.wav"
    source: str = ""                  # 樣本來源說明（自行錄音 / 資料集…）
    placeholder: bool = False         # True = 暫代樣本（建議之後換成真人錄音）
    updated_at: float = 0.0

    @property
    def ref_path(self) -> Optional[str]:
        if not self.ref_wav:
            return None
        p = os.path.join(VOICES_DIR, self.ref_wav)
        return p if os.path.exists(p) else None

    @property
    def ready(self) -> bool:
        return self.ref_path is not None

    def to_public(self) -> dict:
        d = asdict(self)
        d["ready"] = self.ready
        return d


class VoiceLibrary:
    """配音員名冊：負責讀寫 voices/voices.json。"""

    def __init__(self, catalog_path: str = CATALOG_PATH):
        self.catalog_path = catalog_path
        self._lock = threading.Lock()
        self._templates: Dict[str, VoiceTemplate] = {}
        self.load()

    # ---- 讀取 / 儲存 ----
    def load(self) -> None:
        os.makedirs(VOICES_DIR, exist_ok=True)
        data = {}
        if os.path.exists(self.catalog_path):
            with open(self.catalog_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        self._templates = {}
        # 先放預設 8 位，確保順序固定
        for tid, name, gender, age in DEFAULT_TEMPLATES:
            saved = data.get(tid, {})
            self._templates[tid] = VoiceTemplate(
                id=tid, name=name, gender=gender, age_group=age,
                ref_wav=saved.get("ref_wav"), source=saved.get("source", ""),
                placeholder=saved.get("placeholder", False),
                updated_at=saved.get("updated_at", 0.0),
            )
        # 再放使用者自訂的配音員
        for tid, saved in data.items():
            if tid not in self._templates:
                self._templates[tid] = VoiceTemplate(**saved)

    def save(self) -> None:
        with self._lock:
            data = {t.id: asdict(t) for t in self._templates.values()}
            tmp = self.catalog_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.catalog_path)

    # ---- 查詢 ----
    def all(self) -> List[VoiceTemplate]:
        return list(self._templates.values())

    def get(self, tid: str) -> VoiceTemplate:
        if tid not in self._templates:
            raise KeyError(f"找不到聲音範本：{tid}")
        return self._templates[tid]

    # ---- 更新樣本 ----
    def assign_sample(self, tid: str, wav_filename: str, source: str, placeholder: bool = False) -> VoiceTemplate:
        t = self.get(tid)
        t.ref_wav = wav_filename
        t.source = source
        t.placeholder = placeholder
        t.updated_at = time.time()
        self.save()
        return t

    def add_custom(self, name: str, gender: str = "other", age_group: str = "custom") -> VoiceTemplate:
        tid = "custom_%d" % int(time.time() * 1000)
        t = VoiceTemplate(id=tid, name=name, gender=gender, age_group=age_group)
        self._templates[tid] = t
        self.save()
        return t

    def remove_custom(self, tid: str) -> None:
        if not tid.startswith("custom_"):
            raise ValueError("預設的 8 種聲音不能刪除，只能更換樣本")
        t = self._templates.pop(tid)
        if t.ref_path:
            try:
                os.remove(t.ref_path)
            except OSError:
                pass
        self.save()


@dataclass
class SynthesisOrder:
    """配音工單。"""
    text: str
    voice_id: str
    language: str = LANG_MANDARIN
    emotion: str = "自然"          # "自然" = 跟著樣本本身的語氣；或 EMOTIONS 其中之一
    emotion_strength: float = 0.6  # 0 ~ 1
    speed: float = 1.0             # 0.5 ~ 2.0（以 ffmpeg atempo 調整，不改變音高）
    bitrate: str = "192k"

    def validate(self) -> None:
        self.text = (self.text or "").strip()
        if not self.text:
            raise ValueError("請輸入要轉成語音的文字")
        if len(self.text) > 5000:
            raise ValueError("文字太長（上限 5000 字），請分段產生")
        if self.language not in (LANG_MANDARIN, LANG_HOKKIEN):
            raise ValueError("不支援的語言")
        if self.emotion != "自然" and self.emotion not in EMOTIONS:
            raise ValueError("不支援的情緒")
        self.emotion_strength = max(0.0, min(1.0, float(self.emotion_strength)))
        self.speed = max(0.5, min(2.0, float(self.speed)))
        if self.bitrate not in ("128k", "192k", "256k", "320k"):
            self.bitrate = "192k"

    def emotion_vector(self) -> Optional[List[float]]:
        """把「情緒 + 強度」轉成 IndexTTS2 需要的 8 維向量；自然 = None（沿用樣本語氣）。"""
        if self.emotion == "自然":
            return None
        vec = [0.0] * 8
        vec[EMOTIONS[self.emotion]] = self.emotion_strength
        return vec


@dataclass
class SynthesisResult:
    """成品。"""
    id: str
    mp3_file: str          # 相對於 outputs/ 的檔名
    text: str
    voice_id: str
    voice_name: str
    language: str
    emotion: str
    speed: float
    seconds: float         # 音檔長度
    elapsed: float         # 產生花費秒數
    created_at: float = field(default_factory=time.time)
