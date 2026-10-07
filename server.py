#!/usr/bin/env python3
"""
KeyVault — 本地 API Key 管理与额度查询
单文件后端（仅标准库），数据加密存放在同目录 keys.enc
用法: python3 server.py [端口]   默认 8787，浏览器打开 http://127.0.0.1:8787
"""
import json, os, sys, base64, hashlib, hmac, threading, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(HERE, "keys.enc")
LOCK = threading.Lock()

# ---------- 加密存储：PBKDF2 派生密钥 + AES(如可用) / HMAC 校验流加密 ----------
try:
    from cryptography.fernet import Fernet  # type: ignore
    HAVE_FERNET = True
except ImportError:
    HAVE_FERNET = False

def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)

def _xor_stream(key: bytes, data: bytes) -> bytes:
    out = bytearray(); counter = 0
    while len(out) < len(data):
        block = hashlib.sha256(key + counter.to_bytes(8, "big")).digest()
        out += block; counter += 1
    return bytes(a ^ b for a, b in zip(data, out))

def load_db():
    if not os.path.exists(DB_FILE):
        return {"keys": []}
    raw = open(DB_FILE, "rb").read()
    payload = json.loads(base64.b64decode(raw).decode())
    pw = payload.get("pw", "")
    salt = bytes.fromhex(payload["salt"])
    if HAVE_FERNET:
        key = Fernet(base64.urlsafe_b64encode(_derive(pw, salt)))
        data = json.loads(key.decrypt(payload["data"].encode()).decode())
    else:
        k = _derive(pw, salt)
        body = _xor_stream(k, bytes.fromhex(payload["data"]))
        mac = hmac.new(k, body, hashlib.sha256).hexdigest()
        if mac != payload["mac"]:
            raise ValueError("数据校验失败")
        data = json.loads(body.decode())
    return data

def save_db(db: dict, password: str):
    salt = os.urandom(16)
    if HAVE_FERNET:
        key = Fernet(base64.urlsafe_b64encode(_derive(password, salt)))
        enc = key.encrypt(json.dumps(db, ensure_ascii=False).encode()).decode()
        payload = {"pw": password, "salt": salt.hex(), "data": enc, "v": 2}
    else:
        k = _derive(password, salt)
        body = json.dumps(db, ensure_ascii=False).encode()
        payload = {"pw": password, "salt": salt.hex(),
                   "data": _xor_stream(k, body).hex(),
                   "mac": hmac.new(k, body, hashlib.sha256).hexdigest(), "v": 2}
    with open(DB_FILE, "wb") as f:
        f.write(base64.b64encode(json.dumps(payload).encode()))
    os.chmod(DB_FILE, 0o600)

DB_PASSWORD = "local-vault"  # 本机单用户场景；如需更强保护可改成启动时输入主密码

# ---------- 各厂商余额/校验适配层（端点抄自 Millerderek/Openclaw-API-Balance-Summary，MIT） ----------
def _get(url, key, extra_headers=None, timeout=15):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}", "Accept": "application/json",
        **(extra_headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def _post_json(url, key, body, timeout=25):
    req = urllib.request.Request(url, method="POST",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

PROVIDERS = {
    "deepseek":  {"label": "DeepSeek",  "color": "#4D6BFE",
        "check": lambda k: _check_deepseek(k)},
    "moonshot":  {"label": "Moonshot / Kimi", "color": "#0F172A",
        "check": lambda k: _check_moonshot(k)},
    "openrouter":{"label": "OpenRouter", "color": "#6467F2",
        "check": lambda k: _check_openrouter(k)},
    "together":  {"label": "Together AI", "color": "#0F6FFF",
        "check": lambda k: _check_together(k)},
    "openai":    {"label": "OpenAI",    "color": "#10A37F",
        "check": lambda k: _check_openai(k)},
    "cline":     {"label": "Cline",     "color": "#3B82F6", "console": "https://app.cline.bot/credits"},
    "mimo":      {"label": "小米 MiMo",  "color": "#FF6900",
        "base": "https://api.xiaomimimo.com/v1", "console": "https://mimo.mi.com"},
    "zhipu":     {"label": "智谱 GLM",   "color": "#3859FF", "console": "https://open.bigmodel.cn"},
    "siliconflow":{"label": "SiliconFlow","color": "#7C3AED", "console": "https://cloud.siliconflow.cn"},
    "custom":    {"label": "自定义 / 其他", "color": "#8E8E93"},
}

def _check_deepseek(key):
    d = _get("https://api.deepseek.com/user/balance", key)
    infos = d.get("balance_infos", [])
    if not infos:
        return {"status": "ok", "text": "无余额信息"}
    b = infos[0]
    return {"status": "ok" if d.get("is_available") else "low",
            "text": f"{b['currency']} {float(b['total_balance']):.2f}",
            "detail": f"充值 {b.get('topped_up_balance')} · 赠金 {b.get('granted_balance')}"}

def _check_moonshot(key):
    for host in ("api.moonshot.cn", "api.moonshot.ai"):
        try:
            d = _get(f"https://{host}/v1/users/me/balance", key)
            b = d.get("data", {})
            return {"status": "ok", "text": f"{b.get('currency','CNY')} {float(b.get('available_balance',0)):.2f}",
                    "detail": f"总额度 {b.get('total_balance')}"}
        except urllib.error.HTTPError as e:
            if e.code != 404: raise
    raise RuntimeError("余额接口不可用")

def _check_openrouter(key):
    d = _get("https://openrouter.ai/api/v1/auth/key", key)
    data = d.get("data", {})
    usage, limit = data.get("usage", 0), data.get("limit")
    text = f"$ {float(limit - usage):.2f}" if limit is not None else "无限额度 Key"
    return {"status": "ok", "text": text,
            "detail": f"已用 $ {float(usage):.2f}" + (f" / 上限 $ {limit}" if limit else "")}

def _check_together(key):
    d = _get("https://api.together.xyz/v1/organizations/me", key)
    bal = (d.get("data") or d)
    credits = bal.get("credit_balance", [0])[0] if isinstance(bal.get("credit_balance"), list) else bal.get("credit_balance", 0)
    return {"status": "ok", "text": f"$ {float(credits):.2f}"}

def _check_openai(key):
    # 余额接口需要项目级/组织级权限，401 时降级为仅校验 Key
    try:
        d = _get("https://api.openai.com/v1/dashboard/billing/subscription", key)
        hard = d.get("hard_limit_usd")
        return {"status": "ok", "text": f"额度上限 $ {hard}"}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _get("https://api.openai.com/v1/models", key)  # 仅验证 key 有效性
            return {"status": "ok", "text": "Key 有效", "detail": "余额需组织级 Key，请到 platform.openai.com/usage 查看"}
        raise

def _validate_openai_compatible(key, base):
    """对没有余额接口的厂商：调 /models 验证 Key 有效性"""
    d = _get(f"{base.rstrip('/')}/models", key)
    models = [m.get("id") for m in d.get("data", [])][:8]
    return {"status": "ok", "text": "Key 有效", "detail": "可用模型: " + ", ".join(models) if models else ""}

def query_key(item):
    """返回 {status, text, detail}；status: ok | low | invalid | error | unsupported"""
    pid = item["provider"]
    key = item["key"].strip()
    meta = PROVIDERS.get(pid, PROVIDERS["custom"])
    if not key:
        return {"status": "error", "text": "未填写 Key"}
    fn = meta.get("check")
    try:
        if fn:
            return fn(key)
        base = item.get("base_url") or meta.get("base")
        if base:
            return _validate_openai_compatible(key, base)
        return {"status": "unsupported", "text": "该厂商无公开余额接口",
                "detail": ("请到控制台查看: " + meta["console"]) if meta.get("console") else ""}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"status": "invalid", "text": "Key 无效或已过期"}
        if e.code == 402:
            return {"status": "low", "text": "余额不足"}
        return {"status": "error", "text": f"HTTP {e.code}"}
    except Exception as e:
        return {"status": "error", "text": f"{type(e).__name__}: {e}"}

# ---------- HTTP 服务 ----------
INDEX = open(os.path.join(HERE, "index.html"), encoding="utf-8").read()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = INDEX.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/keys":
            with LOCK:
                db = load_db()
            items = [{**i, "key": self._mask(i["key"])} for i in db["keys"]]
            self._json(200, {"keys": items})
        else:
            self.send_error(404)

    @staticmethod
    def _mask(k):
        return (k[:6] + "•" * 8 + k[-4:]) if len(k) > 12 else "•" * len(k)

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(ln).decode() or "{}")
        if self.path == "/api/keys":
            with LOCK:
                db = load_db()
                old = next((i for i in db["keys"] if i["id"] == body.get("id")), None)
                new_key = (body.get("key") or "").strip()
                if not new_key and old:            # 编辑时留空 = 保持原 Key
                    new_key = old["key"]
                item = {"id": body.get("id") or os.urandom(8).hex(),
                        "name": body.get("name", "").strip(),
                        "provider": body.get("provider", "custom"),
                        "key": new_key,
                        "base_url": (body.get("base_url") or "").strip()}
                if not item["name"] or not item["key"]:
                    return self._json(400, {"error": "名称和 Key 不能为空"})
                db["keys"] = [i for i in db["keys"] if i["id"] != item["id"]]
                db["keys"].append(item)
                save_db(db, DB_PASSWORD)
            return self._json(200, {"ok": True})
        if self.path == "/api/keys/delete":
            with LOCK:
                db = load_db()
                db["keys"] = [i for i in db["keys"] if i["id"] != body.get("id")]
                save_db(db, DB_PASSWORD)
            return self._json(200, {"ok": True})
        if self.path == "/api/check":
            with LOCK:
                db = load_db()
            item = next((i for i in db["keys"] if i["id"] == body.get("id")), None)
            if not item:
                return self._json(404, {"error": "not found"})
            return self._json(200, query_key(item))
        if self.path == "/api/reveal":
            with LOCK:
                db = load_db()
            item = next((i for i in db["keys"] if i["id"] == body.get("id")), None)
            return self._json(200, {"key": item["key"] if item else ""})
        self._json(404, {"error": "not found"})

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    print(f"KeyVault 运行中 → http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
