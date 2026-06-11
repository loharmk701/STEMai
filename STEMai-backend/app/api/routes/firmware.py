# =============================================================
#  app/api/routes/firmware.py
#  STEMbotix Firmware Studio — Production Backend Router
#
#  Endpoints:
#    POST /firmware/ai_generate    — Agentic AI: generate Arduino code
#    POST /firmware/device_ping    — Ping device by IP
#    POST /firmware/push_code      — Compile + OTA push via arduino-cli
#    POST /firmware/ota_push       — Push editor code via HTTP OTA to device
#    POST /firmware/register_device— Register/store a device
#  Depends on:
#    - arduino-cli  (compile + upload)   — sudo apt install arduino-cli
#    - curl/httpx   (OTA HTTP push)
# =============================================================

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("stembotix.firmware")

# ── Router ───────────────────────────────────────────────────
router = APIRouter(prefix="/firmware", tags=["firmware-studio"])

# ── Config ───────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
SITE_URL           = os.getenv("SITE_URL", "https://stembotix-ai.vercel.app").strip()
FIRMWARE_MODEL     = os.getenv("FIRMWARE_MODEL", os.getenv("DEFAULT_MODEL", "openai/gpt-4o-mini"))
COMPILE_TIMEOUT    = int(os.getenv("FIRMWARE_COMPILE_TIMEOUT", "120"))
OTA_TIMEOUT        = int(os.getenv("FIRMWARE_OTA_TIMEOUT", "60"))
PING_TIMEOUT       = int(os.getenv("FIRMWARE_PING_TIMEOUT", "5"))

# arduino-cli path (override if installed elsewhere)
ARDUINO_CLI        = os.getenv("ARDUINO_CLI_PATH", "arduino-cli")

# Default FQBN used when board family isn't specified
DEFAULT_FQBN       = os.getenv("FIRMWARE_DEFAULT_FQBN", "esp32:esp32:esp32")

# In-memory device registry  {device_id: {"ip": ..., "fqbn": ..., ...}}
_DEVICE_REGISTRY: dict[str, dict] = {}


# =============================================================
#  OPENROUTER
# =============================================================

def _or_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": SITE_URL,
        "X-Title": "STEMbotix Firmware Studio",
    }


async def _chat(
    messages: list,
    max_tokens: int = 3000,
    temperature: float = 0.15,
    model: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Returns (content, error). error is None on success."""
    from app.core.context import openrouter_key_var
    api_key = openrouter_key_var.get() or os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None, "OPENROUTER_API_KEY not configured"
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=_or_headers(api_key),
                json={
                    "model": model or FIRMWARE_MODEL,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"], None
    except httpx.HTTPStatusError as e:
        return None, f"OpenRouter {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        return None, str(e)


# =============================================================
#  CODE EXTRACTION
# =============================================================

def _extract_ino(text: str) -> str:
    """Extract the first ```cpp / ```arduino / ```ino code block, or return full text."""
    # Try fenced block
    m = re.search(r"```(?:cpp|c\+\+|arduino|ino)?\s*\n([\s\S]+?)```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Fall back: strip triple-backtick if present
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        return "\n".join(lines[1:end]).strip()
    return s


# =============================================================
#  SYSTEM PROMPT — AI Firmware Generator
# =============================================================

def _build_system_prompt(
    board: str,
    fqbn: str,
    board_family: str,
    has_wifi: bool,
    libraries: list[str],
    led_pin: str,
    dht_pin: str,
    adc_pin: str,
    device_id: str,
    api_base: str = "",
) -> str:
    libs_str = ", ".join(libraries) if libraries else "none selected"
    wifi_note = (
        "This board has WiFi. Use WiFi.h / HTTPClient.h when networking is needed."
        if has_wifi else
        "This board does NOT have WiFi. Do not use WiFi libraries."
    )

    return f"""You are an expert Arduino/ESP32 firmware engineer and AI coding copilot for STEMbotix.

Project context:
- Board           : {board or "ESP32"}
- FQBN            : {fqbn or DEFAULT_FQBN}
- Board family    : {board_family or "esp32"}
- Device ID       : {device_id or "my_device"}
- LED pin         : {led_pin or "2"}
- DHT pin         : {dht_pin or "4"}
- ADC pin         : {adc_pin or "34"}
- WiFi capable    : {wifi_note}
- Selected libs   : {libs_str}
- API base URL    : {api_base or "(not set)"}

RULES:
1. When generating or rewriting firmware, return ONE complete .ino file inside a single ```cpp code block.
2. Always include setup() and loop().
3. Use the correct pin numbers from the context above.
4. When explaining or debugging, be concise and actionable.
5. Never hallucinate library functions — only use functions that exist in the specified libraries.
6. If the user asks for IoT telemetry, use the api_base URL to POST sensor data.
"""


# =============================================================
#  ROUTE — AI GENERATE (Agentic: Generate → Validate → Return)
# =============================================================

@router.post("/ai_generate")
async def firmware_ai_generate(request: Request):
    data = await request.json()

    prompt       = (data.get("prompt") or "").strip()
    device_id    = data.get("device_id", "my_device")
    board        = data.get("board", "ESP32")
    fqbn         = data.get("fqbn", DEFAULT_FQBN)
    board_family = data.get("board_family", "esp32")
    has_wifi     = bool(data.get("has_wifi", True))
    libraries    = data.get("libraries", [])
    led_pin      = str(data.get("led_pin", "2"))
    dht_pin      = str(data.get("dht_pin", "4"))
    adc_pin      = str(data.get("adc_pin", "34"))
    current_code = data.get("current_code", "")
    history      = data.get("history", [])
    api_base     = data.get("api_base", SITE_URL)

    if not prompt:
        return JSONResponse({"error": "prompt required"}, status_code=400)

    sys_prompt = _build_system_prompt(
        board, fqbn, board_family, has_wifi, libraries,
        led_pin, dht_pin, adc_pin, device_id, api_base,
    )

    # Build full message list (include history for context)
    messages: list[dict] = [{"role": "system", "content": sys_prompt}]

    # Add conversation history (last 6 turns max)
    for h in history[-6:]:
        role = h.get("role", "user")
        content = h.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    # Include current editor code as context
    user_content = prompt
    if current_code:
        user_content = (
            f"Current firmware:\n```cpp\n{current_code}\n```\n\n"
            f"Request: {prompt}"
        )
    messages.append({"role": "user", "content": user_content})

    # ── Step 1: Generate ────────────────────────────────────
    raw_response, err = await _chat(messages)
    if err:
        return JSONResponse({"error": err}, status_code=500)

    ino = _extract_ino(raw_response)

    # ── Step 2: Validate (syntax check via arduino-cli dry-run if available) ──
    syntax_error = None
    if ino and shutil.which(ARDUINO_CLI):
        syntax_error = await _dry_compile(ino, fqbn)
        if syntax_error:
            # Auto-fix one pass
            fix_messages = messages + [
                {"role": "assistant", "content": raw_response},
                {
                    "role": "user",
                    "content": (
                        f"The code has this compilation error:\n\n{syntax_error}\n\n"
                        "Please fix it and return the corrected complete code."
                    ),
                },
            ]
            fixed_raw, fix_err = await _chat(fix_messages)
            if not fix_err:
                ino = _extract_ino(fixed_raw)
                raw_response = fixed_raw
                syntax_error = None  # optimistic

    return JSONResponse({
        "ino":           ino,
        "text":          raw_response,
        "syntax_warned": syntax_error,
    })


# =============================================================
#  ROUTE — DEVICE PING
# =============================================================

@router.post("/device_ping")
async def device_ping(request: Request):
    data      = await request.json()
    ip        = (data.get("ip") or "").strip()
    device_id = data.get("device_id", "")

    if not ip:
        return JSONResponse({"error": "ip required"}, status_code=400)

    # Try HTTP ping to device's /ping or /health endpoint
    for path in ["/ping", "/health", "/"]:
        try:
            async with httpx.AsyncClient(timeout=PING_TIMEOUT) as client:
                r = await client.get(f"http://{ip}{path}")
            if r.status_code < 500:
                logger.info(f"Device ping OK: {ip}{path} → {r.status_code}")
                return JSONResponse({
                    "ok":     True,
                    "online": True,
                    "ip":     ip,
                    "path":   path,
                    "status": r.status_code,
                })
        except Exception:
            continue

    # Fallback: OS-level ICMP ping
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(PING_TIMEOUT), ip],
            capture_output=True, text=True, timeout=PING_TIMEOUT + 2,
        )
        online = result.returncode == 0
        return JSONResponse({"ok": online, "online": online, "ip": ip, "method": "icmp"})
    except Exception as e:
        return JSONResponse({"ok": False, "online": False, "ip": ip, "error": str(e)})


# =============================================================
#  ROUTE — PUSH CODE (Compile + OTA via arduino-cli)
# =============================================================

@router.post("/push_code")
async def push_code(request: Request):
    data      = await request.json()
    ino_code  = (data.get("ino") or "").strip()
    device_id = data.get("device_id", "")
    fqbn      = data.get("fqbn", DEFAULT_FQBN)

    if not ino_code:
        return JSONResponse({"error": "ino code required"}, status_code=400)

    # Resolve device IP from registry
    device = _DEVICE_REGISTRY.get(device_id, {})
    ip     = device.get("ip", data.get("ip", ""))
    fqbn   = device.get("fqbn", fqbn)

    if not shutil.which(ARDUINO_CLI):
        return JSONResponse({
            "error": "arduino-cli not installed on server. Install: https://arduino.github.io/arduino-cli/",
        }, status_code=501)

    tmp = tempfile.mkdtemp()
    try:
        sketch_dir = os.path.join(tmp, "sketch")
        os.makedirs(sketch_dir)
        ino_path = os.path.join(sketch_dir, "sketch.ino")
        with open(ino_path, "w") as f:
            f.write(ino_code)

        # ── Compile ──────────────────────────────────────────
        logger.info(f"Compiling {device_id} with fqbn={fqbn}")
        comp = subprocess.run(
            [ARDUINO_CLI, "compile", "--fqbn", fqbn, sketch_dir],
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT,
        )
        if comp.returncode != 0:
            return JSONResponse({
                "ok":    False,
                "error": comp.stderr or comp.stdout,
                "stage": "compile",
            }, status_code=422)

        # ── OTA Upload (if IP known) ──────────────────────────
        if ip:
            token = device.get("token", data.get("token", ""))
            bin_path = _find_bin(sketch_dir)
            if bin_path:
                ota_result = await _ota_upload(ip, bin_path, token)
                if not ota_result["ok"]:
                    return JSONResponse({
                        "ok": False, "error": ota_result["error"], "stage": "ota"
                    }, status_code=502)
                return JSONResponse({"ok": True, "stage": "ota", "ip": ip})

            return JSONResponse({"ok": False, "error": "Compiled but .bin not found", "stage": "ota"})

        # No IP — just confirm compilation
        return JSONResponse({"ok": True, "stage": "compile_only",
                              "message": "Compiled successfully. No device IP to push to."})

    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "error": f"Compile timed out ({COMPILE_TIMEOUT}s)"})
    except Exception as e:
        logger.exception("push_code error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =============================================================
#  ROUTE — OTA PUSH (send editor .ino via HTTP OTA)
# =============================================================

@router.post("/ota_push")
async def ota_push(request: Request):
    data      = await request.json()
    ip        = (data.get("ip") or "").strip()
    ino_code  = (data.get("ino") or "").strip()
    token     = data.get("token", "")
    device_id = data.get("device_id", "")
    fqbn      = data.get("fqbn", DEFAULT_FQBN)

    if not ip:
        return JSONResponse({"error": "ip required"}, status_code=400)
    if not ino_code:
        return JSONResponse({"error": "ino code required"}, status_code=400)

    if not shutil.which(ARDUINO_CLI):
        return JSONResponse({
            "error": "arduino-cli not installed. OTA push requires server-side compilation.",
        }, status_code=501)

    tmp = tempfile.mkdtemp()
    try:
        sketch_dir = os.path.join(tmp, "sketch")
        os.makedirs(sketch_dir)
        with open(os.path.join(sketch_dir, "sketch.ino"), "w") as f:
            f.write(ino_code)

        # Compile
        comp = subprocess.run(
            [ARDUINO_CLI, "compile", "--fqbn", fqbn, sketch_dir],
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT,
        )
        if comp.returncode != 0:
            return JSONResponse({"ok": False, "error": comp.stderr or comp.stdout,
                                  "stage": "compile"}, status_code=422)

        # Push .bin via HTTP OTA
        bin_path = _find_bin(sketch_dir)
        if not bin_path:
            return JSONResponse({"ok": False, "error": "Compiled .bin not found", "stage": "ota"})

        ota_result = await _ota_upload(ip, bin_path, token)
        if not ota_result["ok"]:
            return JSONResponse({"ok": False, "error": ota_result["error"], "stage": "ota"},
                                  status_code=502)

        return JSONResponse({"ok": True, "ip": ip, "message": "OTA upload accepted. Device rebooting."})

    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "error": f"Compile timed out ({COMPILE_TIMEOUT}s)"})
    except Exception as e:
        logger.exception("ota_push error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =============================================================
#  ROUTE — REGISTER DEVICE
# =============================================================

@router.post("/register_device")
async def register_device(request: Request):
    data      = await request.json()
    device_id = (data.get("device_id") or "").strip()
    ip        = (data.get("ip") or "").strip()
    fqbn      = data.get("fqbn", DEFAULT_FQBN)
    token     = data.get("token", "")
    label     = data.get("label", device_id)

    if not device_id:
        return JSONResponse({"error": "device_id required"}, status_code=400)

    _DEVICE_REGISTRY[device_id] = {
        "device_id":   device_id,
        "ip":          ip,
        "fqbn":        fqbn,
        "token":       token,
        "label":       label,
        "registered":  time.time(),
    }

    logger.info(f"Device registered: {device_id} @ {ip}")
    return JSONResponse({"ok": True, "device_id": device_id, "ip": ip})


@router.get("/devices")
async def list_devices():
    return JSONResponse({"devices": list(_DEVICE_REGISTRY.values())})


# =============================================================
#  HELPERS
# =============================================================

def _find_bin(directory: str) -> Optional[str]:
    """Recursively find the first .bin file produced by arduino-cli."""
    for root, _, files in os.walk(directory):
        for f in files:
            if f.endswith(".bin"):
                return os.path.join(root, f)
    return None


async def _ota_upload(ip: str, bin_path: str, token: str = "") -> dict:
    """Upload .bin to ESP32 OTA endpoint at http://{ip}/update"""
    url = f"http://{ip}/update"
    params = {"token": token} if token else {}
    try:
        with open(bin_path, "rb") as f:
            bin_data = f.read()

        async with httpx.AsyncClient(timeout=OTA_TIMEOUT) as client:
            r = await client.post(
                url,
                params=params,
                content=bin_data,
                headers={"Content-Type": "application/octet-stream"},
            )
        # ESP32 OTA returns 200 or 0 on success
        if r.status_code in (200, 0):
            return {"ok": True}
        return {"ok": False, "error": f"OTA HTTP {r.status_code}: {r.text[:200]}"}

    except httpx.ConnectError:
        return {"ok": False, "error": f"Cannot connect to {ip}. Is the device online and OTA-enabled?"}
    except httpx.TimeoutException:
        return {"ok": False, "error": f"OTA upload timed out after {OTA_TIMEOUT}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _dry_compile(ino_code: str, fqbn: str) -> Optional[str]:
    """Run arduino-cli --dry-run / verify to catch syntax errors. Returns error string or None."""
    tmp = tempfile.mkdtemp()
    try:
        sketch_dir = os.path.join(tmp, "sketch")
        os.makedirs(sketch_dir)
        with open(os.path.join(sketch_dir, "sketch.ino"), "w") as f:
            f.write(ino_code)

        result = await asyncio.to_thread(
            subprocess.run,
            [ARDUINO_CLI, "compile", "--fqbn", fqbn, sketch_dir],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return result.stderr or result.stdout
        return None
    except Exception:
        return None  # Don't block generation on dry-compile failure
    finally:
        shutil.rmtree(tmp, ignore_errors=True)