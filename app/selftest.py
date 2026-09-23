# -*- coding: utf-8 -*-
# 安裝完成後的自我測試：載入 IndexTTS-2 模型並合成一段語音
import os, sys, time
REPO = os.path.join(os.path.expanduser("~"), "index-tts")
MODEL_DIR = os.path.join(REPO, "checkpoints_2")
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("HF_HUB_CACHE", os.path.join(MODEL_DIR, "hf_cache"))

import torch
print("torch", torch.__version__, "cuda:", torch.cuda.is_available())
if torch.cuda.is_available():
    p = torch.cuda.get_device_properties(0)
    print("GPU:", p.name, round(p.total_memory / 1024**3, 1), "GB")

from indextts.utils.examples_downloader import ensure_examples_available
ensure_examples_available()

from indextts.infer_v2 import IndexTTS2
t = time.time()
tts = IndexTTS2(cfg_path=os.path.join(MODEL_DIR, "config.yaml"), model_dir=MODEL_DIR,
                use_fp16=torch.cuda.is_available(), use_cuda_kernel=False, use_deepspeed=False)
print("model load sec:", round(time.time() - t, 1))
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "selftest.wav")
t = time.time()
tts.infer(spk_audio_prompt=os.path.join(REPO, "examples", "voice_01.wav"),
          text="大家好，這是安裝完成後的測試語音。", output_path=out, verbose=False)
print("infer sec:", round(time.time() - t, 1))
print("SELFTEST_OK", os.path.abspath(out))
