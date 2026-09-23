# -*- coding: utf-8 -*-
"""
台語（閩南語）引擎工作站 —— 實驗性
====================================
在獨立的 Python 環境（hokkien_env）執行，避免跟 IndexTTS2 的套件版本打架。
主網站（server.py）把工單用 HTTP 丟到這裡：POST /synth {text, ref_wav, out_wav}

模型：MERaLiON/MERaLiON-OmniVoice-Hokkien-TTS（以新加坡福建話訓練，屬閩南語）
搭配台灣人的聲音樣本做聲音複製，腔調會往樣本靠近，但不是完整的台灣台語。
"""
import json
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = os.environ.get("HOKKIEN_MODEL", "MERaLiON/MERaLiON-OmniVoice-Hokkien-TTS")
PORT = int(os.environ.get("HOKKIEN_PORT", "7862"))

# Windows 在一般權限下不能建立 symlink，huggingface 下載會失敗 → 強制改用「複製檔案」
try:
    import huggingface_hub.file_download as _fd
    _fd.are_symlinks_supported = lambda *a, **k: False
except Exception:
    pass

state = {"status": "loading", "message": "台語模型載入中…"}
model = None
lock = threading.Lock()
prompt_cache = {}   # 同一個樣本檔只做一次聲音特徵（樣本檔更新時間也算進 key）


def load():
    global model
    try:
        import torch
        from omnivoice.models.omnivoice import OmniVoice
        dev = "cuda:0" if torch.cuda.is_available() else "cpu"
        model = OmniVoice.from_pretrained(MODEL_ID, device_map=dev,
                                          dtype=torch.float16 if dev != "cpu" else torch.float32)
        state.update(status="ready", message="就緒")
        print(">> Hokkien model ready on", dev, flush=True)
    except Exception as e:
        traceback.print_exc()
        state.update(status="error", message=f"台語模型載入失敗：{e}")


def synth(text, ref_wav, out_wav):
    import soundfile as sf
    key = (ref_wav, os.path.getmtime(ref_wav)) if ref_wav and os.path.exists(ref_wav) else None
    with lock:
        kwargs = {"text": text, "language": "nan"}
        if key:
            if key not in prompt_cache:
                # 不給逐字稿 → OmniVoice 會自動用 Whisper 聽寫樣本
                prompt_cache[key] = model.create_voice_clone_prompt(ref_audio=ref_wav)
            kwargs["voice_clone_prompt"] = prompt_cache[key]
        audios = model.generate(**kwargs)
    y = audios[0]
    try:
        y = y.detach().float().cpu().numpy()
    except AttributeError:
        pass
    sf.write(out_wav, y.squeeze(), model.sampling_rate)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, state)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/synth"):
            return self._send(404, {"error": "not found"})
        if state["status"] != "ready":
            return self._send(503, {"ok": False, "error": state["message"]})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            synth(req["text"], req.get("ref_wav"), req["out_wav"])
            self._send(200, {"ok": True})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"ok": False, "error": str(e)})

    def log_message(self, *a):
        pass


def selftest(out_wav):
    """install_hokkien.bat 用：下載模型並合成一句測試。"""
    load()
    if state["status"] != "ready":
        raise SystemExit(state["message"])
    synth("你食飽未？今仔日天氣真好。", None, out_wav)
    print("HOKKIEN_OK", out_wav)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
        raise SystemExit(0)
    threading.Thread(target=load, daemon=True).start()
    print(f">> Hokkien worker on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
