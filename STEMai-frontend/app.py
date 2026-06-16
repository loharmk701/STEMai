# =========================
# VERCEL FRONTEND app.py
# Only serves HTML templates.
# ALL /api/* calls are proxied to VPS via vercel.json (in prod)
# Locally, we proxy /api/* to the FastAPI backend via Flask
# =========================
import os
import requests as http_requests
from flask import Flask, render_template, send_from_directory, request, Response
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

# Backend URL for local API proxying
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8123")


# ── API Proxy (local dev) ────────────────────────────────────
# Forwards all /api/* requests to the FastAPI backend
@app.route("/api/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def proxy_api(path):
    target_url = f"{BACKEND_URL}/api/{path}"
    
    # Forward headers (except Host)
    headers = {k: v for k, v in request.headers if k.lower() != "host"}
    
    # Debug: log forwarded headers
    auth_hdr = headers.get('Authorization', 'NONE')
    print(f"[PROXY] {request.method} /api/{path} -> {target_url}")
    print(f"[PROXY] Auth header: {auth_hdr[:50] if auth_hdr != 'NONE' else 'NONE'}")
    
    try:
        resp = http_requests.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            params=request.args,
            timeout=120,
            allow_redirects=False,
        )
        
        # Build Flask response from backend response
        excluded_headers = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        response_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded_headers}
        
        return Response(resp.content, resp.status_code, response_headers)
    except http_requests.exceptions.ConnectionError:
        return Response(
            '{"error": "Backend unreachable. Is the FastAPI server running on port 8123?"}',
            502,
            {"Content-Type": "application/json"},
        )


# Also proxy /auth/* requests (auth routes don't have /api prefix)
@app.route("/auth/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def proxy_auth(path):
    target_url = f"{BACKEND_URL}/auth/{path}"
    headers = {k: v for k, v in request.headers if k.lower() != "host"}
    
    try:
        resp = http_requests.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            params=request.args,
            timeout=120,
            allow_redirects=False,
        )
        excluded_headers = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        response_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded_headers}
        return Response(resp.content, resp.status_code, response_headers)
    except http_requests.exceptions.ConnectionError:
        return Response(
            '{"error": "Backend unreachable"}',
            502,
            {"Content-Type": "application/json"},
        )

BLOCKZIE_DIR = os.path.join(os.path.dirname(__file__), "static", "blockzie")

# ── Pages ────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/ide")
def ide():
    return render_template("ide.html")

@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")

@app.route("/iot")
def iot_dashboard():
    return render_template("dashboard.html")

@app.route("/simulator")
def simulator_page():
    return render_template("simulator.html")

@app.route("/esp32-simulator")
def esp32_simulator_page():
    return render_template("Esp32 simulator.html")

@app.route("/blockzie")
def blockzie_app():
    return send_from_directory(BLOCKZIE_DIR, "index.html")

@app.route("/blockzie/<path:filename>")
def blockzie_static(filename):
    return send_from_directory(BLOCKZIE_DIR, filename)

@app.route("/blockzie_bridge")
def blockzie_bridge():
    return render_template("blockzie_bridge.html")

@app.route("/programming-lab")
def programming_lab():
    return render_template("programming_lab.html")

# ── Run ──────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=3000)