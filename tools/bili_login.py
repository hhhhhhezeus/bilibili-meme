#!/usr/bin/env python3
"""B站扫码登录助手：生成二维码 → 轮询 → 抓 SESSDATA → 写入插件配置。"""
import json, subprocess, sys, time, urllib.parse, urllib.request
import qrcode

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
QR_PNG = "/home/hhhhhhezeus/maibot/bili_login_qr.png"
CONTAINER_CFG = "/MaiMBot/plugins/bilibili-meme/config.toml"

def api(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r), r.headers

gen, _ = api("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
if gen.get("code") != 0:
    print("GEN_FAIL", gen); sys.exit(1)
data = gen["data"]
key, url = data["qrcode_key"], data["url"]
qrcode.make(url).save(QR_PNG)
print(f"QR_SAVED {QR_PNG}")

sessdata = ""
deadline = time.time() + 300
last_state = ""
while time.time() < deadline and not sessdata:
    try:
        payload, headers = api(
            f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={key}"
        )
        d = payload.get("data") or {}
        code = d.get("code")
        if code == 0:
            for c in (headers.get_all("Set-Cookie") or []):
                if c.startswith("SESSDATA="):
                    sessdata = c.split(";")[0].split("=", 1)[1]
            if not sessdata:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(d.get("url") or "").query)
                sessdata = (q.get("SESSDATA") or [""])[0]
            break
        state = {86101: "WAITING_SCAN", 86090: "SCANNED_CONFIRM_ON_PHONE", 86038: "EXPIRED"}.get(code, f"CODE_{code}")
        if state != last_state:
            print(state, flush=True)
            last_state = state
        if code == 86038:
            sys.exit(2)
    except Exception as exc:
        print("poll_err", type(exc).__name__, exc, flush=True)
    time.sleep(2)

if not sessdata:
    print("TIMEOUT_NO_SESSDATA"); sys.exit(3)

print(f"SESSDATA_OK len={len(sessdata)}", flush=True)

# 写入容器内的插件配置（以 root 身份，避免属主问题）
py = f'''
import pathlib, re
p = pathlib.Path("{CONTAINER_CFG}")
s = p.read_text(encoding="utf-8")
s2, n = re.subn(r'^sessdata\s*=\s*".*?"', 'sessdata = "{sessdata}"', s, count=1, flags=re.M)
if n == 0:
    s2 = s.replace("llm_task =", 'sessdata = "{sessdata}"\nllm_task =', 1)
p.write_text(s2, encoding="utf-8")
print("CONFIG_WRITTEN", n)
'''
res = subprocess.run(["docker", "exec", "-i", "maim-bot-core", "python3", "-"],
                     input=py, text=True, capture_output=True)
print((res.stdout or "").strip() or (res.stderr or "").strip()[:200])
print("DONE")
