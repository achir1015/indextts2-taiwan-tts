# -*- coding: utf-8 -*-
"""
網頁伺服器（櫃台）
==================
網頁 → 送出「配音工單」→ 櫃台（這支程式）找配音員樣本 → 交給錄音師（引擎）→ 後製成 MP3 → 回傳下載連結

啟動方式：雙擊 start_server.bat，然後打開 http://127.0.0.1:7861
環境變數：
  TTS_PORT       連接埠（預設 7861）
  TTS_PASSWORD   設定後，網頁需輸入此密碼才能使用（開放到網路上時務必設定）
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import tempfile
import threading
import time
import re
import uuid

import numpy as np
from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import audio
from domain import (APP_DIR, OUTPUTS_DIR, VOICES_DIR, EMOTIONS, LANG_HOKKIEN, LANG_MANDARIN,
                    SynthesisOrder, SynthesisResult, VoiceLibrary)
from engines import EngineStatus, HokkienEngine, IndexTTS2Engine
import drama

PASSWORD = os.environ.get("TTS_PASSWORD", "").strip()
HISTORY_PATH = os.path.join(OUTPUTS_DIR, "history.json")
CANDIDATES_DIR = os.path.join(VOICES_DIR, "candidates")
os.makedirs(OUTPUTS_DIR, exist_ok=True)

app = FastAPI(title="台灣語音 MP3 工具")
library = VoiceLibrary()
mandarin = IndexTTS2Engine()
hokkien = HokkienEngine()


# ------------------------------------------------------------
# 成品紀錄（history.json）
# ------------------------------------------------------------
class History:
    def __init__(self, path: str, keep: int = 200):
        self.path, self.keep = path, keep
        self._lock = threading.Lock()
        self.items = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.items = json.load(f)
            except Exception:
                self.items = []

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=1)

    def add(self, r: SynthesisResult):
        with self._lock:
            self.items.insert(0, asdict(r))
            # 超過保留數量就刪掉最舊的檔案
            for old in self.items[self.keep:]:
                try:
                    os.remove(os.path.join(OUTPUTS_DIR, old["mp3_file"]))
                except OSError:
                    pass
            self.items = self.items[: self.keep]
            self._save()

    def remove(self, rid: str):
        with self._lock:
            for it in self.items:
                if it["id"] == rid:
                    try:
                        os.remove(os.path.join(OUTPUTS_DIR, it["mp3_file"]))
                    except OSError:
                        pass
            self.items = [it for it in self.items if it["id"] != rid]
            self._save()


history = History(HISTORY_PATH)


# ------------------------------------------------------------
# 簡易密碼保護（只有設定 TTS_PASSWORD 才啟用）
# ------------------------------------------------------------
@app.middleware("http")
async def password_guard(request: Request, call_next):
    path = request.url.path
    if PASSWORD and (path.startswith("/api/") or path.startswith("/outputs/")) and path != "/api/status":
        key = request.headers.get("x-access-key") or request.query_params.get("key") \
            or request.cookies.get("tts_key")
        if key != PASSWORD:
            return JSONResponse({"detail": "需要密碼"}, status_code=401)
    return await call_next(request)


@app.on_event("startup")
def _startup():
    mandarin.load_async()
    hokkien.refresh()


# ------------------------------------------------------------
# API
# ------------------------------------------------------------
@app.get("/api/status")
def status():
    hokkien.refresh()
    return {
        "password_required": bool(PASSWORD),
        "engines": {
            LANG_MANDARIN: {"status": mandarin.status, "message": mandarin.message, "device": mandarin.device},
            LANG_HOKKIEN: {"status": hokkien.status, "message": hokkien.message},
        },
        "emotions": ["自然"] + list(EMOTIONS.keys()),
    }


@app.get("/api/progress")
def progress():
    """產生中的進度：第幾段 / 共幾段、已花時間、預估剩餘時間。"""
    p = dict(mandarin.progress)
    elapsed = time.time() - p["started"] if p["busy"] else 0.0
    p["elapsed"] = round(elapsed)
    seg = None
    desc = p.get("desc", "")
    if "synthesis" in desc:
        try:
            a, b = desc.split()[-1].rstrip(".").split("/")
            seg = (int(a), int(b))
        except ValueError:
            pass
    p["segment"] = seg
    # 用「已完成段數」估算剩餘時間
    if seg and seg[0] > 1 and elapsed > 0:
        per = elapsed / (seg[0] - 1)
        p["eta"] = round(per * (seg[1] - seg[0] + 1))
    else:
        p["eta"] = None
    return p


@app.get("/api/voices")
def list_voices():
    out = []
    for t in library.all():
        d = t.to_public()
        d["candidates"] = sorted(os.path.basename(p) for p in glob.glob(os.path.join(CANDIDATES_DIR, t.id + "__*.wav")))
        out.append(d)
    return out


@app.get("/api/voices/{tid}/sample")
def get_sample(tid: str):
    try:
        t = library.get(tid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    if not t.ready:
        raise HTTPException(404, "這個聲音還沒有樣本")
    return FileResponse(t.ref_path, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.get("/api/candidates/{name}")
def get_candidate(name: str):
    p = os.path.join(CANDIDATES_DIR, os.path.basename(name))
    if not os.path.exists(p):
        raise HTTPException(404, "找不到候選樣本")
    return FileResponse(p, media_type="audio/wav")


@app.post("/api/voices/{tid}/use_candidate/{name}")
def use_candidate(tid: str, name: str):
    src = os.path.join(CANDIDATES_DIR, os.path.basename(name))
    if not os.path.exists(src) or not os.path.basename(name).startswith(tid + "__"):
        raise HTTPException(404, "找不到候選樣本")
    dst_name = f"{tid}.wav"
    shutil.copyfile(src, os.path.join(VOICES_DIR, dst_name))
    meta_path = src[:-4] + ".json"
    source, placeholder = "開放資料集候選樣本", False
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            m = json.load(f)
        source, placeholder = m.get("source", source), m.get("placeholder", False)
    return library.assign_sample(tid, dst_name, source, placeholder).to_public()


@app.post("/api/voices/{tid}/sample")
async def upload_sample(tid: str, file: UploadFile = File(...)):
    """上傳或網頁錄音的樣本（任何常見格式都可以），自動整理成 24kHz 單聲道。"""
    try:
        library.get(tid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    suffix = os.path.splitext(file.filename or "rec.webm")[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name
    dst_name = f"{tid}.wav"
    try:
        secs = audio.to_reference_wav(tmp_path, os.path.join(VOICES_DIR, dst_name))
    except Exception as e:
        raise HTTPException(400, f"無法讀取這個音檔：{e}")
    finally:
        os.remove(tmp_path)
    if secs < 2.0:
        raise HTTPException(400, f"樣本太短（{secs:.1f} 秒），請錄 5~15 秒清楚的說話聲")
    return library.assign_sample(tid, dst_name, "自行上傳 / 錄音", False).to_public()


class NewVoice(BaseModel):
    name: str


@app.post("/api/voices")
def add_voice(v: NewVoice):
    name = v.name.strip()[:20]
    if not name:
        raise HTTPException(400, "請輸入名稱")
    return library.add_custom(name).to_public()


@app.delete("/api/voices/{tid}")
def delete_voice(tid: str):
    try:
        library.remove_custom(tid)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class TTSRequest(BaseModel):
    text: str
    voice_id: str
    language: str = LANG_MANDARIN
    emotion: str = "自然"
    emotion_strength: float = 0.6
    speed: float = 1.0
    bitrate: str = "192k"


@app.post("/api/tts")
def tts(req: TTSRequest):
    order = SynthesisOrder(**(req.model_dump() if hasattr(req, "model_dump") else req.dict()))
    try:
        order.validate()
        voice = library.get(order.voice_id)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    if not voice.ready:
        raise HTTPException(400, f"「{voice.name}」還沒有聲音樣本，請先到「聲音範本」設定")

    engine = mandarin if order.language == LANG_MANDARIN else hokkien
    rid = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    wav_path = os.path.join(OUTPUTS_DIR, rid + ".wav")
    mp3_name = rid + ".mp3"
    t0 = time.time()
    try:
        engine.synthesize(order, voice.ref_path, wav_path)
        audio.wav_to_mp3(wav_path, os.path.join(OUTPUTS_DIR, mp3_name), order.speed, order.bitrate,
                         title=order.text)
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)

    result = SynthesisResult(
        id=rid, mp3_file=mp3_name, text=order.text, voice_id=voice.id, voice_name=voice.name,
        language=order.language, emotion=order.emotion, speed=order.speed,
        seconds=round(audio.mp3_seconds(os.path.join(OUTPUTS_DIR, mp3_name)), 2),
        elapsed=round(time.time() - t0, 1),
    )
    history.add(result)
    return asdict(result)


@app.get("/api/history")
def get_history():
    return history.items


@app.delete("/api/history/{rid}")
def delete_history(rid: str):
    history.remove(rid)
    return {"ok": True}


# ------------------------------------------------------------
# 小說劇場
# ------------------------------------------------------------
def _drama_done(job, script):
    """劇場完成 → 也放進「產生紀錄」，方便一起下載管理。"""
    names = "、".join(script.speakers()[:5])
    history.add(SynthesisResult(
        id=job.id, mp3_file=job.mp3_file, text=f"【小說劇場】{job.title}（{names}）",
        voice_id="drama", voice_name="小說劇場", language=LANG_MANDARIN, emotion="多角色",
        speed=1.0, seconds=job.seconds, elapsed=round(job.finished_at - job.started_at, 1)))


studio = drama.DramaStudio(library, {LANG_MANDARIN: mandarin, LANG_HOKKIEN: hokkien}, audio.wav_to_mp3,
                           on_done=_drama_done)


def _parser():
    voices = library.all()
    return drama.ScriptParser([v.id for v in voices], {v.name: v.id for v in voices})


def _sec_per_char():
    return 9.0 if "CPU" in (mandarin.device or "") else 0.35


class DramaParseReq(BaseModel):
    script: str
    cast: dict = {}          # 網頁上調整過的演員設定 {角色名: {voice_id, language, pan, volume}}


def _build_script(req: DramaParseReq) -> "drama.Script":
    sc = _parser().parse(req.script)
    for name, o in (req.cast or {}).items():
        c = sc.cast.get(name)
        if not c or not isinstance(o, dict):
            continue
        if o.get("voice_id") in {v.id for v in library.all()}:
            c.voice_id = o["voice_id"]
        if o.get("language") in (LANG_MANDARIN, LANG_HOKKIEN):
            c.language = o["language"]
        try:
            c.pan = max(-1.0, min(1.0, float(o.get("pan", c.pan))))
            c.volume = max(0.2, min(2.0, float(o.get("volume", c.volume))))
        except (TypeError, ValueError):
            pass
    return sc


@app.post("/api/drama/parse")
def drama_parse(req: DramaParseReq):
    sc = _build_script(req)
    out = sc.to_public()
    out["estimate"] = studio.estimate(sc, _sec_per_char())
    return out


class DramaConvertReq(BaseModel):
    prose: str
    names: str = ""
    narration: bool = True


@app.post("/api/drama/convert")
def drama_convert(req: DramaConvertReq):
    nc = drama.NovelConverter()
    names = [n.strip() for n in re.split(r"[,，、\s]+", req.names or "") if n.strip()]
    if not names:
        names = nc.detect_names(req.prose)
    return {"script": nc.convert(req.prose, names, req.narration), "names": names}


class DramaJobReq(DramaParseReq):
    title: str = ""
    pace: str = "一般"
    room: str = "無"
    ambience_volume: float = 1.0
    stereo: bool = True


@app.post("/api/drama/jobs")
def drama_submit(req: DramaJobReq):
    sc = _build_script(req)
    if not sc.all_lines():
        raise HTTPException(400, "劇本裡沒有任何台詞")
    if len(sc.all_lines()) > 300:
        raise HTTPException(400, "台詞太多（上限 300 句），請分成幾集")
    for c in sc.cast.values():
        try:
            if not library.get(c.voice_id).ready:
                raise HTTPException(400, f"角色「{c.name}」用的聲音還沒有樣本")
        except KeyError:
            raise HTTPException(400, f"角色「{c.name}」沒有指定聲音")
        if c.language == LANG_HOKKIEN:
            hokkien.refresh()
            if hokkien.status != EngineStatus.READY:
                raise HTTPException(400, f"角色「{c.name}」設定講台語，但台語引擎尚未啟動")
    settings = drama.MixSettings(pace=req.pace if req.pace in drama.PACE_GAP else "一般",
                                 room=req.room if req.room in ("無", "房間", "大廳") else "無",
                                 ambience_volume=max(0.0, min(2.0, req.ambience_volume)), stereo=req.stereo)
    job = studio.submit(sc, settings, (req.title or "小說劇場").strip()[:30])
    return job.to_public()


@app.get("/api/drama/jobs")
def drama_jobs():
    jobs = sorted(studio.jobs.values(), key=lambda j: -j.created_at)[:20]
    return [j.to_public() for j in jobs]


@app.get("/api/drama/jobs/{jid}")
def drama_job(jid: str):
    j = studio.jobs.get(jid)
    if not j:
        raise HTTPException(404, "找不到這個工單（伺服器重新啟動過？）")
    return j.to_public()


@app.get("/api/ambience")
def ambience_list():
    return {"builtin": drama.BUILTIN_AMBIENCE, "custom": drama.list_custom_ambience()}


@app.get("/api/ambience/preview/{name}")
def ambience_preview(name: str):
    """試聽 8 秒背景音。"""
    import soundfile as sf
    samples = []
    for t in library.all():
        if t.ready:
            try:
                samples.append(drama.read_mono(t.ref_path))
            except Exception:
                pass
    y = drama.make_ambience(name, 8, samples)[: 8 * drama.SR]
    fade = int(0.5 * drama.SR)
    y[:fade] *= np.linspace(0, 1, fade)
    y[-fade:] *= np.linspace(1, 0, fade)
    y = y * (0.5 / (float(np.max(np.abs(y))) or 1.0))
    p = os.path.join(tempfile.gettempdir(), f"amb_preview_{uuid.uuid4().hex[:6]}.wav")
    sf.write(p, y, drama.SR)
    return FileResponse(p, media_type="audio/wav")


@app.post("/api/ambience")
async def ambience_upload(name: str = "", file: UploadFile = File(...)):
    """上傳自己的背景音（例如從 freesound.org 下載的 CC0 錄音）。"""
    name = re.sub(r"[\\/:*?\"<>|\s]", "", name or os.path.splitext(file.filename or "背景")[0])[:20] or "自訂背景"
    os.makedirs(drama.AMBIENCE_DIR, exist_ok=True)
    suffix = os.path.splitext(file.filename or "a.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name
    try:
        audio._run([audio.ffmpeg_exe(), "-nostdin", "-y", "-loglevel", "error", "-i", tmp_path,
                    "-ac", "1", "-ar", str(drama.SR), "-t", "300", os.path.join(drama.AMBIENCE_DIR, name + ".wav")])
    except Exception as e:
        raise HTTPException(400, f"無法讀取這個音檔：{e}")
    finally:
        os.remove(tmp_path)
    return {"ok": True, "name": name}


# ------------------------------------------------------------
# 靜態檔案 + 手機捷徑（PWA）
# ------------------------------------------------------------
STATIC_DIR = os.path.join(APP_DIR, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(os.path.join(STATIC_DIR, "manifest.webmanifest"), media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # Service Worker 必須放在網站根目錄，才能管整個網站
    return FileResponse(os.path.join(STATIC_DIR, "sw.js"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(STATIC_DIR, "icons", "favicon.ico"), media_type="image/x-icon")

app.mount("/outputs", StaticFiles(directory=OUTPUTS_DIR), name="outputs")


@app.get("/")
def index():
    return FileResponse(os.path.join(APP_DIR, "static", "index.html"),
                        headers={"Cache-Control": "no-store"})


class _Tee:
    """同時輸出到視窗與 logs/server_log.txt（方便遠端查看錯誤）。"""
    def __init__(self, stream, path):
        self.stream, self.f = stream, open(path, "a", encoding="utf-8", buffering=1)

    def write(self, s):
        try:
            self.stream.write(s)
        except Exception:
            pass
        self.f.write(s)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass
        self.f.flush()

    def isatty(self):
        return False


if __name__ == "__main__":
    import sys
    import uvicorn
    _log = os.path.join(os.path.dirname(APP_DIR), "logs", "server_log.txt")
    os.makedirs(os.path.dirname(_log), exist_ok=True)
    sys.stdout, sys.stderr = _Tee(sys.stdout, _log), _Tee(sys.stderr, _log)
    port = int(os.environ.get("TTS_PORT", "7861"))
    print("=" * 60)
    print(f" 台灣語音 MP3 工具： http://127.0.0.1:{port}")
    print(" （同一個區域網路的其他電腦：http://<這台電腦的IP>:%d）" % port)
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
