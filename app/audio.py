# -*- coding: utf-8 -*-
"""
音訊工具：全部透過 ffmpeg（imageio-ffmpeg 內附，不必另外安裝）
比喻：這裡是「後製室」—— 把錄音師給的 WAV 壓成 MP3、調語速、整理樣本音量。
"""
from __future__ import annotations

import os
import subprocess
import wave
from typing import Optional


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"  # 退而求其次：用系統 PATH 裡的 ffmpeg


def _run(args: list) -> None:
    # Windows 下不要跳出黑色視窗
    flags = 0x08000000 if os.name == "nt" else 0
    p = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=flags, timeout=600)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg 失敗：" + p.stderr.decode("utf-8", "ignore")[-800:])


def _atempo_chain(speed: float) -> str:
    """atempo 單次只接受 0.5~2.0，這裡保險起見串接。"""
    parts = []
    s = speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append("atempo=%.4f" % s)
    return ",".join(parts)


def wav_to_mp3(wav_path: str, mp3_path: str, speed: float = 1.0, bitrate: str = "192k",
               title: Optional[str] = None) -> None:
    """WAV → MP3（可同時調整語速，不改變音高）。"""
    args = [ffmpeg_exe(), "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", wav_path]
    if abs(speed - 1.0) > 1e-3:
        args += ["-filter:a", _atempo_chain(speed)]
    args += ["-codec:a", "libmp3lame", "-b:a", bitrate]
    if title:
        args += ["-metadata", "title=" + title[:60]]
    args += [mp3_path]
    _run(args)


def to_reference_wav(src_path: str, dst_path: str, max_seconds: float = 15.0) -> float:
    """
    把上傳 / 網頁錄音（webm、mp3、m4a、wav…）整理成「聲音範本」：
    單聲道、24kHz、去頭尾靜音、音量標準化、最長 15 秒。回傳秒數。
    """
    af = (
        "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1,"
        "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.2,areverse"
    )
    _run([ffmpeg_exe(), "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", src_path,
          "-af", af, "-ac", "1", "-ar", "24000", "-t", str(max_seconds), "-sample_fmt", "s16", dst_path])
    _normalize_wav(dst_path)
    return wav_seconds(dst_path)


def _normalize_wav(path: str, target_rms_db: float = -20.0, peak: float = 0.89) -> None:
    """音量標準化：先把平均音量拉到約 -20 dBFS，再確保峰值不破音。"""
    import numpy as np
    import soundfile as sf
    y, sr = sf.read(path, dtype="float32")
    if y.size == 0:
        return
    rms = float(np.sqrt(np.mean(y ** 2))) or 1e-6
    y = y * (10 ** (target_rms_db / 20) / rms)
    m = float(np.max(np.abs(y)))
    if m > peak:
        y = y * (peak / m)
    sf.write(path, y.astype("float32"), sr, subtype="PCM_16")


def wav_seconds(path: str) -> float:
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


def mp3_seconds(path: str) -> float:
    """用 ffmpeg 讀 MP3 長度（沒有 ffprobe 也能用）。"""
    flags = 0x08000000 if os.name == "nt" else 0
    p = subprocess.run([ffmpeg_exe(), "-nostdin", "-hide_banner", "-i", path], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, creationflags=flags)
    txt = p.stderr.decode("utf-8", "ignore")
    import re
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", txt)
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)
