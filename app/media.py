"""Ingestao de video: arquivo fonte -> HLS (h264/aac, 720p no maximo) -> /data/media/<aula>/. Uma fila, um trabalhador."""
import json
import os
import queue
import shutil
import subprocess
import threading

from . import db

MEDIA = os.path.join(db.DATA, "media")
SRC = os.path.join(db.DATA, "src")
_q = queue.Queue()
_started = False


def enqueue(lesson_id, src_path):
    global _started
    db.x("UPDATE lessons SET video_status='processing', error='' WHERE id=?", (lesson_id,))
    _q.put((lesson_id, src_path))
    if not _started:
        _started = True
        threading.Thread(target=_worker, daemon=True).start()


def resume_pending():
    """Reinicio do container no meio de um job: refaz o que ficou 'processing' (idempotente)."""
    for r in db.q("SELECT id FROM lessons WHERE video_status='processing'"):
        for f in os.listdir(SRC) if os.path.isdir(SRC) else []:
            if f.split(".")[0] == str(r["id"]):
                enqueue(r["id"], os.path.join(SRC, f))
                break
        else:
            db.x("UPDATE lessons SET video_status='error', error='arquivo fonte ausente' WHERE id=?", (r["id"],))


def _worker():
    while True:
        lid, src = _q.get()
        try:
            _transcode(lid, src)
        except Exception as e:  # o aluno nao ve erro tecnico; o admin ve no painel
            db.x("UPDATE lessons SET video_status='error', error=? WHERE id=?", (str(e)[:300], lid))


def _probe(src):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", src],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError("ffprobe: arquivo nao e um video valido")
    j = json.loads(out.stdout)
    has_v = any(s.get("codec_type") == "video" for s in j.get("streams", []))
    has_a = any(s.get("codec_type") == "audio" for s in j.get("streams", []))
    if not has_v:
        raise RuntimeError("arquivo sem faixa de video")
    return float(j["format"].get("duration") or 0), has_a


def _transcode(lid, src):
    dur, has_a = _probe(src)
    tmp = os.path.join(MEDIA, f"{lid}.tmp")
    final = os.path.join(MEDIA, str(lid))
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-map", "0:v:0"]
    if has_a:
        cmd += ["-map", "0:a:0"]
    cmd += ["-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-g", "96", "-keyint_min", "96", "-sc_threshold", "0"]
    if has_a:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    cmd += ["-f", "hls", "-hls_time", "4", "-hls_playlist_type", "vod", "-hls_flags", "independent_segments",
            "-hls_segment_filename", os.path.join(tmp, "seg_%05d.ts"), os.path.join(tmp, "index.m3u8")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60 * 3)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr[-250:])
    poster = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(min(3, dur / 3)), "-i", src, "-frames:v", "1",
                             "-vf", "scale=640:-2", os.path.join(tmp, "poster.jpg")], capture_output=True, text=True, timeout=120)
    if not os.path.isfile(os.path.join(tmp, "index.m3u8")):
        raise RuntimeError("playlist nao gerada")
    shutil.rmtree(final, ignore_errors=True)  # troca atomica: o aluno nunca ve meia versao
    os.rename(tmp, final)
    db.x("UPDATE lessons SET video_status='ready', duration=?, error='' WHERE id=?", (dur, lid))
