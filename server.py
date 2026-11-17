#!/usr/bin/env python3
"""Bonsai Chat local server (Python standard library only).

Serves the chat page and forwards requests:
  /v1/*, /health        -> local llama-server (Bonsai) on LLAMA_PORT
  /p/<provider>/chat    -> that provider's chat API (OpenAI-style request in, OpenAI-style stream out)
  /p/<provider>/models  -> that provider's model list
  /api/settings         -> read / save keys, server address and added models per provider

Listens on 127.0.0.1 only. API keys are kept in config.json (mode 600) and are
never sent back to the browser.
"""
import http.client
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
CONF = HERE / "config.json"
PORT = int(os.environ.get("PORT", "8090"))
LLAMA_PORT = int(os.environ.get("LLAMA_PORT", "8091"))
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
MAX_BODY = 8 * 1024 * 1024

# kind "openai" = speaks the OpenAI chat-completions format; "anthropic" is translated here.
PROVIDERS = {
    "openai": {
        "name": "OpenAI", "kind": "openai", "base": "https://api.openai.com/v1", "needs_key": True,
        "desc": "Create a key at platform.openai.com → API keys.",
        "key_ph": "sk-…", "model_ph": "Model ID, e.g. gpt-4o-mini",
    },
    "gemini": {
        "name": "Google Gemini", "kind": "openai",
        "base": "https://generativelanguage.googleapis.com/v1beta/openai", "needs_key": True,
        "desc": "Create a key at aistudio.google.com → Get API key.",
        "key_ph": "AIza…", "model_ph": "Model ID, e.g. gemini-2.5-flash",
    },
    "anthropic": {
        "name": "Anthropic", "kind": "anthropic", "base": "https://api.anthropic.com/v1", "needs_key": True,
        "desc": "Create a key at console.anthropic.com → API keys.",
        "key_ph": "sk-ant-…", "model_ph": "Model ID, e.g. claude-sonnet-5-5",
    },
    "nvidia": {
        "name": "NVIDIA", "kind": "openai", "base": "https://integrate.api.nvidia.com/v1", "needs_key": True,
        "desc": "Create a free key at build.nvidia.com (it starts with \"nvapi-\").",
        "key_ph": "nvapi-…", "model_ph": "Model ID, e.g. meta/llama-3.3-70b-instruct",
        "extra": {"max_tokens": 4096},
        # sent when the Think button is off; dropped automatically if a model rejects them
        "think_off": {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "low"},
    },
    "local": {
        "name": "Local LLM", "kind": "openai", "base": "http://localhost:11434/v1", "needs_key": False,
        "base_editable": True,
        "desc": "Any OpenAI-compatible server on your network. Ollama: http://localhost:11434/v1 · "
                "LM Studio: http://localhost:1234/v1. The key is optional.",
        "key_ph": "Optional", "model_ph": "Model name, e.g. gemma4:e2b",
        "think_off": {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "low"},
    },
}


def load_conf():
    try:
        raw = json.loads(CONF.read_text())
    except (OSError, ValueError):
        raw = {}
    provs = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
    if "nvidia_key" in raw or "models" in raw:  # settings saved by the first version
        provs.setdefault("nvidia", {"key": str(raw.get("nvidia_key", "")), "models": list(raw.get("models", []))})
    conf = {}
    for pid, p in PROVIDERS.items():
        c = provs.get(pid) if isinstance(provs.get(pid), dict) else {}
        conf[pid] = {
            "key": str(c.get("key", "")),
            "base_url": str(c.get("base_url") or p["base"]) if p.get("base_editable") else p["base"],
            "models": [m for m in c.get("models", []) if isinstance(m, str)],
        }
    return conf


def save_conf(conf):
    tmp = CONF.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"providers": conf}, f, indent=2)
    os.replace(tmp, CONF)


def connect(base, timeout=600):
    u = urlsplit(base)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port, timeout=timeout), u.path.rstrip("/")


def auth_headers(p, key):
    if p["kind"] == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": "Bearer " + key} if key else {}


class Handler(BaseHTTPRequestHandler):
    server_version = "BonsaiChat"

    def log_message(self, fmt, *args):  # keep the log short; never log bodies or keys
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    # ---- helpers -------------------------------------------------------
    def send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def err(self, code, msg):
        self.send_json(code, {"error": {"message": msg}})

    def trusted(self):
        """Only this page may use the server (blocks other websites and DNS rebinding)."""
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {"http://" + h for h in ALLOWED_HOSTS}

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request too large")
        return self.rfile.read(n) if n else b""

    def start_stream(self, status, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def relay(self, conn, method, path, body, headers, down_msg, translate=None):
        """Forward a request and stream the answer back as it arrives."""
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError:
            conn.close()
            return self.err(503, down_msg)
        self.pump(conn, resp, translate)

    def pump(self, conn, resp, translate=None):
        try:
            self.start_stream(resp.status, resp.getheader("Content-Type", "application/json"))
            if translate and resp.status == 200:
                translate(resp)
            else:
                while True:
                    chunk = resp.read1(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except OSError:
            pass  # the browser pressed Stop; closing upstream ends the generation
        finally:
            conn.close()

    def emit(self, delta):
        self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": delta}]}).encode() + b"\n\n")
        self.wfile.flush()

    def anthropic_stream(self, resp):
        """Turn Anthropic's event stream into OpenAI-style chunks."""
        while True:
            line = resp.readline()
            if not line:
                break
            if not line.startswith(b"data:"):
                continue
            try:
                ev = json.loads(line[5:])
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "content_block_delta":
                d = ev.get("delta", {})
                if d.get("type") == "text_delta":
                    self.emit({"content": d.get("text", "")})
                elif d.get("type") == "thinking_delta":
                    self.emit({"reasoning_content": d.get("thinking", "")})
            elif kind == "error":
                self.emit({"content": "\n\n[Anthropic error: %s]" % ev.get("error", {}).get("message", "unknown")})
            elif kind == "message_stop":
                break
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # ---- upstreams -----------------------------------------------------
    def to_bonsai(self, method, body=None):
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn = http.client.HTTPConnection("127.0.0.1", LLAMA_PORT, timeout=900)
        self.relay(conn, method, self.path, body, headers,
                   "The local Bonsai model is not running. Start it with the Bonsai Chat launcher.")

    def provider(self, pid):
        p = PROVIDERS.get(pid)
        if not p:
            self.err(404, "unknown provider")
            return None
        c = load_conf()[pid]
        if p["needs_key"] and not c["key"]:
            self.err(401, "No %s API key saved yet. Add it in Settings." % p["name"])
            return None
        return p, c

    def down_msg(self, p, c):
        if p.get("base_editable"):
            return "Could not reach your local server at %s. Is it running?" % c["base_url"]
        return "Could not reach %s. Check your internet connection." % p["name"]

    def provider_models(self, pid):
        got = self.provider(pid)
        if not got:
            return
        p, c = got
        conn, prefix = connect(c["base_url"], timeout=30)
        path = prefix + "/models" + ("?limit=1000" if p["kind"] == "anthropic" else "")
        try:
            conn.request("GET", path, headers=auth_headers(p, c["key"]))
            resp = conn.getresponse()
            raw = resp.read()
        except OSError:
            return self.err(503, self.down_msg(p, c))
        finally:
            conn.close()
        try:
            data = json.loads(raw)
        except ValueError:
            return self.err(502, "%s sent an unexpected reply (HTTP %s)." % (p["name"], resp.status))
        if resp.status != 200:
            if isinstance(data, list) and data:
                data = data[0]
            return self.send_json(resp.status, data if isinstance(data, dict) else {"error": {"message": str(data)[:300]}})
        items = data.get("data") or data.get("models") or []
        ids = sorted({str(m.get("id") or m.get("name") or "").removeprefix("models/") for m in items if isinstance(m, dict)} - {""})
        self.send_json(200, {"data": [{"id": i} for i in ids]})

    def provider_chat(self, pid, body):
        got = self.provider(pid)
        if not got:
            return
        p, c = got
        try:
            req = json.loads(body)
            model, messages = str(req["model"]), list(req["messages"])
        except (ValueError, KeyError, TypeError):
            return self.err(400, "bad request")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", **auth_headers(p, c["key"])}
        conn, prefix = connect(c["base_url"])
        if p["kind"] == "anthropic":
            system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
            out = {"model": model, "max_tokens": 4096, "stream": True,
                   "messages": [m for m in messages if m.get("role") != "system"]}
            if system:
                out["system"] = system
            return self.relay(conn, "POST", prefix + "/messages", json.dumps(out).encode(), headers,
                              self.down_msg(p, c), translate=self.anthropic_stream)
        conn.close()
        extras = p.get("think_off", {}) if req.get("think") is False else {}
        for use in ([extras, {}] if extras else [{}]):
            out = {"model": model, "messages": messages, "stream": True, **p.get("extra", {}), **use}
            conn, prefix = connect(c["base_url"])
            try:
                conn.request("POST", prefix + "/chat/completions", body=json.dumps(out).encode(), headers=headers)
                resp = conn.getresponse()
            except OSError:
                conn.close()
                return self.err(503, self.down_msg(p, c))
            if use and resp.status in (400, 422):   # this model does not accept the thinking options
                resp.read()
                conn.close()
                continue
            return self.pump(conn, resp)

    # ---- settings ------------------------------------------------------
    def settings(self):
        conf = load_conf()
        out = []
        for pid, p in PROVIDERS.items():
            c = conf[pid]
            key = c["key"]
            out.append({
                "id": pid, "name": p["name"], "needs_key": p["needs_key"], "desc": p["desc"],
                "key_ph": p["key_ph"], "model_ph": p["model_ph"],
                "base_editable": bool(p.get("base_editable")), "base_url": c["base_url"],
                "think_toggle": bool(p.get("think_off")),
                "has_key": bool(key), "key_hint": key[-4:] if len(key) > 8 else "", "models": c["models"],
            })
        self.send_json(200, {"providers": out})

    def save_settings(self, body):
        try:
            new = json.loads(body or b"{}")
        except ValueError:
            return self.err(400, "bad JSON")
        pid = new.get("provider")
        if pid not in PROVIDERS:
            return self.err(400, "unknown provider")
        conf = load_conf()
        c = conf[pid]
        if isinstance(new.get("key"), str):
            c["key"] = new["key"].strip()
        if isinstance(new.get("base_url"), str) and PROVIDERS[pid].get("base_editable"):
            url = new["base_url"].strip().rstrip("/")
            u = urlsplit(url)
            if u.scheme not in ("http", "https") or not u.hostname:
                return self.err(400, "The server address must start with http:// or https://")
            c["base_url"] = url
        if isinstance(new.get("models"), list):
            seen = []
            for m in new["models"]:
                if isinstance(m, str) and 0 < len(m) <= 200 and m not in seen:
                    seen.append(m)
            c["models"] = seen[:100]
        save_conf(conf)
        self.settings()

    # ---- routes --------------------------------------------------------
    def do_GET(self):
        if not self.trusted():
            return self.err(403, "forbidden")
        path = self.path.split("?")[0]
        parts = path.strip("/").split("/")
        if path == "/api/settings":
            return self.settings()
        if path == "/health" or path.startswith("/v1/"):
            return self.to_bonsai("GET")
        if len(parts) == 3 and parts[0] == "p" and parts[2] == "models":
            return self.provider_models(parts[1])
        if path in ("/", "/index.html"):
            data = (WEB / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return self.wfile.write(data)
        self.err(404, "not found")

    def do_POST(self):
        if not self.trusted():
            return self.err(403, "forbidden")
        path = self.path.split("?")[0]
        parts = path.strip("/").split("/")
        try:
            body = self.body()
        except ValueError as e:
            return self.err(413, str(e))
        if path == "/api/settings":
            return self.save_settings(body)
        if path == "/v1/chat/completions":
            return self.to_bonsai("POST", body)
        if len(parts) == 3 and parts[0] == "p" and parts[2] == "chat":
            return self.provider_chat(parts[1], body)
        self.err(404, "not found")


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Bonsai Chat on http://127.0.0.1:{PORT} (local model on port {LLAMA_PORT})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
