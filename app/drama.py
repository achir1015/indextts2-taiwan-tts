# -*- coding: utf-8 -*-
"""
小說劇場（多角色對話 → 一個 MP3）
=================================
比喻：把一本小說交給「廣播劇團」

  Character      = 演員：角色名 → 用哪一種聲音、國語或台語、站在舞台左邊還是右邊（立體聲）、音量
  Line           = 一句台詞：誰說、說什麼、什麼情緒、要不要「搶話」（跟上一句重疊）
  Together       = 一段「同時說話」：菜市場叫賣、吵架互嗆 —— 多人一起開口
  Pause          = 停頓幾秒
  Ambience       = 背景環境音：菜市場、人群、會議廳、雨聲…（持續到換場或結束）
  Script         = 整本劇本 = 演員表 + 一連串事件
  ScriptParser   = 把使用者寫的劇本文字，讀成 Script
  NovelConverter = 把一般小說文字（「」對話）自動整理成劇本格式
  Mixer          = 混音師：把每句台詞放到時間軸上、疊上背景音、做立體聲，輸出 MP3
  DramaJob       = 一張「製作工單」：在背景慢慢做，網頁可以看進度

劇本格式（網頁上也有說明與範本）：
  # 這是註解
  [角色] 阿明=年輕男音 左, 阿嬤=高齡女音 右 台語
  [背景 菜市場 40%]
  [停頓 1.5]
  阿明：阿嬤，今天的菜怎麼賣？
  阿嬤（開心）：便宜啦，一把二十！
  >阿明（生氣）：你上次也這樣說！          ← 開頭加 > = 搶話，跟上一句重疊
  【同時】
  攤販甲：來喔來喔，俗俗賣！
  攤販乙：新鮮的高麗菜喔！
  【/同時】
  旁白：市場裡的聲音此起彼落。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional, Union

import numpy as np

from domain import APP_DIR, EMOTIONS, LANG_HOKKIEN, LANG_MANDARIN, OUTPUTS_DIR, SynthesisOrder

SR = 24000                                   # 混音用的取樣率
CACHE_DIR = os.path.join(APP_DIR, "cache", "lines")
AMBIENCE_DIR = os.path.join(APP_DIR, "ambience")

# ------------------------------------------------------------
# 領域物件
# ------------------------------------------------------------
@dataclass
class Character:
    name: str
    voice_id: str = ""
    language: str = LANG_MANDARIN
    pan: float = 0.0          # -1 = 最左、0 = 中間、1 = 最右
    volume: float = 1.0       # 0.2 ~ 2.0


@dataclass
class Line:
    speaker: str
    text: str
    emotion: str = "自然"
    strength: float = 0.7
    barge_in: bool = False    # 搶話：跟上一句重疊一點
    kind: str = "line"


@dataclass
class Together:
    lines: List[Line]
    kind: str = "together"


@dataclass
class Pause:
    seconds: float
    kind: str = "pause"


@dataclass
class Ambience:
    name: str                 # "無" = 關掉背景
    volume: float = 0.35
    kind: str = "ambience"


Event = Union[Line, Together, Pause, Ambience]


@dataclass
class Script:
    cast: Dict[str, Character] = field(default_factory=dict)
    events: List[Event] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def all_lines(self) -> List[Line]:
        out = []
        for e in self.events:
            if isinstance(e, Line):
                out.append(e)
            elif isinstance(e, Together):
                out.extend(e.lines)
        return out

    def speakers(self) -> List[str]:
        seen = []
        for l in self.all_lines():
            if l.speaker not in seen:
                seen.append(l.speaker)
        return seen

    def to_public(self) -> dict:
        return {
            "cast": {k: asdict(v) for k, v in self.cast.items()},
            "speakers": self.speakers(),
            "events": [asdict(e) for e in self.events],
            "line_count": len(self.all_lines()),
            "char_count": sum(len(l.text) for l in self.all_lines()),
            "warnings": self.warnings,
        }


# ------------------------------------------------------------
# 劇本解析
# ------------------------------------------------------------
# 聲音名稱 → voice id（也接受直接寫 id）
VOICE_ALIASES = {
    "年輕女音": "young_female", "年輕男音": "young_male", "女童音": "girl", "男童音": "boy",
    "中年女音": "middle_female", "中年男音": "middle_male", "高齡女音": "elder_female", "高齡男音": "elder_male",
    "女童": "girl", "男童": "boy", "小女孩": "girl", "小男孩": "boy", "阿嬤": "elder_female", "阿公": "elder_male",
}
EMO_ALIASES = {
    "生氣": "生氣", "憤怒": "生氣", "怒": "生氣", "大吼": "生氣", "罵": "生氣", "吵": "生氣",
    "開心": "開心", "高興": "開心", "笑": "開心", "興奮": "開心",
    "悲傷": "悲傷", "難過": "悲傷", "哭": "悲傷", "傷心": "悲傷",
    "害怕": "害怕", "恐懼": "害怕", "驚恐": "害怕",
    "厭惡": "厭惡", "嫌棄": "厭惡", "不屑": "厭惡",
    "低落": "低落", "無奈": "低落", "沮喪": "低落", "嘆氣": "低落",
    "驚訝": "驚訝", "驚": "驚訝", "吃驚": "驚訝",
    "平靜": "平靜", "冷靜": "平靜", "溫柔": "平靜", "自然": "自然",
}
AMBIENCE_ALIASES = {"市場": "菜市場", "菜市場": "菜市場", "夜市": "菜市場", "人群": "人群嘈雜", "人群嘈雜": "人群嘈雜",
                    "餐廳": "人群嘈雜", "會議廳": "會議廳", "辯論": "會議廳", "辯論會": "會議廳", "教室": "會議廳",
                    "雨": "雨聲", "雨聲": "雨聲", "下雨": "雨聲", "無": "無", "關": "無", "停": "無", "安靜": "無"}

RE_LINE = re.compile(r"^(?P<barge>[>＞])?\s*(?P<name>[^：:（(\[【〔\s][^：:（(\[【〔]{0,11}?)\s*"
                     r"(?:[（(](?P<emo>[^）)]{1,10})[）)])?\s*[：:]\s*(?P<text>.+)$")
RE_DIRECTIVE = re.compile(r"^[\[【〔](?P<head>[^\]】〕]+)[\]】〕]\s*(?P<rest>.*)$")
RE_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
RE_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")


def resolve_voice(word: str, known_ids: List[str]) -> Optional[str]:
    word = word.strip()
    if word in known_ids:
        return word
    if word in VOICE_ALIASES:
        return VOICE_ALIASES[word]
    return None


def parse_emotion(raw: str):
    """「生氣」「生氣80%」「大吼」→ (情緒, 強度)"""
    raw = (raw or "").strip()
    if not raw:
        return "自然", 0.7
    strength = 0.7
    m = RE_PERCENT.search(raw)
    if m:
        strength = max(0.1, min(1.0, float(m.group(1)) / 100))
        raw = RE_PERCENT.sub("", raw).strip()
    for k, v in EMO_ALIASES.items():
        if k in raw:
            return v, strength
    return "自然", strength


class ScriptParser:
    """把劇本文字讀成 Script。voice_ids = 目前可用的聲音 id（含自訂）；voice_names = 顯示名稱 → id"""

    def __init__(self, voice_ids: List[str], voice_names: Dict[str, str]):
        self.voice_ids = voice_ids
        self.voice_names = voice_names

    def _voice(self, word):
        return self.voice_names.get(word.strip()) or resolve_voice(word, self.voice_ids)

    def _parse_cast(self, body: str, script: Script):
        # 阿明=年輕男音 左, 阿嬤=高齡女音 右 台語
        for part in re.split(r"[,，、;；]", body):
            if "=" not in part and "＝" not in part:
                continue
            name, spec = re.split(r"[=＝]", part, 1)
            name, words = name.strip(), spec.split()
            if not name:
                continue
            c = script.cast.get(name) or Character(name=name)
            for w in words:
                v = self._voice(w)
                if v:
                    c.voice_id = v
                elif w in ("左", "左邊"):
                    c.pan = -0.6
                elif w in ("右", "右邊"):
                    c.pan = 0.6
                elif w in ("中", "中間"):
                    c.pan = 0.0
                elif w in ("台語", "閩南語", "臺語"):
                    c.language = LANG_HOKKIEN
                elif w in ("國語", "華語"):
                    c.language = LANG_MANDARIN
                elif RE_PERCENT.search(w):
                    c.volume = max(0.2, min(2.0, float(RE_PERCENT.search(w).group(1)) / 100))
                else:
                    script.warnings.append(f"角色「{name}」的設定「{w}」看不懂，已略過")
            script.cast[name] = c

    def parse(self, text: str) -> Script:
        s = Script()
        together: Optional[Together] = None
        for no, raw in enumerate((text or "").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            m = RE_DIRECTIVE.match(line)
            if m:
                # 支援 [背景 菜市場 40%] 與 [角色] 阿明=年輕男音 兩種寫法
                body = (m.group("head").strip() + " " + m.group("rest").strip()).strip()
                head = body.split()[0] if body.split() else ""
                rest = body[len(head):].strip()
                if head.startswith("角色"):
                    self._parse_cast(rest if rest else body[2:], s)
                elif head in ("同時", "一起", "齊聲", "同時開始"):
                    together = Together(lines=[])
                elif head in ("/同時", "／同時", "同時結束", "結束同時", "/一起"):
                    if together and together.lines:
                        s.events.append(together)
                    together = None
                elif head.startswith("停頓") or head.startswith("暫停"):
                    n = RE_NUMBER.search(body)
                    s.events.append(Pause(seconds=min(10.0, float(n.group(1))) if n else 1.0))
                elif head.startswith("背景") or head.startswith("場景") or head.startswith("環境"):
                    words = rest.split()
                    name = "無"
                    vol = 0.35
                    for w in words:
                        pm = RE_PERCENT.search(w)
                        if pm:
                            vol = max(0.0, min(1.0, float(pm.group(1)) / 100))
                        else:
                            name = AMBIENCE_ALIASES.get(w, w)
                    s.events.append(Ambience(name=name, volume=vol))
                else:
                    s.warnings.append(f"第 {no} 行：看不懂的指令 {line}")
                continue
            m = RE_LINE.match(line)
            if not m:
                s.warnings.append(f"第 {no} 行沒有「角色：」開頭，當作旁白：{line[:20]}")
                m_name, m_text, emo, barge = "旁白", line, "", False
            else:
                m_name, m_text = m.group("name").strip(), m.group("text").strip()
                emo, barge = m.group("emo") or "", bool(m.group("barge"))
            emotion, strength = parse_emotion(emo)
            if "插話" in emo or "搶話" in emo:
                barge = True
            ln = Line(speaker=m_name, text=m_text, emotion=emotion, strength=strength, barge_in=barge)
            if not re.search(r"[一-鿿A-Za-z0-9]", m_text):
                continue
            if together is not None:
                together.lines.append(ln)
            else:
                s.events.append(ln)
            if m_name not in s.cast:
                s.cast[m_name] = Character(name=m_name)
        if together and together.lines:
            s.events.append(together)
            s.warnings.append("【同時】沒有對應的【/同時】，已自動結束")
        self._auto_cast(s)
        return s

    def _auto_cast(self, s: Script):
        """沒指定聲音的角色：依名字猜（阿嬤→高齡女音…），猜不到就輪流分配，並自動左右分開。"""
        guesses = [("嬤", "elder_female"), ("婆", "elder_female"), ("奶奶", "elder_female"),
                   ("公", "elder_male"), ("爺", "elder_male"), ("伯", "middle_male"), ("叔", "middle_male"),
                   ("姨", "middle_female"), ("嬸", "middle_female"), ("媽", "middle_female"), ("母", "middle_female"),
                   ("爸", "middle_male"), ("父", "middle_male"), ("老闆娘", "middle_female"), ("老闆", "middle_male"),
                   ("妹", "girl"), ("女孩", "girl"), ("弟", "boy"), ("男孩", "boy"), ("小明", "boy"),
                   ("哥", "young_male"), ("姊", "young_female"), ("姐", "young_female"),
                   ("旁白", "middle_male"), ("主持", "young_female")]
        female_list = ["young_female", "middle_female", "girl", "elder_female"]
        male_list = ["young_male", "middle_male", "boy", "elder_male"]
        female_chars = "美花婷玲芳珍娟雅麗琪萱妤婕怡慧淑秀蓮娘女姐姊妹嫂"
        used = {c.voice_id for c in s.cast.values() if c.voice_id}
        pans = [-0.5, 0.5, -0.25, 0.25, -0.7, 0.7, 0.0]
        k = 0
        for i, c in enumerate(s.cast.values()):
            if not c.voice_id:
                for key, vid in guesses:
                    if key in c.name and vid in self.voice_ids:
                        c.voice_id = vid
                        break
            if not c.voice_id:
                pref = female_list if any(ch in c.name for ch in female_chars) else male_list
                pref = [v for v in pref if v in self.voice_ids]
                free = [v for v in pref if v not in used] or pref or self.voice_ids
                c.voice_id = free[0] if free else ""
            used.add(c.voice_id)
            if c.name != "旁白" and c.pan == 0.0:
                c.pan = pans[k % len(pans)]
                k += 1


# ------------------------------------------------------------
# 小說 → 劇本
# ------------------------------------------------------------
SPEECH_VERBS = "說道|說|道|問道|問|喊道|喊|叫道|叫|罵道|罵|吼道|吼|答道|答|回答|笑道|笑說|哭道|嘆道|嚷道|嚷|低聲說|大聲說|插嘴|接著說"
RE_TAG_BEFORE = re.compile(r"([一-鿿]{1,4}?)(?:[一-鿿]{0,4}地)?(?:" + SPEECH_VERBS + r")[：:，,]?\s*$")
RE_TAG_AFTER = re.compile(r"^\s*([一-鿿]{1,4}?)(?:[一-鿿]{0,4}地)?(?:" + SPEECH_VERBS + r")")
NOT_NAMES = {"他", "她", "我", "你", "它", "大家", "有人", "對方", "又", "便", "就", "才", "也", "還", "再", "卻", "然後", "接著"}
EMO_HINTS = [("罵", "生氣"), ("吼", "生氣"), ("怒", "生氣"), ("氣", "生氣"), ("嚷", "生氣"),
             ("笑", "開心"), ("哭", "悲傷"), ("嘆", "低落"), ("驚", "驚訝"), ("怕", "害怕"), ("抖", "害怕")]


class NovelConverter:
    """
    把一般小說（對話用「」包起來）轉成劇本：
      阿明生氣地說：「你又遲到了！」  →  阿明（生氣）：你又遲到了！
      「對不起嘛。」小美笑著說。       →  小美（開心）：對不起嘛。
      其他敘述文字                     →  旁白：……（可選）
    找不到說話者時，會沿用「上上一位」說話者（一問一答通常兩人交替），並標記「？」讓你檢查。
    """

    ADVERBS = ("大聲", "小聲", "輕聲", "低聲", "笑著", "哭著", "冷冷", "急忙", "連忙", "忍不住", "突然", "接著",
               "然後", "於是", "只好", "終於", "馬上", "立刻", "一邊", "頭也")

    def detect_names(self, prose: str) -> List[str]:
        """
        自動找角色名：取每個「說話標記」子句的開頭 2~3 個字當候選（主詞通常在最前面），
        再看它在整篇文章出現幾次，挑出現最多的那個長度。
        """
        cands = {}
        for m in re.finditer(r"([^，,。！？!?；;「」『』\n]{1,12}?)(?:" + SPEECH_VERBS + r")[：:，,]?\s*[「『]", prose):
            clause = m.group(1).strip()
            self._add_cand(clause, prose, cands)
        for m in re.finditer(r"[」』]\s*([^，,。！？!?；;「」『』\n]{1,12}?)(?:" + SPEECH_VERBS + r")", prose):
            self._add_cand(m.group(1).strip(), prose, cands)
        return [n for n, _ in sorted(cands.items(), key=lambda kv: -kv[1])]

    def _add_cand(self, clause, prose, cands):
        best, best_n = None, 0
        for L in (2, 3):
            if len(clause) < L:
                continue
            w = clause[:L]
            if w in NOT_NAMES or any(w.startswith(a) for a in self.ADVERBS) or not re.fullmatch(r"[\u4e00-\u9fff]+", w):
                continue
            n = prose.count(w)
            if n > best_n or (n == best_n and best and len(w) < len(best)):
                best, best_n = w, n
        if best:
            cands[best] = cands.get(best, 0) + best_n

    def convert(self, prose: str, known_names: List[str], with_narration: bool = True) -> str:
        known_names = [n for n in (known_names or []) if n and n != "旁白"] or self.detect_names(prose)
        out, recent = [], []
        for para in re.split(r"\n\s*\n|\n", prose or ""):
            para = para.strip()
            if not para:
                continue
            pos = 0
            for m in re.finditer(r"[「『“\"](.+?)[」』”\"]", para):
                before, speech = para[pos:m.start()], m.group(1).strip()
                after = para[m.end():m.end() + 16]
                speaker, emo, tag_span = self._find_speaker(before, after, known_names)
                narr = before[:tag_span] if tag_span is not None else before
                if with_narration and self._meaningful(narr):
                    out.append(f"旁白：{narr.strip(' ，,：:')}")
                if not speaker:
                    speaker = recent[-2] if len(recent) >= 2 else (recent[-1] if recent else "？")
                    speaker = speaker + "？" if not speaker.endswith("？") else speaker
                clean = speaker.rstrip("？")
                recent.append(clean)
                out.append(f"{speaker}{f'（{emo}）' if emo else ''}：{speech}")
                pos = m.end()
                # 跳過緊接在後面的說話標記（「……」小美笑著說。）
                am = RE_TAG_AFTER.match(para[pos:])
                if am and am.group(1) not in NOT_NAMES:
                    pos += am.end()
            tail = para[pos:]
            if with_narration and self._meaningful(tail):
                out.append(f"旁白：{tail.strip(' ，,。')}")
        return "\n".join(out)

    def _name_ok(self, name, known):
        return name and name not in NOT_NAMES and (not known or any(name.endswith(k) or k.endswith(name) for k in known) or len(name) <= 3)

    def _find_speaker(self, before, after, known):
        """回傳（說話者, 情緒, 要從敘述中拿掉的說話標記起點）"""
        # 說話標記：句尾像「……大聲問：」「阿嬤生氣地罵：」這種短子句
        tag = re.search(r"[^，,。！？!?；;」』]{0,10}?(?:" + SPEECH_VERBS + r")[：:，,]?\s*$", before)
        tag_start = tag.start() if tag else None
        last_sentence = re.split(r"[。！？!?；;」』]", before)[-1]
        emo = self._emo(before[tag_start:]) if tag else ""
        # 1) 最後一句裡，最早出現的已知角色（通常是主詞）
        hits = [(last_sentence.find(n), n) for n in known if n and n in last_sentence]
        if hits:
            name = min(hits)[1]
            return name, emo, tag_start
        # 2) 說話標記裡的名字
        if tag:
            mb = RE_TAG_BEFORE.search(before[tag_start:])
            if mb and mb.group(1) not in NOT_NAMES:
                return mb.group(1), emo, tag_start
        # 3) 引號後面：「……」小美笑著說
        for name in sorted(known, key=len, reverse=True):
            if name and after.startswith(name):
                return name, self._emo(after), tag_start
        ma = RE_TAG_AFTER.match(after)
        if ma and ma.group(1) not in NOT_NAMES:
            return ma.group(1), self._emo(ma.group(0)), tag_start
        return None, emo, tag_start

    def _emo(self, s):
        for k, v in EMO_HINTS:
            if k in s:
                return v
        return ""

    def _meaningful(self, s):
        return len(re.sub(r"[\s，。,.！!？?：:、…]", "", s or "")) >= 4


# ------------------------------------------------------------
# 背景環境音（不用下載任何音效：用現有的人聲樣本 + 數學產生）
# ------------------------------------------------------------
def _pink_noise(n, rng):
    """粉紅雜訊：比白雜訊柔和，像遠處的環境底噪。"""
    white = rng.standard_normal(n)
    f = np.fft.rfft(white)
    freqs = np.arange(len(f))
    freqs[0] = 1
    f = f / np.sqrt(freqs)
    y = np.fft.irfft(f, n)
    return (y / (np.max(np.abs(y)) + 1e-9)).astype(np.float32)


def _band(y, lo, hi):
    """簡易頻段濾波（FFT 方式，不需要 scipy）。"""
    f = np.fft.rfft(y)
    freqs = np.fft.rfftfreq(len(y), 1 / SR)
    f[(freqs < lo) | (freqs > hi)] = 0
    return np.fft.irfft(f, len(y)).astype(np.float32)


def _babble(n, voices, layers, rng, gain_range=(0.3, 1.0)):
    """把多位說話者的片段隨機剪下、重疊 → 聽起來就是一群人在講話。"""
    out = np.zeros(n, dtype=np.float32)
    if not voices:
        return out
    for _ in range(layers):
        pos = int(rng.uniform(0, SR * 0.8))
        while pos < n:
            v = voices[rng.integers(len(voices))]
            L = int(rng.uniform(0.8, 2.5) * SR)
            st = int(rng.uniform(0, max(1, len(v) - L)))
            chunk = v[st: st + L].copy()
            if len(chunk) < SR * 0.3:
                pos += SR // 2
                continue
            fade = min(len(chunk) // 4, int(0.08 * SR))
            chunk[:fade] *= np.linspace(0, 1, fade)
            chunk[-fade:] *= np.linspace(1, 0, fade)
            end = min(n, pos + len(chunk))
            out[pos:end] += chunk[: end - pos] * rng.uniform(*gain_range)
            pos = end + int(rng.uniform(0.05, 0.6) * SR)
    return out


def make_ambience(name: str, seconds: float, voices: List[np.ndarray], seed: int = 1) -> np.ndarray:
    """產生指定長度的背景音（單聲道，已標準化到大約 -24 dBFS）。"""
    n = int(seconds * SR) + SR
    rng = np.random.default_rng(seed)
    if name == "菜市場":
        y = _band(_babble(n, voices, 9, rng), 250, 3800) * 0.9
        y += _band(_babble(n, voices, 2, rng, (0.8, 1.4)), 200, 5000)       # 比較近的叫賣聲
        y += _band(_pink_noise(n, rng), 80, 6000) * 0.06                     # 底噪
        # 偶爾的碗盤 / 塑膠袋聲：短促的高頻爆裂
        for _ in range(int(seconds / 2.5)):
            p = int(rng.uniform(0, n - SR // 5))
            burst = _band(rng.standard_normal(SR // 12).astype(np.float32), 2000, 9000)
            burst *= np.exp(-np.linspace(0, 8, len(burst))).astype(np.float32) * rng.uniform(0.05, 0.15)
            y[p:p + len(burst)] += burst
    elif name == "人群嘈雜":
        y = _band(_babble(n, voices, 12, rng), 200, 3000)
        y += _band(_pink_noise(n, rng), 60, 4000) * 0.05
    elif name == "會議廳":
        y = _band(_babble(n, voices, 3, rng, (0.15, 0.4)), 200, 2500)       # 台下零星交談
        y += _band(_pink_noise(n, rng), 40, 1500) * 0.08                     # 空調 / 空間底噪
    elif name == "雨聲":
        w = rng.standard_normal(n).astype(np.float32)
        y = _band(w, 500, 9000) * 0.5 + _band(w, 80, 600) * 0.3
        env = 0.8 + 0.2 * np.sin(np.linspace(0, seconds / 3, n)).astype(np.float32)
        y *= env
    else:
        y = load_custom_ambience(name, n)
        if y is None:
            return np.zeros(n, dtype=np.float32)
    rms = float(np.sqrt(np.mean(y ** 2))) or 1e-6
    return (y * (10 ** (-24 / 20) / rms)).astype(np.float32)


def list_custom_ambience() -> List[str]:
    if not os.path.isdir(AMBIENCE_DIR):
        return []
    return sorted(os.path.splitext(f)[0] for f in os.listdir(AMBIENCE_DIR) if f.lower().endswith(".wav"))


def load_custom_ambience(name: str, n: int) -> Optional[np.ndarray]:
    """使用者上傳的背景音（會重複播放到需要的長度）。"""
    import soundfile as sf
    p = os.path.join(AMBIENCE_DIR, name + ".wav")
    if not os.path.exists(p):
        return None
    y, sr = sf.read(p, dtype="float32", always_2d=True)
    y = y.mean(axis=1)
    if sr != SR:
        y = resample(y, sr, SR)
    reps = int(np.ceil(n / max(1, len(y))))
    return np.tile(y, reps)[:n]


BUILTIN_AMBIENCE = ["菜市場", "人群嘈雜", "會議廳", "雨聲"]


# ------------------------------------------------------------
# 音訊工具
# ------------------------------------------------------------
def resample(y: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    if sr_from == sr_to:
        return y
    try:
        import librosa
        return librosa.resample(y, orig_sr=sr_from, target_sr=sr_to).astype(np.float32)
    except Exception:
        n = int(len(y) * sr_to / sr_from)
        return np.interp(np.linspace(0, len(y) - 1, n), np.arange(len(y)), y).astype(np.float32)


def read_mono(path: str) -> np.ndarray:
    import soundfile as sf
    y, sr = sf.read(path, dtype="float32", always_2d=True)
    return resample(y.mean(axis=1), sr, SR)


def trim_silence(y: np.ndarray, thresh_db: float = -42) -> np.ndarray:
    """去掉每句前後多餘的靜音，對話節奏才抓得準。"""
    if len(y) == 0:
        return y
    win = int(0.02 * SR)
    frames = len(y) // win
    if frames == 0:
        return y
    e = np.sqrt(np.mean(y[: frames * win].reshape(frames, win) ** 2, axis=1) + 1e-12)
    peak = e.max()
    on = np.where(20 * np.log10(e / peak + 1e-12) > thresh_db)[0]
    if len(on) == 0:
        return y
    a = max(0, on[0] * win - int(0.03 * SR))
    b = min(len(y), (on[-1] + 1) * win + int(0.08 * SR))
    return y[a:b]


def simple_reverb(y: np.ndarray, room: str) -> np.ndarray:
    """用衰減雜訊當「空間響應」做卷積 → 房間 / 大廳的空間感。"""
    if room == "無":
        return y
    t60 = {"房間": 0.35, "大廳": 1.1}.get(room, 0)
    if not t60:
        return y
    rng = np.random.default_rng(7)
    L = int(t60 * SR)
    ir = rng.standard_normal(L).astype(np.float32) * np.exp(-6.9 * np.arange(L) / L).astype(np.float32)
    ir = _band(ir, 150, 6000)
    ir /= np.sqrt(np.sum(ir ** 2)) + 1e-9
    wet_amt = 0.18 if room == "房間" else 0.3
    nfft = 1 << int(np.ceil(np.log2(len(y) + L)))
    wet = np.fft.irfft(np.fft.rfft(y, nfft) * np.fft.rfft(ir, nfft), nfft)[: len(y)]
    return (y * (1 - wet_amt) + wet.astype(np.float32) * wet_amt).astype(np.float32)


# ------------------------------------------------------------
# 混音師
# ------------------------------------------------------------
PACE_GAP = {"從容": 0.7, "一般": 0.4, "緊湊": 0.15, "爭吵": -0.15}


@dataclass
class MixSettings:
    pace: str = "一般"          # 句子之間的間隔
    room: str = "無"            # 空間感：無 / 房間 / 大廳
    ambience_volume: float = 1.0   # 背景音總音量倍率
    stereo: bool = True


class Mixer:
    def __init__(self, settings: MixSettings, voice_samples: List[np.ndarray]):
        self.s = settings
        self.voice_samples = voice_samples
        self.rng = random.Random(42)

    def mix(self, script: Script, audio_of: Callable[[Line], np.ndarray]) -> np.ndarray:
        gap = PACE_GAP.get(self.s.pace, 0.4)
        placed = []           # (起點秒數, 單聲道音訊, 角色)
        amb_marks = []        # (起點秒數, 名稱, 音量)
        t = 0.3
        last_start, last_end = 0.0, 0.0
        for e in script.events:
            if isinstance(e, Line):
                y = audio_of(e)
                dur = len(y) / SR
                if e.barge_in and last_end > 0:
                    # 搶話：在上一句結束前 0.4~0.8 秒切進來
                    start = max(last_start + 0.3, last_end - self.rng.uniform(0.4, 0.8))
                else:
                    start = max(0.0, last_end + gap) if last_end > 0 else t
                placed.append((start, y, e.speaker))
                last_start, last_end = start, start + dur
            elif isinstance(e, Together):
                base = last_end + max(gap, 0.1) if last_end > 0 else t
                end = base
                for i, ln in enumerate(e.lines):
                    y = audio_of(ln)
                    st = base + (0 if i == 0 else self.rng.uniform(0.1, 0.9))
                    placed.append((st, y, ln.speaker))
                    end = max(end, st + len(y) / SR)
                last_start, last_end = base, end
            elif isinstance(e, Pause):
                last_end = (last_end if last_end > 0 else t) + e.seconds
            elif isinstance(e, Ambience):
                amb_marks.append((last_end if last_end > 0 else 0.0, e.name, e.volume))
        total = (last_end if last_end > 0 else 1.0) + 1.2
        n = int(total * SR)
        L = np.zeros(n, dtype=np.float32)
        R = np.zeros(n, dtype=np.float32)

        # 1) 台詞：依角色放到左右聲道
        for start, y, spk in placed:
            c = script.cast.get(spk) or Character(name=spk)
            y = simple_reverb(y, self.s.room) * c.volume
            p = int(start * SR)
            end = min(n, p + len(y))
            seg = y[: end - p]
            if self.s.stereo:
                ang = (c.pan + 1) * np.pi / 4           # 等功率聲像
                L[p:end] += seg * np.cos(ang) * 1.2
                R[p:end] += seg * np.sin(ang) * 1.2
            else:
                L[p:end] += seg
                R[p:end] += seg

        # 2) 背景音：每個 [背景] 從該處開始，直到下一個 [背景] 或結尾，前後淡入淡出
        for i, (st, name, vol) in enumerate(amb_marks):
            if name == "無" or vol <= 0:
                continue
            en = amb_marks[i + 1][0] if i + 1 < len(amb_marks) else total
            dur = max(0.5, en - st)
            amb = make_ambience(name, dur, self.voice_samples, seed=i + 1)[: int(dur * SR)]
            fade = min(len(amb) // 3, int(1.2 * SR))
            if fade > 0:
                amb[:fade] *= np.linspace(0, 1, fade)
                amb[-fade:] *= np.linspace(1, 0, fade)
            amb *= vol * self.s.ambience_volume * 2.2
            p = int(st * SR)
            end = min(n, p + len(amb))
            # 背景音做一點左右差異，比較有「身在其中」的感覺
            L[p:end] += amb[: end - p]
            R[p:end] += np.roll(amb, int(0.013 * SR))[: end - p]

        out = np.stack([L, R], axis=1)
        peak = float(np.max(np.abs(out))) or 1.0
        return (out * (0.9 / peak)).astype(np.float32)


# ------------------------------------------------------------
# 製作工單 + 排程
# ------------------------------------------------------------
@dataclass
class DramaJob:
    id: str
    title: str
    status: str = "queued"        # queued / running / done / error
    done_lines: int = 0
    total_lines: int = 0
    current: str = ""
    message: str = ""
    mp3_file: str = ""
    seconds: float = 0.0
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    cached_lines: int = 0

    def to_public(self):
        d = asdict(self)
        el = (self.finished_at or time.time()) - self.started_at if self.started_at else 0
        d["elapsed"] = round(el)
        made = self.done_lines - self.cached_lines
        left = self.total_lines - self.done_lines
        d["eta"] = round(el / made * left) if made > 0 and left > 0 and self.status == "running" else None
        return d


class LineCache:
    """同一句（同聲音、同情緒、同文字）做過就存起來，改劇本重做時只做有變的句子。"""

    def __init__(self, root=CACHE_DIR):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def key(self, ln: Line, ch: Character, ref_path: str) -> str:
        mt = os.path.getmtime(ref_path) if ref_path and os.path.exists(ref_path) else 0
        raw = json.dumps([ln.text, ln.emotion, round(ln.strength, 2), ch.voice_id, ch.language, mt], ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def path(self, key: str) -> str:
        return os.path.join(self.root, key + ".wav")


class DramaStudio:
    """劇團經理：收工單、排隊、一句一句請錄音師錄，最後交給混音師。"""

    def __init__(self, library, engines: Dict[str, object], wav_to_mp3: Callable,
                 on_done: Optional[Callable] = None):
        self.on_done = on_done            # 完成時通知（例如寫進「產生紀錄」）
        self.library = library
        self.engines = engines
        self.wav_to_mp3 = wav_to_mp3
        self.cache = LineCache()
        self.jobs: Dict[str, DramaJob] = {}
        self._queue: List[tuple] = []
        self._cv = threading.Condition()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- 對外 ----
    def submit(self, script: Script, settings: MixSettings, title: str) -> DramaJob:
        job = DramaJob(id=time.strftime("drama_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4],
                       title=title or "小說劇場", total_lines=len(script.all_lines()))
        self.jobs[job.id] = job
        with self._cv:
            self._queue.append((job, script, settings))
            self._cv.notify()
        return job

    def estimate(self, script: Script, sec_per_char: float) -> dict:
        todo_chars, cached = 0, 0
        for ln in script.all_lines():
            ch = script.cast.get(ln.speaker) or Character(name=ln.speaker)
            ref = self._ref(ch)
            if ref and os.path.exists(self.cache.path(self.cache.key(ln, ch, ref))):
                cached += 1
            else:
                todo_chars += len(ln.text)
        return {"seconds": round(todo_chars * sec_per_char + 15), "cached_lines": cached}

    # ---- 內部 ----
    def _ref(self, ch: Character) -> Optional[str]:
        try:
            return self.library.get(ch.voice_id).ref_path
        except KeyError:
            return None

    def _worker(self):
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                job, script, settings = self._queue.pop(0)
            try:
                self._run(job, script, settings)
            except Exception as e:
                traceback.print_exc()
                job.status, job.message = "error", str(e)
                job.finished_at = time.time()

    def _render_line(self, ln: Line, ch: Character) -> str:
        ref = self._ref(ch)
        if not ref:
            raise RuntimeError(f"角色「{ch.name}」使用的聲音還沒有樣本")
        path = self.cache.path(self.cache.key(ln, ch, ref))
        if os.path.exists(path):
            return path
        order = SynthesisOrder(text=ln.text, voice_id=ch.voice_id, language=ch.language,
                               emotion=ln.emotion, emotion_strength=ln.strength)
        order.validate()
        tmp = path + ".tmp.wav"
        self.engines[ch.language].synthesize(order, ref, tmp)
        os.replace(tmp, path)
        return path

    def _run(self, job: DramaJob, script: Script, settings: MixSettings):
        job.status, job.started_at = "running", time.time()
        lines = script.all_lines()
        # 先把「同一個聲音」的句子排在一起做：模型會重複使用聲音特徵，比較快
        order = sorted(range(len(lines)), key=lambda i: (script.cast.get(lines[i].speaker, Character("")).voice_id, i))
        files: Dict[int, str] = {}
        for i in order:
            ln = lines[i]
            ch = script.cast.get(ln.speaker) or Character(name=ln.speaker)
            job.current = f"{ln.speaker}：{ln.text[:18]}"
            ref = self._ref(ch)
            if ref and os.path.exists(self.cache.path(self.cache.key(ln, ch, ref))):
                job.cached_lines += 1
            files[i] = self._render_line(ln, ch)
            job.done_lines += 1
            print(f">> 劇場 {job.id}：{job.done_lines}/{job.total_lines} 句完成", flush=True)

        job.current = "混音中…"
        audio = {id(ln): trim_silence(read_mono(files[i])) for i, ln in enumerate(lines)}
        voice_samples = []
        for t in self.library.all():
            if t.ready:
                try:
                    voice_samples.append(read_mono(t.ref_path))
                except Exception:
                    pass
        mix = Mixer(settings, voice_samples).mix(script, lambda ln: audio[id(ln)])

        import soundfile as sf
        wav = os.path.join(OUTPUTS_DIR, job.id + ".wav")
        sf.write(wav, mix, SR, subtype="PCM_16")
        mp3 = job.id + ".mp3"
        try:
            self.wav_to_mp3(wav, os.path.join(OUTPUTS_DIR, mp3), 1.0, "192k", job.title)
        finally:
            os.remove(wav)
        job.mp3_file, job.seconds = mp3, round(len(mix) / SR, 1)
        job.status, job.current, job.finished_at = "done", "完成", time.time()
        if self.on_done:
            try:
                self.on_done(job, script)
            except Exception:
                traceback.print_exc()
