# =============================================================
#  app/api/routes/esp32_simulator.py
#  STEMbotix ESP32 Virtual Lab — Production Backend Router
#  Handles: /api/sim_ai · /api/sim
#
#  Response contract (frontend expects):
#  { "project": {
#      "title": str, "description": str,
#      "difficulty": "Beginner|Intermediate|Advanced",
#      "hardware_suggestion": str,
#      "steps": [str, ...],
#      "components": [{"type": str, "x": int, "y": int}, ...],
#      "connections": [
#        {"from": {"type": str, "pin": str, "index": int},
#         "to":   {"type": str, "pin": str, "index": int}}, ...
#      ],
#      "code": str
#  }}
# =============================================================

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("stembotix.simulator")

# ── Router ───────────────────────────────────────────────────
router = APIRouter(tags=["esp32-simulator"])

# ── Config ───────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
SITE_URL           = os.getenv("SITE_URL", "https://stembotix-ai.vercel.app").strip()
SIM_MODEL          = os.getenv("SIM_MODEL", os.getenv("DEFAULT_MODEL", "openai/gpt-4o-mini"))


# =============================================================
#  VALID COMPONENT CATALOGUE (must match frontend COMPONENT_DEFS)
# =============================================================

COMPONENT_CATALOGUE = {
    "esp32": {
        "desc": "ESP32 microcontroller (always include exactly 1)",
        "pins": ["3V3","EN","VP","VN","34","35","32","33","25","26","27","14","12",
                 "GND1","23","22","TX","RX","GND2","21","19","18","5","17","16","4"],
    },
    "led_red":      {"desc": "Red LED", "pins": ["anode", "cathode"]},
    "led_green":    {"desc": "Green LED", "pins": ["anode", "cathode"]},
    "led_blue":     {"desc": "Blue LED", "pins": ["anode", "cathode"]},
    "resistor":     {"desc": "330Ω resistor (use with LEDs)", "pins": ["p1", "p2"]},
    "button":       {"desc": "Push button", "pins": ["p1", "p2"]},
    "buzzer":       {"desc": "Passive buzzer", "pins": ["pos", "neg"]},
    "servo":        {"desc": "Servo motor (SG90)", "pins": ["sig", "vcc", "gnd"]},
    "potentiometer":{"desc": "Rotary potentiometer", "pins": ["vcc", "out", "gnd"]},
    "ldr":          {"desc": "Light-dependent resistor", "pins": ["p1", "p2"]},
    "ultrasonic":   {"desc": "HC-SR04 ultrasonic distance sensor", "pins": ["vcc","trig","echo","gnd"]},
    "dht11":        {"desc": "DHT11 temperature & humidity sensor", "pins": ["vcc","data","gnd"]},
    "motor":        {"desc": "DC motor", "pins": ["pos", "neg"]},
}

VALID_TYPES = set(COMPONENT_CATALOGUE.keys())

# Suggested canvas layout positions by component type
LAYOUT_GRID = {
    "esp32":        {"x": 300, "y": 100},
    "led_red":      {"x": 80,  "y": 80},
    "led_green":    {"x": 80,  "y": 160},
    "led_blue":     {"x": 80,  "y": 240},
    "resistor":     {"x": 160, "y": 100},
    "button":       {"x": 80,  "y": 320},
    "buzzer":       {"x": 80,  "y": 400},
    "servo":        {"x": 540, "y": 150},
    "potentiometer":{"x": 80,  "y": 480},
    "ldr":          {"x": 80,  "y": 480},
    "ultrasonic":   {"x": 540, "y": 250},
    "dht11":        {"x": 540, "y": 350},
    "motor":        {"x": 540, "y": 450},
}


# =============================================================
#  OPENROUTER
# =============================================================

def _or_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": SITE_URL,
        "X-Title": "STEMbotix Simulator",
    }


async def _chat_json(
    messages: list,
    max_tokens: int = 2500,
    temperature: float = 0.1,
) -> tuple[Optional[dict], Optional[str]]:
    """Call OpenRouter and return (parsed_dict, error)."""
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
                    "model": SIM_MODEL,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
        return _parse_json_response(raw), None

    except httpx.HTTPStatusError as e:
        return None, f"OpenRouter {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        return None, str(e)


def _parse_json_response(text: str) -> Optional[dict]:
    """Extract JSON from model response, strip markdown fences."""
    # Try direct parse first
    try:
        return json.loads(text.strip())
    except Exception:
        pass

    # Strip ```json ... ``` fences
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass

    # Last attempt: find first { ... } block
    m2 = re.search(r"\{[\s\S]+\}", text)
    if m2:
        try:
            return json.loads(m2.group(0))
        except Exception:
            pass

    return None


# =============================================================
#  SYSTEM PROMPT
# =============================================================

_COMPONENT_CATALOGUE_STR = "\n".join(
    f'  "{k}": pins={v["pins"]}  — {v["desc"]}'
    for k, v in COMPONENT_CATALOGUE.items()
)

SYSTEM_PROMPT = f"""You are an expert ESP32 hardware design AI for STEMbotix Virtual Lab.

Your task: given a project prompt, return a SINGLE valid JSON object (no markdown, no prose).

JSON schema:
{{
  "title":              "Short project title",
  "description":        "1-2 sentence project overview",
  "difficulty":         "Beginner" | "Intermediate" | "Advanced",
  "hardware_suggestion":"Real component names for physical build",
  "steps":              ["Step 1...", "Step 2...", "Step 3..."],
  "components": [
    {{"type": "<component_type>", "x": <int>, "y": <int>}},
    ...
  ],
  "connections": [
    {{
      "from": {{"type": "<comp_type>", "pin": "<pin_id>", "index": <0-based int>}},
      "to":   {{"type": "<comp_type>", "pin": "<pin_id>", "index": <0-based int>}}
    }},
    ...
  ],
  "code": "<complete ESP32 Arduino .ino code as single string with \\n line breaks>"
}}

VALID component types and their pin IDs:
{_COMPONENT_CATALOGUE_STR}

RULES:
1. Always include exactly one "esp32" component.
2. Index = 0-based order of that type in the components array (first led_red is index 0, second is index 1).
3. LEDs MUST connect: anode → resistor.p1, resistor.p2 → esp32 GPIO, cathode → esp32.GND1.
4. All sensor VCC → esp32.3V3, all sensor GND → esp32.GND1 or esp32.GND2.
5. Code must be a complete, compilable Arduino sketch. Use \\n for newlines inside the JSON string.
6. Keep x/y positions sensible: esp32 at ~(300,100), components spread around it.
7. Return ONLY the JSON object. No markdown, no explanation outside the JSON.
"""


# =============================================================
#  VALIDATION & REPAIR
# =============================================================

def _validate_and_repair(project: dict, prompt: str) -> dict:
    """Sanitize the AI response to match frontend expectations."""

    # Ensure required keys exist
    project.setdefault("title", prompt[:60])
    project.setdefault("description", "AI-generated ESP32 project.")
    project.setdefault("difficulty", "Beginner")
    project.setdefault("hardware_suggestion", "ESP32 dev board + components")
    project.setdefault("steps", ["Wire components as shown", "Upload code", "Test your project"])
    project.setdefault("components", [])
    project.setdefault("connections", [])
    project.setdefault("code", "")

    # Validate component types — drop unknowns
    valid_components = []
    for comp in project["components"]:
        if comp.get("type") in VALID_TYPES:
            # Apply default canvas position if missing
            default_pos = LAYOUT_GRID.get(comp["type"], {"x": 100, "y": 100})
            comp.setdefault("x", default_pos["x"])
            comp.setdefault("y", default_pos["y"])
            # Ensure coordinates are integers
            comp["x"] = int(comp.get("x") or default_pos["x"])
            comp["y"] = int(comp.get("y") or default_pos["y"])
            valid_components.append(comp)
        else:
            logger.warning(f"Simulator: dropping unknown component type '{comp.get('type')}'")

    # Ensure esp32 is present
    if not any(c["type"] == "esp32" for c in valid_components):
        valid_components.insert(0, {"type": "esp32", "x": 300, "y": 100})

    project["components"] = valid_components

    # Validate connections — drop those referencing missing types
    present_types = {c["type"] for c in valid_components}
    valid_conns = []
    for conn in project.get("connections", []):
        f = conn.get("from", {})
        t = conn.get("to", {})
        if (f.get("type") in present_types and t.get("type") in present_types
                and f.get("pin") and t.get("pin")):
            # Validate pin IDs
            f_pins = COMPONENT_CATALOGUE.get(f["type"], {}).get("pins", [])
            t_pins = COMPONENT_CATALOGUE.get(t["type"], {}).get("pins", [])
            if f["pin"] in f_pins and t["pin"] in t_pins:
                conn["from"]["index"] = int(f.get("index") or 0)
                conn["to"]["index"]   = int(t.get("index") or 0)
                valid_conns.append(conn)
            else:
                logger.warning(f"Simulator: dropping connection with invalid pin: {f} → {t}")
    project["connections"] = valid_conns

    return project


# =============================================================
#  ROUTES
# =============================================================

@router.post("/api/sim_ai")
async def sim_ai(request: Request):
    data   = await request.json()
    prompt = (data.get("prompt") or "").strip()
    mode   = (data.get("mode") or "agent_build").strip()

    if not prompt:
        return JSONResponse({"error": "prompt required"}, status_code=400)

    # ── mode: agent_build — full structured project ───────────
    if mode == "agent_build":
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"Build this project: {prompt}"},
        ]

        project_data, err = await _chat_json(messages)

        if err:
            return JSONResponse({"error": err}, status_code=500)

        if not project_data:
            return JSONResponse(
                {"error": "AI returned invalid JSON. Try a simpler prompt."},
                status_code=502,
            )

        project = _validate_and_repair(project_data, prompt)

        logger.info(
            f"Simulator agent_build: '{prompt[:60]}' → "
            f"{len(project['components'])} components, "
            f"{len(project['connections'])} connections"
        )

        return JSONResponse({"project": project})

    # ── mode: chat — freeform Q&A about the simulator ─────────
    elif mode == "chat":
        text = prompt
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an ESP32 hardware expert and electronics tutor. "
                    "Answer student questions clearly and concisely."
                ),
            },
            {"role": "user", "content": text},
        ]
        from app.core.context import openrouter_key_var
        api_key = openrouter_key_var.get() or os.getenv("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            return JSONResponse({"error": "API key not configured"}, status_code=500)
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=_or_headers(api_key),
                    json={"model": SIM_MODEL, "messages": messages,
                          "temperature": 0.3, "max_tokens": 800},
                )
            r.raise_for_status()
            reply = r.json()["choices"][0]["message"]["content"]
            return JSONResponse({"reply": reply})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    else:
        return JSONResponse({"error": f"Unknown mode: {mode}"}, status_code=400)


# Alias — frontend also calls /api/sim in some places
@router.post("/api/sim")
async def sim_alias(request: Request):
    return await sim_ai(request)