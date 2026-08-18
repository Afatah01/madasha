import os
import time
import threading
import zipfile
from flask import Flask, request, send_file, jsonify, abort

app = Flask(__name__)

DIR = "/tmp/madasha"
os.makedirs(DIR, exist_ok=True)
MIC_KEY = os.environ.get("MIC_KEY", "mic-change-me")
LISTEN_KEY = os.environ.get("LISTEN_KEY", "listen-change-me")
KEEP = 12 * 3600  # keep last 12 hours

index = {"chunks": []}
state = {"last": 0}
lock = threading.Lock()


@app.route("/")
def home():
    return "madasha ok"


@app.route("/up", methods=["POST"])
def up():
    if request.args.get("key") != MIC_KEY:
        abort(403)
    f = request.files.get("audio")
    if not f:
        return "no audio", 400
    ext = request.form.get("ext", "webm")
    if ext not in ("webm", "m4a", "ogg", "mp4"):
        ext = "webm"
    ts = time.time()
    seq = request.form.get("seq", str(int(ts)))
    try:
        name = "c%06d.%s" % (int(seq), ext)
    except ValueError:
        name = "c%d.%s" % (int(ts * 1000) % 10**9, ext)
    f.save(os.path.join(DIR, name))
    with lock:
        index["chunks"].append({"ts": ts, "name": name})
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
        index["chunks"] = keep[-3000:]
    return "ok"


@app.route("/index.json")
def idx():
    if request.args.get("key") != LISTEN_KEY:
        abort(403)
    with lock:
        return jsonify({
            "chunks": index["chunks"],
            "live": (time.time() - state["last"]) < 40,
            "last": state["last"],
        })


@app.route("/f/<name>")
def serve_file(name):
    if request.args.get("key") != LISTEN_KEY:
        abort(403)
    if "/" in name or ".." in name:
        abort(400)
    path = os.path.join(DIR, name)
    if not os.path.exists(path):
        abort(404)
    mime = "audio/webm" if name.endswith(".webm") else "audio/mp4"
    return send_file(path, mimetype=mime)


@app.route("/archive.zip")
def archive():
    # full meeting recording - chairman only (MIC_KEY)
    if request.args.get("key") != MIC_KEY:
        abort(403)
    with lock:
        chunks = list(index["chunks"])
    if not chunks:
        return "no recording yet", 404
    zpath = os.path.join("/tmp", "kulan-%d.zip" % int(time.time()))
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for c in chunks:
            p = os.path.join(DIR, c["name"])
            if os.path.exists(p):
                arc = time.strftime("%H-%M-%S_", time.localtime(c["ts"])) + c["name"]
                z.write(p, arc)
    return send_file(zpath, mimetype="application/zip", as_attachment=True,
                     download_name="kulan-%s.zip" % time.strftime("%Y%m%d-%H%M"))


MIC_HTML = """<!doctype html><html><head>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Madasha</title>
<style>
body{font-family:sans-serif;background:#0d1b2a;color:#eee;text-align:center;padding:24px}
h2{color:#ffd166;font-size:22px}
.card{background:#1b2a3d;border-radius:14px;padding:18px;margin:16px 0}
#st{margin:16px 0;font-size:18px;min-height:24px}
.dot{display:inline-block;width:14px;height:14px;border-radius:50%;background:#ef476f;
 animation:p 1s infinite}
@keyframes p{50%{opacity:.2}}
.err{color:#ef476f}
.note{color:#8d99ae;font-size:14px;margin-top:30px}
a{color:#8ecae6}
</style></head><body>
<h2>Madasha Wada-Tashiga iyo Horumarinta Ummada</h2>
<div class=card>
<p>Waan kugu casuumaynaa kulamadeena.<br>
Microphone-ka ayaa hadda furmaya.<br>
Marka la weydiiyo, riix <b>Allow / Ogolaado</b>.</p>
</div>
<div id=st>Microphone-ka ayaa la furayaa...</div>
<div id=err class=err></div>
<p class=note>Si aad u joojiso kulanka: xir boggan (close this page).</p>
<p class=note><a id=dl href="#">&#11015; Soo dejiso duubista kulanka (Download full recording)</a></p>
<script>
var KEY=new URLSearchParams(location.search).get('key')||'';
var seq=0,rec=null,EXT='webm',stream=null;
document.getElementById('dl').href='/archive.zip?key='+KEY;
function st(t){document.getElementById('st').innerHTML=t}
function start(){
 navigator.mediaDevices.getUserMedia({audio:true}).then(function(s){
  stream=s;
  var MT='';
  var cands=['audio/webm;codecs=opus','audio/webm','audio/mp4'];
  for(var i=0;i<cands.length;i++){if(window.MediaRecorder&&MediaRecorder.isTypeSupported(cands[i])){MT=cands[i];break}}
  if(MT.indexOf('mp4')>=0)EXT='m4a';
  rec=new MediaRecorder(s,MT?{mimeType:MT}:undefined);
  rec.ondataavailable=function(e){
   if(e.data&&e.data.size>0){
    seq++;
    var fd=new FormData();
    fd.append('audio',e.data,'c.'+EXT);
    fd.append('seq',seq);
    fd.append('ext',EXT);
    fetch('/up?key='+KEY,{method:'POST',body:fd}).then(function(r){
     st(r.ok?('<span class=dot></span> <b>TOOS AH (LIVE)</b><br>Microphone-kagu wuu furan yahay. Xubnaha way ku maqlayaan. ('+seq+')')
            :('Khalad dirid: '+r.status));
    }).catch(function(){st('Internet-ku wuu go&rsquo;day - isku day...')});
   }};
  rec.onstop=function(){if(rec&&stream)rec.start()};
  rec.start();
  setInterval(function(){if(rec&&rec.state==='recording')rec.stop()},10000);
  st('<span class=dot></span> <b>TOOS AH (LIVE)</b><br>Microphone-kagu wuu furan yahay.');
 }).catch(function(e){
  document.getElementById('err').textContent='Microphone-ka lama ogolaan. Dib u fur bogga kadibna riix Allow / Ogolaado.';
 });
}
window.addEventListener('load',start);
</script></body></html>"""

LISTEN_HTML = """<!doctype html><html><head>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Dhegayso Kulanka</title>
<style>
body{font-family:sans-serif;background:#0d1b2a;color:#eee;margin:0;padding:16px}
h2{color:#ffd166;font-size:22px;text-align:center}
button{font-size:20px;padding:14px 20px;border-radius:12px;border:0;background:#06d6a0;color:#031;width:100%}
.c{padding:10px;border-bottom:1px solid #1b2a3d}
a{color:#8ecae6;text-decoration:none}
#lv{text-align:center;margin:12px 0;font-size:17px}
.on{color:#06d6a0}.off{color:#8d99ae}
</style></head><body>
<h2>Madasha Wada-Tashiga iyo Horumarinta Ummada</h2>
<div id=lv class=off>Kulanka weli ma bilaaban...</div>
<button id=b>&#9654; DHAGAYSO (Listen LIVE)</button>
<div id=s style="margin:10px 0"></div><div id=list></div>
<script>
var KEY=new URLSearchParams(location.search).get('key')||'';
var A=new Audio(),played={},live=false;
function fmt(t){return new Date(t*1000).toLocaleTimeString()}
document.getElementById('b').onclick=function(){live=true;this.textContent='&#9679; LIVE - DHAGAYSO';poll()};
A.onended=function(){if(live)setTimeout(poll,1500)};
function poll(){
 fetch('/index.json?key='+KEY).then(function(r){return r.json()}).then(function(d){
  var L=document.getElementById('lv');
  if(d.live){L.className='on';L.textContent='Kulanku wuu socdaa - TOOS AH';}
  else{L.className='off';L.textContent=d.last?('Kulanka wuu istaagay / weli ma bilaaban'):('Kulanka weli ma bilaaban...');}
  var h='';
  for(var i=d.chunks.length-1;i>=0;i--){var c=d.chunks[i];
   h+='<div class=c><a href="javascript:manual(\''+c.name+'\')">&#9654; '+fmt(c.ts)+'</a>'
     +' &nbsp;<a href="/f/'+c.name+'?key='+KEY+'">kaydso</a></div>'}
  document.getElementById('list').innerHTML=h;
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


@app.route("/mic")
def mic_page():
    if request.args.get("key") != MIC_KEY:
        abort(403)
    return MIC_HTML


@app.route("/listen")
@app.route("/kaqaybgal")
def listen_page():
    if request.args.get("key") != LISTEN_KEY:
        abort(403)
    return LISTEN_HTML


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
