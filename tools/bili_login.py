#!/usr/bin/env python3
"""B站扫码登录助手：生成二维码 → 轮询 → 抓 SESSDATA → 写入插件配置。"""
import argparse, json, re, sys, time, tomllib, urllib.parse, urllib.request
from pathlib import Path

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}

def api(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r), r.headers

def write_sessdata(path, sessdata):
    """只修改 [plugin].sessdata；验证完整 TOML 后才落盘。"""
    source = path.read_text(encoding="utf-8")
    original = tomllib.loads(source)
    if not isinstance(original.get("plugin", {}).get("sessdata"), str):
        raise ValueError("配置缺少 [plugin].sessdata 字符串，请使用示例配置")
    lines = source.splitlines(keepends=True)
    section = ""
    count = 0
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            section = line.split("#", 1)[0].strip()
        if section == "[plugin]" and re.match(r'^\s*sessdata\s*=', line):
            lines[index] = "sessdata = " + json.dumps(sessdata, ensure_ascii=True) + "\n"
            count += 1
    updated = "".join(lines)
    expected = {**original, "plugin": {**original["plugin"], "sessdata": sessdata}}
    if count != 1 or tomllib.loads(updated) != expected:
        raise ValueError("无法安全更新 sessdata，请使用示例中的单行配置格式")
    path.write_text(updated, encoding="utf-8")


def main():
    import qrcode
    parser = argparse.ArgumentParser(description="扫码登录并写入本地插件配置")
    parser.add_argument("--config", required=True, type=Path, help="插件 config.toml 路径（容器部署时使用挂载到宿主机的路径）")
    parser.add_argument("--qr", type=Path, default=Path("bili_login_qr.png"), help="二维码图片保存路径")
    args = parser.parse_args()
    if not args.config.is_file():
        parser.error("配置文件不存在，请先复制 config.example.toml 并提供其路径")
    QR_PNG = args.qr

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

    write_sessdata(args.config, sessdata)
    print("CONFIG_WRITTEN", args.config)


if __name__ == "__main__":
    main()
