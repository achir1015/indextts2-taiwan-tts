# -*- coding: utf-8 -*-
"""
語音引擎（錄音師）
==================
  IndexTTS2Engine : 台灣國語。用 8 位配音員的樣本做「零樣本聲音複製」，
                    口音、音色、年齡感都來自樣本，所以樣本是台灣人 → 講出來就是台灣腔。
  HokkienEngine   : 台灣閩南語（選配、實驗性）。IndexTTS2 本身不會講台語，
                    所以另外接一個閩南語模型（MERaLiON OmniVoice Hokkien），
                    它跑在獨立的 Python 環境（hokkien_worker.py, port 7862），這裡只負責轉送工單。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import urllib.request
from typing import Optional

from domain import SynthesisOrder

REPO_DIR = os.environ.get("INDEXTTS_REPO", os.path.join(os.path.expanduser("~"), "index-tts"))
MODEL_DIR = os.environ.get("INDEXTTS_MODEL_DIR", os.path.join(REPO_DIR, "checkpoints_2"))
HOKKIEN_URL = os.environ.get("HOKKIEN_URL", "http://127.0.0.1:7862")


class EngineStatus:
    NOT_INSTALLED = "not_installed"
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"


class IndexTTS2Engine:
    """包裝官方 IndexTTS2：負責載入模型、繁轉簡、依工單合成 WAV。"""

    def __init__(self):
        self.status = EngineStatus.LOADING
        self.message = "模型載入中…"
        self.device = "?"
        self._tts = None
        self._lock = threading.Lock()      # 一張顯卡一次只做一張工單
        self._t2s = None
        # 目前進度（給網頁顯示）：value 0~1、desc 說明、started 開始時間
        self.progress = {"busy": False, "value": 0.0, "desc": "", "started": 0.0}

    def _on_progress(self, value, desc=None):
        """官方程式每做完一段就會呼叫這裡（原本是給 Gradio 進度條用的）。"""
        self.progress.update(value=float(value), desc=desc or "")
        if desc and "synthesis" in desc:
            seg = desc.split()[-1].rstrip(".")
            print(f">> 進度：第 {seg} 段，已花 {time.time() - self.progress['started']:.0f} 秒", flush=True)

    # ---- 載入（在背景執行緒進行，網頁可以先打開） ----
    def load_async(self) -> None:
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        try:
            os.environ.setdefault("HF_HUB_CACHE", os.path.join(MODEL_DIR, "hf_cache"))
            if REPO_DIR not in sys.path:
                sys.path.insert(0, REPO_DIR)
            cwd = os.getcwd()
            os.chdir(REPO_DIR)   # 官方程式有些路徑是相對於 repo 的
            try:
                import torch
                from indextts.infer_v2 import IndexTTS2
                use_cuda = torch.cuda.is_available()
                self.device = torch.cuda.get_device_name(0) if use_cuda else "CPU（會很慢）"
                self._tts = IndexTTS2(
                    cfg_path=os.path.join(MODEL_DIR, "config.yaml"),
                    model_dir=MODEL_DIR,
                    use_fp16=use_cuda,          # 有顯卡就用半精度：更快、更省顯存
                    use_cuda_kernel=False,
                    use_deepspeed=False,
                )
            finally:
                os.chdir(cwd)
            self._tts.gr_progress = self._on_progress
            self._init_t2s()
            self.status = EngineStatus.READY
            self.message = "就緒"
        except Exception as e:  # noqa
            traceback.print_exc()
            self.status = EngineStatus.ERROR
            self.message = "模型載入失敗：%s" % e

    def _init_t2s(self) -> None:
        """模型主要用簡體字訓練，繁體字先轉簡體再送進模型，發音比較準。"""
        try:
            import opencc  # opencc-python-reimplemented
            self._t2s = opencc.OpenCC("t2s")
        except Exception:
            self._t2s = None

    def prepare_text(self, text: str) -> str:
        return self._t2s.convert(text) if self._t2s else text

    # ---- 合成 ----
    def synthesize(self, order: SynthesisOrder, ref_wav: str, out_wav: str) -> None:
        if self.status != EngineStatus.READY:
            raise RuntimeError(self.message)
        text = self.prepare_text(order.text)
        vec = order.emotion_vector()
        with self._lock:
            self.progress.update(busy=True, value=0.0, desc="準備中", started=time.time())
            kwargs = dict(
                spk_audio_prompt=ref_wav,
                text=text,
                output_path=out_wav,
                verbose=False,
                max_text_tokens_per_segment=120,
            )
            if vec is not None:
                kwargs["emo_vector"] = self._tts.normalize_emo_vec(vec, apply_bias=True)
            try:
                self._tts.infer(**kwargs)
            finally:
                self.progress.update(busy=False, value=1.0, desc="完成")
        if not os.path.exists(out_wav):
            raise RuntimeError("合成失敗，沒有產生音檔")


class HokkienEngine:
    """台語引擎的「窗口」：把工單轉送給 hokkien_worker.py。"""

    def __init__(self, base_url: str = HOKKIEN_URL):
        self.base_url = base_url.rstrip("/")
        self.status = EngineStatus.NOT_INSTALLED
        self.message = "尚未安裝台語引擎（請執行 install_hokkien.bat）"
        self._lock = threading.Lock()

    def refresh(self) -> None:
        try:
            with urllib.request.urlopen(self.base_url + "/health", timeout=2) as r:
                info = json.loads(r.read().decode("utf-8"))
            self.status = info.get("status", EngineStatus.READY)
            self.message = info.get("message", "就緒")
        except Exception:
            self.status = EngineStatus.NOT_INSTALLED
            self.message = "台語引擎未啟動（需先執行 install_hokkien.bat，之後 start_server.bat 會自動啟動）"

    def synthesize(self, order: SynthesisOrder, ref_wav: str, out_wav: str) -> None:
        self.refresh()
        if self.status != EngineStatus.READY:
            raise RuntimeError(self.message)
        payload = json.dumps({"text": order.text, "ref_wav": ref_wav, "out_wav": out_wav}).encode("utf-8")
        req = urllib.request.Request(self.base_url + "/synth", data=payload,
                                     headers={"Content-Type": "application/json"})
        with self._lock:
            with urllib.request.urlopen(req, timeout=600) as r:
                res = json.loads(r.read().decode("utf-8"))
        if not res.get("ok"):
            raise RuntimeError("台語合成失敗：" + str(res.get("error")))
