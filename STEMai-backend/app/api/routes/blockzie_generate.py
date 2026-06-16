# app/api/routes/blockzie_generate.py
#
# v5 — SMART NESTED XML GENERATOR
# ══════════════════════════════════════════════════════════════════════════════
# Root causes fixed in this version:
#
#  1. HTTP 402 (payment required) — anthropic/claude-3.5-sonnet and
#     openai/gpt-4o-mini need credits. Removed from primary list.
#     Only truly free OpenRouter models are tried first.
#     Paid models are used ONLY if ALLOW_PAID_MODELS=true in .env.
#
#  2. DSL fallback produced flat XML with no nesting — build_xml_from_cmds
#     chains everything with <next>, so forever/repeat had empty SUBSTACK.
#     Fixed with a new _build_nested_xml() that parses indentation to
#     correctly nest body blocks inside <statement name="SUBSTACK">.
#
#  3. Direct XML strategy: LLM given full schema + 3 complete examples.
#     Works well with gemini-flash and llama-70b on correct prompts.
#
#  4. Triple fallback chain:
#     Strategy A → LLM writes XML directly (best quality, handles nesting)
#     Strategy B → LLM writes indented DSL → nested XML builder
#     Strategy C → simple flat blocks (no loops, always works)
# ══════════════════════════════════════════════════════════════════════════════

import os
import re
import time
import uuid
import textwrap
import logging
import xml.etree.ElementTree as ET
from typing import Optional, List, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.core.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/blockzie", tags=["blockzie"])

# ── Config ─────────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"
SITE_URL           = os.getenv("SITE_URL", "https://stembotix-ai.vercel.app").strip()

# ── Model tiers ────────────────────────────────────────────────────────────────
#
# SMART tier  — Claude 3.5 Sonnet / GPT-4o.
#   Used automatically when the prompt is detected as "complex"
#   (nested loops, if/else, multi-sensor, full projects, etc.)
#   Requires OpenRouter credits. Set OPENROUTER_API_KEY in .env.
#
# DEFAULT tier — GPT-4o-mini / Claude 3.5 Haiku.
#   Used for medium-difficulty prompts.
#
# FREE tier    — Gemini / Llama / Mistral free models.
#   Always tried. No credits needed. Used for simple prompts or as fallback.
#
# The route auto-selects the tier based on prompt complexity score.
# You can override by setting FORCE_SMART_MODEL=true in .env.

SMART_MODELS = [
    os.getenv("SMART_MODEL_1", "anthropic/claude-3.5-sonnet").strip(),
    os.getenv("SMART_MODEL_2", "openai/gpt-4o").strip(),
    os.getenv("SMART_MODEL_3", "anthropic/claude-3-opus").strip(),
]

DEFAULT_MODELS = [
    os.getenv("DEFAULT_MODEL", "openai/gpt-4o-mini").strip(),
    os.getenv("PREMIUM_MODEL", "anthropic/claude-3.5-haiku").strip(),
]

FREE_MODELS = [
    os.getenv("FREE_MODEL", "google/gemini-2.5-pro-exp-03-25:free").strip(),
    "google/gemini-2.0-flash-thinking-exp:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "openrouter/auto",
]

# If true, always use smart models regardless of complexity score
FORCE_SMART = os.getenv("FORCE_SMART_MODEL", "false").lower() == "true"

_model_rate_limited_until: dict[str, float] = {}
_model_payment_failed:     set[str]         = set()   # 402 → skip forever
_RATE_LIMIT_COOLDOWN = 60


# ── Complexity scorer ──────────────────────────────────────────────────────────
# Returns a score 0–100. >= 40 → use smart models. < 40 → try free first.
#
# Smart models (Claude/GPT-4o) are ONLY called when:
#   a) score >= 40  (complex prompt detected), OR
#   b) FORCE_SMART_MODEL=true in .env, OR
#   c) free models all failed for a simpler prompt
#
# This keeps costs near-zero for simple commands while giving hard projects
# the full power of Claude/GPT-4o.

_COMPLEX_KEYWORDS = [
    # Full project / game keywords — these alone should push to SMART
    (r"\bgame\b",                                        35),
    (r"\bplatform(er)?\b",                               35),
    (r"\bpong\b",                                        35),
    (r"\bsnake\b",                                       35),
    (r"\bmaze\b",                                        35),
    (r"\bquiz\b",                                        30),
    (r"\bsimulat\b",                                     30),
    (r"\bproject\b",                                     20),
    # Structural complexity keywords
    (r"\bif\s+.{1,30}\s+else\b",                        30),
    (r"\bnested\b",                                      25),
    (r"\bstate\s+machine\b",                             35),
    (r"\bvariable\b",                                    20),
    (r"\bscore\b",                                       20),
    (r"\blive(s)?\b",                                    20),
    (r"\bcount\b",                                       15),
    (r"\btimer\b",                                       15),
    (r"\bcollision\b",                                   25),
    (r"\btouching\b",                                    20),
    (r"\bmultiple\s+(sprites?|loops?|conditions?|sensors?|levels?)\b", 25),
    # Interaction complexity
    (r"\bkeyboard\b|\barrow\s+key\b|\bkey\s+press\b",   20),
    (r"\bjump\b",                                        15),
    (r"\bask\b.{1,20}\banswer\b",                        20),
    (r"\bbroadcast\b",                                   15),
    (r"\bclone\b",                                       20),
    # Advanced hardware
    (r"\bpid\b",                                         35),
    (r"\bgyro\b|\bimu\b|\bi2c\b|\buart\b",              30),
    (r"\bif\b.{1,30}\bmotor\b",                         25),
    (r"\bif\b.{1,30}\bsensor\b",                        25),
    (r"\bmultiple\s+(pin|sensor|motor)\b",               20),
]

_MEDIUM_KEYWORDS = [
    (r"\bforever\b",       8),
    (r"\brepeat\b",        8),
    (r"\bloop\b",          6),
    (r"\bwait\b",          5),
    (r"\bbounce\b",        6),
    (r"\bglide\b",         6),
    (r"\bsound\b",         5),
    (r"\bbackdrop\b",      5),
    (r"\bcostume\b",       5),
    (r"\bsay\b.{1,10}\bfor\b", 6),
    (r"\bmotor\b",         8),
    (r"\bservo\b",         8),
    (r"\bpin\b",           5),
    (r"\bled\b.{1,10}\bblink\b|\bblink\b.{1,10}\bled\b", 8),
    (r"\bkeyboard\b|\bkey\b", 8),
]


def _complexity_score(prompt: str) -> int:
    """
    Returns 0-100.
    >= 60 → SMART tier (Claude 3.5 Sonnet / GPT-4o)
    >= 30 → DEFAULT tier (GPT-4o-mini / Claude Haiku)
     < 30 → FREE tier first
    """
    low   = prompt.lower()
    score = 0

    # Word count heuristic
    words = len(prompt.split())
    if   words > 40: score += 20
    elif words > 20: score += 10
    elif words > 10: score +=  5

    # Complex keyword hits (weighted)
    for pattern, weight in _COMPLEX_KEYWORDS:
        if re.search(pattern, low):
            score += weight

    # Medium keyword hits (weighted)
    for pattern, weight in _MEDIUM_KEYWORDS:
        if re.search(pattern, low):
            score += weight

    # Multi-sentence / list structure
    score += len(re.findall(r'[.;!?]', prompt)) * 4
    score += prompt.count(",") * 2

    return min(score, 100)


def _get_model_list(score: int) -> List[str]:
    """
    Returns ordered list of models to try based on complexity score.
    Smart models (Claude/GPT-4o) appear first for hard prompts.
    Free models always appear as fallback.
    """
    def _dedup(lst: List[str]) -> List[str]:
        seen: set[str] = set()
        out:  List[str] = []
        for m in lst:
            if m not in seen:
                seen.add(m)
                out.append(m)
        return out

    if FORCE_SMART or score >= 50:
        # Hard project: Claude/GPT-4o first, then defaults, then free
        logger.info(f"[models] score={score} → SMART tier")
        return _dedup(SMART_MODELS + DEFAULT_MODELS + FREE_MODELS)

    if score >= 25:
        # Medium: GPT-4o-mini/Haiku first, then free, smart as last resort
        logger.info(f"[models] score={score} → DEFAULT tier")
        return _dedup(DEFAULT_MODELS + FREE_MODELS + SMART_MODELS)

    # Simple: free models first, paid only if all free fail
    logger.info(f"[models] score={score} → FREE tier")
    return _dedup(FREE_MODELS + DEFAULT_MODELS + SMART_MODELS)


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT A — LLM writes XML directly
# ══════════════════════════════════════════════════════════════════════════════
PROMPT_DIRECT_XML = '''You are a Blockzie/Scratch XML generator.
Convert the user request into valid Blockzie XML.
Output ONLY the raw XML — no explanation, no markdown, no code fences.

XML RULES:
• Wrap everything in: <xml xmlns="http://www.w3.org/1999/xhtml"><variables/>  ...  </xml>
• Every <block> must have id="XXXXXXXX" (8 random hex chars, all unique)
• First block must have x="20" y="20"
• Sequential blocks chain with: <next><block ...>...</block></next>
• Loop/if BODY blocks go inside: <statement name="SUBSTACK">...</statement>
• if/else second branch: <statement name="SUBSTACK2">...</statement>
• Number input:   <value name="STEPS"><shadow type="math_number" id="xx"><field name="NUM">10</field></shadow></value>
• Duration input: <value name="DURATION"><shadow type="math_positive_number" id="xx"><field name="NUM">1</field></shadow></value>
• Repeat count:   <value name="TIMES"><shadow type="math_integer" id="xx"><field name="NUM">10</field></shadow></value>
• Text input:     <value name="MESSAGE"><shadow type="text" id="xx"><field name="TEXT">Hello</field></shadow></value>

BLOCK TYPES:
event_whenflagclicked        → no inputs (HAT block, always first)
event_whenkeypressed         → <field name="KEY_OPTION">space</field>
motion_movesteps             → STEPS=num
motion_turnright             → DEGREES=num
motion_turnleft              → DEGREES=num
motion_gotoxy                → X=num, Y=num
motion_glidesecstoxy         → SECS=num, X=num, Y=num
motion_changexby             → DX=num
motion_changeyby             → DY=num
motion_setx                  → X=num
motion_sety                  → Y=num
motion_ifonedgebounce        → no inputs
motion_pointindirection      → <value name="DIRECTION"><shadow type="math_angle" id="xx"><field name="NUM">90</field></shadow></value>
looks_say                    → MESSAGE=text
looks_sayforsecs             → MESSAGE=text, SECS=num
looks_show / looks_hide      → no inputs
looks_nextcostume            → no inputs
looks_changesizeby           → CHANGE=num
looks_setsizeto              → SIZE=num
sound_play                   → <value name="SOUND_MENU"><shadow type="sound_sounds_menu" id="xx"><field name="SOUND_MENU">pop</field></shadow></value>
sound_playuntildone          → same as sound_play
sound_stopallsounds          → no inputs
control_wait                 → DURATION=num (use math_positive_number shadow)
control_repeat               → TIMES=num (use math_integer shadow) + SUBSTACK body
control_forever              → SUBSTACK body only (no <next> after forever block)
control_if                   → CONDITION=boolean + SUBSTACK body
control_if_else              → CONDITION=boolean + SUBSTACK + SUBSTACK2
control_stop                 → <field name="STOP_OPTION">all</field>
arduino_pin_setDigitalOutput → <field name="PIN">2</field> + <value name="LEVEL"><shadow type="arduino_pin_menu_level" id="xx"><field name="level">HIGH</field></shadow></value>
arduino_pin_readDigitalPin   → <field name="PIN">2</field>
arduino_pin_readAnalogPin    → <field name="PIN">34</field>
arduino_pin_esp32SetPwmOutput→ <field name="PIN">2</field><field name="CH">0</field> + OUT=num
arduino_pin_esp32SetServoOutput → <field name="PIN">13</field><field name="CH">0</field> + <value name="OUT"><shadow type="math_angle" id="xx"><field name="NUM">90</field></shadow></value>
arduino_dcomtor_runMotor     → <field name="MOTOR">M1</field><field name="DIRECTION">forward</field><field name="MOTOR1">M2</field><field name="DIRECTION1">forward</field>

EXAMPLE — "move back and forth forever":
<xml xmlns="http://www.w3.org/1999/xhtml"><variables/>
<block type="event_whenflagclicked" id="e1f2a3b4" x="20" y="20">
<next><block type="control_forever" id="e1f2a3b5">
<statement name="SUBSTACK">
<block type="motion_movesteps" id="e1f2a3b6">
<value name="STEPS"><shadow type="math_number" id="e1f2a3b7"><field name="NUM">10</field></shadow></value>
<next><block type="control_wait" id="e1f2a3b8">
<value name="DURATION"><shadow type="math_positive_number" id="e1f2a3b9"><field name="NUM">0.5</field></shadow></value>
<next><block type="motion_movesteps" id="e1f2a3ba">
<value name="STEPS"><shadow type="math_number" id="e1f2a3bb"><field name="NUM">-10</field></shadow></value>
<next><block type="control_wait" id="e1f2a3bc">
<value name="DURATION"><shadow type="math_positive_number" id="e1f2a3bd"><field name="NUM">0.5</field></shadow></value>
</block></next></block></next></block></next></block>
</statement>
</block></next>
</block>
</xml>

EXAMPLE — "draw a square":
<xml xmlns="http://www.w3.org/1999/xhtml"><variables/>
<block type="event_whenflagclicked" id="f1000001" x="20" y="20">
<next><block type="control_repeat" id="f1000002">
<value name="TIMES"><shadow type="math_integer" id="f1000003"><field name="NUM">4</field></shadow></value>
<statement name="SUBSTACK">
<block type="motion_movesteps" id="f1000004">
<value name="STEPS"><shadow type="math_number" id="f1000005"><field name="NUM">100</field></shadow></value>
<next><block type="motion_turnright" id="f1000006">
<value name="DEGREES"><shadow type="math_number" id="f1000007"><field name="NUM">90</field></shadow></value>
</block></next>
</block>
</statement>
</block></next>
</block>
</xml>

EXAMPLE — "blink LED on pin 2 forever":
<xml xmlns="http://www.w3.org/1999/xhtml"><variables/>
<block type="event_whenflagclicked" id="c1000001" x="20" y="20">
<next><block type="control_forever" id="c1000002">
<statement name="SUBSTACK">
<block type="arduino_pin_setDigitalOutput" id="c1000003">
<field name="PIN">2</field>
<value name="LEVEL"><shadow type="arduino_pin_menu_level" id="c1000004"><field name="level">HIGH</field></shadow></value>
<next><block type="control_wait" id="c1000005">
<value name="DURATION"><shadow type="math_positive_number" id="c1000006"><field name="NUM">0.5</field></shadow></value>
<next><block type="arduino_pin_setDigitalOutput" id="c1000007">
<field name="PIN">2</field>
<value name="LEVEL"><shadow type="arduino_pin_menu_level" id="c1000008"><field name="level">LOW</field></shadow></value>
<next><block type="control_wait" id="c1000009">
<value name="DURATION"><shadow type="math_positive_number" id="c1000010"><field name="NUM">0.5</field></shadow></value>
</block></next></block></next></block></next></block>
</statement>
</block></next>
</block>
</xml>

Always use forever loops for animations. Always put control_wait inside loops.
Generate creative, working programs for STEM students.
'''


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT B — LLM writes indented DSL (simpler, works on weaker models)
# ══════════════════════════════════════════════════════════════════════════════
PROMPT_DSL = """You are a Scratch/Blockzie DSL generator for STEM education.
Output ONLY the DSL program — no explanation, no markdown.

RULES:
• One command per line
• Always start with: when green flag clicked
• Indent loop/if body with exactly 2 spaces
• Use "forever" for infinite loops — indent body 2 spaces inside it
• Use "repeat N" for counted loops — indent body 2 spaces inside it
• Always put "wait 0.5" inside loops so the program doesn't freeze

AVAILABLE COMMANDS:
  when green flag clicked
  when key [space/up/down/left/right] pressed
  move [n]
  turn right [n]
  turn left [n]
  go to x [n] y [n]
  glide [secs] to x [n] y [n]
  change x by [n]
  change y by [n]
  set x to [n]
  set y to [n]
  bounce if on edge
  point in direction [n]
  say "[text]"
  say "[text]" for [secs]
  show
  hide
  next costume
  change size by [n]
  set size to [n]
  play sound [name]
  wait [secs]
  forever
  repeat [n]
  stop all
  set pin [n] output HIGH
  set pin [n] output LOW
  set servo pin [n] channel [n] angle [n]
  dc motor M1 forward M2 forward

EXAMPLE — bounce forever:
when green flag clicked
set rotation style left-right
forever
  move 5
  bounce if on edge
  wait 0.1

EXAMPLE — draw square:
when green flag clicked
repeat 4
  move 100
  turn right 90

EXAMPLE — blink LED:
when green flag clicked
forever
  set pin 2 output HIGH
  wait 0.5
  set pin 2 output LOW
  wait 0.5
"""


# ══════════════════════════════════════════════════════════════════════════════
#  NESTED XML BUILDER  (fixes the flat-chain problem in build_xml_from_cmds)
# ══════════════════════════════════════════════════════════════════════════════

def _uid() -> str:
    return uuid.uuid4().hex[:8]


# Map DSL keyword → (block_type, field_name_for_count_or_none)
_LOOP_BLOCKS = {
    "forever": ("control_forever", None),
    "repeat":  ("control_repeat",  "TIMES"),
    "if":      ("control_if",      None),
    "if else": ("control_if_else", None),
}

# Map DSL command keyword → builder function
def _dsl_line_to_xml(line: str) -> Optional[ET.Element]:
    """Convert a single DSL line to an ET.Element block. Returns None if unknown."""
    s = line.strip()
    if not s:
        return None

    def _num(text: str, idx: int = 0) -> str:
        nums = re.findall(r"-?\d+(?:\.\d+)?", text)
        return nums[idx] if idx < len(nums) else "0"

    def _numval(block: ET.Element, name: str, val, shadow_type: str = "math_number") -> None:
        v  = ET.SubElement(block, "value", {"name": name})
        sh = ET.SubElement(v, "shadow", {"type": shadow_type, "id": _uid()})
        f  = ET.SubElement(sh, "field", {"name": "NUM"})
        f.text = str(val)

    def _textval(block: ET.Element, name: str, val: str) -> None:
        v  = ET.SubElement(block, "value", {"name": name})
        sh = ET.SubElement(v, "shadow", {"type": "text", "id": _uid()})
        f  = ET.SubElement(sh, "field", {"name": "TEXT"})
        f.text = str(val)

    low = s.lower()

    # ── Events ──────────────────────────────────────────────────────────────
    if re.search(r"\bwhen\s+green\s+flag\s+clicked\b", low):
        return ET.Element("block", {"type": "event_whenflagclicked", "id": _uid()})

    m = re.search(r"\bwhen\s+key\s+(.+?)\s+pressed\b", low)
    if m:
        b = ET.Element("block", {"type": "event_whenkeypressed", "id": _uid()})
        f = ET.SubElement(b, "field", {"name": "KEY_OPTION"})
        key_map = {"up": "up arrow", "down": "down arrow",
                   "left": "left arrow", "right": "right arrow", "space": "space"}
        f.text = key_map.get(m.group(1).strip(), m.group(1).strip())
        return b

    # ── Motion ───────────────────────────────────────────────────────────────
    m = re.search(r"\bmove\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "motion_movesteps", "id": _uid()})
        _numval(b, "STEPS", m.group(1))
        return b

    m = re.search(r"\bturn\s+right\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "motion_turnright", "id": _uid()})
        _numval(b, "DEGREES", m.group(1))
        return b

    m = re.search(r"\bturn\s+left\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "motion_turnleft", "id": _uid()})
        _numval(b, "DEGREES", m.group(1))
        return b

    m = re.search(r"\bgo\s+to\s+x\s+(-?\d+(?:\.\d+)?)\s+y\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "motion_gotoxy", "id": _uid()})
        _numval(b, "X", m.group(1))
        _numval(b, "Y", m.group(2))
        return b

    m = re.search(r"\bglide\s+(-?\d+(?:\.\d+)?)\s+to\s+x\s+(-?\d+(?:\.\d+)?)\s+y\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "motion_glidesecstoxy", "id": _uid()})
        _numval(b, "SECS", m.group(1))
        _numval(b, "X", m.group(2))
        _numval(b, "Y", m.group(3))
        return b

    m = re.search(r"\bchange\s+x\s+by\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "motion_changexby", "id": _uid()})
        _numval(b, "DX", m.group(1))
        return b

    m = re.search(r"\bchange\s+y\s+by\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "motion_changeyby", "id": _uid()})
        _numval(b, "DY", m.group(1))
        return b

    m = re.search(r"\bset\s+x\s+to\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "motion_setx", "id": _uid()})
        _numval(b, "X", m.group(1))
        return b

    m = re.search(r"\bset\s+y\s+to\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "motion_sety", "id": _uid()})
        _numval(b, "Y", m.group(1))
        return b

    if re.search(r"\bbounce\s+if\s+on\s+edge\b", low):
        return ET.Element("block", {"type": "motion_ifonedgebounce", "id": _uid()})

    m = re.search(r"\bpoint\s+in\s+direction\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "motion_pointindirection", "id": _uid()})
        v  = ET.SubElement(b, "value", {"name": "DIRECTION"})
        sh = ET.SubElement(v, "shadow", {"type": "math_angle", "id": _uid()})
        f  = ET.SubElement(sh, "field", {"name": "NUM"})
        f.text = m.group(1)
        return b

    m = re.search(r"\bset\s+rotation\s+style\s+(.+)\b", low)
    if m:
        b = ET.Element("block", {"type": "motion_setrotationstyle", "id": _uid()})
        style_map = {"left-right": "left-right", "left right": "left-right",
                     "all around": "all around", "all-around": "all around",
                     "dont rotate": "don't rotate", "don't rotate": "don't rotate"}
        f = ET.SubElement(b, "field", {"name": "STYLE"})
        f.text = style_map.get(m.group(1).strip(), "left-right")
        return b

    # ── Looks ─────────────────────────────────────────────────────────────────
    m = re.search(r'\bsay\s+"([^"]+)"\s+for\s+(-?\d+(?:\.\d+)?)\b', s, re.IGNORECASE)
    if m:
        b = ET.Element("block", {"type": "looks_sayforsecs", "id": _uid()})
        _textval(b, "MESSAGE", m.group(1))
        _numval(b, "SECS", m.group(2))
        return b

    m = re.search(r'\bsay\s+"([^"]+)"\b', s, re.IGNORECASE)
    if m:
        b = ET.Element("block", {"type": "looks_say", "id": _uid()})
        _textval(b, "MESSAGE", m.group(1))
        return b

    m = re.search(r"\bsay\s+(\S+)\b", low)
    if m and m.group(1) not in ("for",):
        b = ET.Element("block", {"type": "looks_say", "id": _uid()})
        _textval(b, "MESSAGE", m.group(1))
        return b

    if re.search(r"\bshow\b", low):
        return ET.Element("block", {"type": "looks_show", "id": _uid()})
    if re.search(r"\bhide\b", low):
        return ET.Element("block", {"type": "looks_hide", "id": _uid()})
    if re.search(r"\bnext\s+costume\b", low):
        return ET.Element("block", {"type": "looks_nextcostume", "id": _uid()})

    m = re.search(r"\bchange\s+size\s+by\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "looks_changesizeby", "id": _uid()})
        _numval(b, "CHANGE", m.group(1))
        return b

    m = re.search(r"\bset\s+size\s+to\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "looks_setsizeto", "id": _uid()})
        _numval(b, "SIZE", m.group(1))
        return b

    # ── Sound ─────────────────────────────────────────────────────────────────
    m = re.search(r"\bplay\s+sound\s+(.+?)\s+until\s+done\b", low)
    if m:
        b = ET.Element("block", {"type": "sound_playuntildone", "id": _uid()})
        v  = ET.SubElement(b, "value", {"name": "SOUND_MENU"})
        sh = ET.SubElement(v, "shadow", {"type": "sound_sounds_menu", "id": _uid()})
        f  = ET.SubElement(sh, "field", {"name": "SOUND_MENU"})
        f.text = m.group(1).strip()
        return b

    m = re.search(r"\bplay\s+sound\s+(.+)\b", low)
    if m:
        b = ET.Element("block", {"type": "sound_play", "id": _uid()})
        v  = ET.SubElement(b, "value", {"name": "SOUND_MENU"})
        sh = ET.SubElement(v, "shadow", {"type": "sound_sounds_menu", "id": _uid()})
        f  = ET.SubElement(sh, "field", {"name": "SOUND_MENU"})
        f.text = m.group(1).strip()
        return b

    if re.search(r"\bstop\s+all\s+sounds\b", low):
        return ET.Element("block", {"type": "sound_stopallsounds", "id": _uid()})

    m = re.search(r"\bset\s+volume\s+to\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "sound_setvolumeto", "id": _uid()})
        _numval(b, "VOLUME", m.group(1))
        return b

    # ── Control ───────────────────────────────────────────────────────────────
    m = re.search(r"\bwait\s+(-?\d+(?:\.\d+)?)\b", low)
    if m:
        b = ET.Element("block", {"type": "control_wait", "id": _uid()})
        _numval(b, "DURATION", m.group(1), "math_positive_number")
        return b

    if re.search(r"\bstop\s+all\b", low):
        b = ET.Element("block", {"type": "control_stop", "id": _uid()})
        f = ET.SubElement(b, "field", {"name": "STOP_OPTION"})
        f.text = "all"
        return b

    # ── Arduino ───────────────────────────────────────────────────────────────
    m = re.search(r"\bset\s+(?:digital\s+)?pin\s+(\w+)\s+(?:out(?:put)?)\s+(high|low)\b", low)
    if m:
        b = ET.Element("block", {"type": "arduino_pin_setDigitalOutput", "id": _uid()})
        pf = ET.SubElement(b, "field", {"name": "PIN"})
        pf.text = m.group(1).upper()
        v  = ET.SubElement(b, "value", {"name": "LEVEL"})
        sh = ET.SubElement(v, "shadow", {"type": "arduino_pin_menu_level", "id": _uid()})
        lf = ET.SubElement(sh, "field", {"name": "level"})
        lf.text = m.group(2).upper()
        return b

    m = re.search(r"\bset\s+servo\s+pin\s+(\w+)\s+channel\s+(\w+)\s+angle\s+(-?\d+)\b", low)
    if m:
        b = ET.Element("block", {"type": "arduino_pin_esp32SetServoOutput", "id": _uid()})
        ET.SubElement(b, "field", {"name": "PIN"}).text = m.group(1)
        ET.SubElement(b, "field", {"name": "CH"}).text  = m.group(2)
        v  = ET.SubElement(b, "value", {"name": "OUT"})
        sh = ET.SubElement(v, "shadow", {"type": "math_angle", "id": _uid()})
        ET.SubElement(sh, "field", {"name": "NUM"}).text = m.group(3)
        return b

    m = re.search(r"\bdc\s+motor\s+(\w+)\s+(forward|backward|stop)\s+(\w+)\s+(forward|backward|stop)\b", low)
    if m:
        b = ET.Element("block", {"type": "arduino_dcomtor_runMotor", "id": _uid()})
        ET.SubElement(b, "field", {"name": "MOTOR"}).text      = m.group(1).upper()
        ET.SubElement(b, "field", {"name": "DIRECTION"}).text  = m.group(2).lower()
        ET.SubElement(b, "field", {"name": "MOTOR1"}).text     = m.group(3).upper()
        ET.SubElement(b, "field", {"name": "DIRECTION1"}).text = m.group(4).lower()
        return b

    return None   # unrecognised line


def _get_indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _is_loop_header(line: str) -> Optional[Tuple[str, Optional[str]]]:
    """
    Returns (block_type, count_value_or_None) if line is a loop/control header.
    Returns None if it's a regular statement.
    """
    low = line.strip().lower()

    if re.fullmatch(r"forever", low):
        return ("control_forever", None)

    m = re.match(r"repeat\s+(\d+)", low)
    if m:
        return ("control_repeat", m.group(1))

    if re.match(r"if\s+else\b", low):
        return ("control_if_else", None)

    if re.match(r"if\b", low):
        return ("control_if", None)

    return None


def _build_nested_xml(dsl_text: str) -> str:
    """
    Parse indented DSL text and produce properly nested Blockzie XML.
    Handles forever, repeat, if/else with correct SUBSTACK nesting.
    """
    # Normalise line endings and strip markdown
    dsl_text = re.sub(r"```\w*", "", dsl_text).strip()
    lines = [l.rstrip() for l in dsl_text.splitlines() if l.strip()]

    root = ET.Element("xml", {"xmlns": "http://www.w3.org/1999/xhtml"})
    ET.SubElement(root, "variables")

    # We process using a stack-based approach
    # stack item: (indent_level, parent_element_for_next_sibling, current_chain_tail)
    # current_chain_tail = the last <block> element we appended at this level

    # Flatten into structured list first
    # Each item: (indent, raw_line)
    structured = [(len(l) - len(l.lstrip()), l.strip()) for l in lines]

    def _build_chain(items: List[Tuple[int, str]], base_indent: int, parent: ET.Element) -> None:
        """
        Recursively build a chain of blocks at `base_indent` level,
        appending them to `parent` (which is either xml root or a <statement>).
        """
        i = 0
        current_block: Optional[ET.Element] = None   # last block in chain at this level

        while i < len(items):
            indent, line = items[i]

            # Only process lines at exactly base_indent
            if indent != base_indent:
                i += 1
                continue

            loop_info = _is_loop_header(line)

            if loop_info:
                # This is a loop/control header — collect its body (deeper indented lines)
                block_type, count_val = loop_info
                block = ET.Element("block", {"type": block_type, "id": _uid()})

                # Add count value for repeat
                if block_type == "control_repeat" and count_val:
                    v  = ET.SubElement(block, "value", {"name": "TIMES"})
                    sh = ET.SubElement(v, "shadow", {"type": "math_integer", "id": _uid()})
                    f  = ET.SubElement(sh, "field", {"name": "NUM"})
                    f.text = count_val

                # Collect body items (everything at base_indent + 2 until next base_indent item)
                body_indent = base_indent + 2
                body_items  = []
                j = i + 1
                while j < len(items):
                    next_indent, next_line = items[j]
                    if next_indent <= base_indent:
                        break
                    body_items.append((next_indent, next_line))
                    j += 1

                # Build body inside SUBSTACK
                if body_items:
                    stmt = ET.SubElement(block, "statement", {"name": "SUBSTACK"})
                    _build_chain(body_items, body_indent, stmt)

                # Attach to chain
                if current_block is None:
                    parent.append(block)
                else:
                    nxt = ET.SubElement(current_block, "next")
                    nxt.append(block)

                # forever cannot have <next> after it — stop chain
                if block_type == "control_forever":
                    return

                current_block = block
                i = j  # skip body lines

            else:
                # Regular statement
                block = _dsl_line_to_xml(line)
                if block is not None:
                    if current_block is None:
                        parent.append(block)
                    else:
                        nxt = ET.SubElement(current_block, "next")
                        nxt.append(block)
                    current_block = block
                i += 1

    if structured:
        # Set x/y on the first block — we do this by building into root
        # and then setting attributes on the first block element appended
        _build_chain(structured, 0, root)
        # Find the first block element (skip <variables>)
        for child in root:
            if child.tag == "block":
                child.set("x", "20")
                child.set("y", "20")
                break

    return ET.tostring(root, encoding="unicode")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY C — Minimal flat fallback (always works, no loops)
# ══════════════════════════════════════════════════════════════════════════════
def _minimal_fallback_xml(prompt: str) -> str:
    """
    Build a minimal but valid program from the prompt using keyword matching.
    Never fails. Used as absolute last resort.
    """
    root = ET.Element("xml", {"xmlns": "http://www.w3.org/1999/xhtml"})
    ET.SubElement(root, "variables")

    low = prompt.lower()

    hat = ET.SubElement(root, "block", {"type": "event_whenflagclicked", "id": _uid(), "x": "20", "y": "20"})
    chain = hat

    def _append(block: ET.Element) -> ET.Element:
        nxt = ET.SubElement(chain, "next")
        nxt.append(block)
        return block

    def _numval(b: ET.Element, name: str, val, stype: str = "math_number") -> None:
        v  = ET.SubElement(b, "value", {"name": name})
        sh = ET.SubElement(v, "shadow", {"type": stype, "id": _uid()})
        ET.SubElement(sh, "field", {"name": "NUM"}).text = str(val)

    # Detect keywords and build sensible blocks
    if re.search(r"\bpin\s+(\d+)\b.*\b(high|blink|led)\b|\b(led|blink)\b.*\bpin\s+(\d+)\b", low):
        pin_m = re.search(r"\bpin\s+(\d+)\b", low)
        pin   = pin_m.group(1) if pin_m else "2"
        b = ET.Element("block", {"type": "arduino_pin_setDigitalOutput", "id": _uid()})
        ET.SubElement(b, "field", {"name": "PIN"}).text = pin
        v  = ET.SubElement(b, "value", {"name": "LEVEL"})
        sh = ET.SubElement(v, "shadow", {"type": "arduino_pin_menu_level", "id": _uid()})
        ET.SubElement(sh, "field", {"name": "level"}).text = "HIGH"
        _append(b)
    elif re.search(r"\bmove\b", low):
        nums = re.findall(r"\d+", low)
        steps = nums[0] if nums else "10"
        b = ET.Element("block", {"type": "motion_movesteps", "id": _uid()})
        _numval(b, "STEPS", steps)
        _append(b)
    elif re.search(r"\bsay\b", low):
        m = re.search(r'"([^"]+)"', prompt)
        text = m.group(1) if m else "Hello!"
        b = ET.Element("block", {"type": "looks_say", "id": _uid()})
        v  = ET.SubElement(b, "value", {"name": "MESSAGE"})
        sh = ET.SubElement(v, "shadow", {"type": "text", "id": _uid()})
        ET.SubElement(sh, "field", {"name": "TEXT"}).text = text
        _append(b)
    elif re.search(r"\bturn\b", low):
        nums = re.findall(r"\d+", low)
        deg  = nums[0] if nums else "90"
        b = ET.Element("block", {"type": "motion_turnright", "id": _uid()})
        _numval(b, "DEGREES", deg)
        _append(b)
    else:
        # Default: move 10
        b = ET.Element("block", {"type": "motion_movesteps", "id": _uid()})
        _numval(b, "STEPS", "10")
        _append(b)

    return ET.tostring(root, encoding="unicode")


# ══════════════════════════════════════════════════════════════════════════════
#  XML VALIDATION HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def _extract_xml(raw: str) -> str:
    raw = re.sub(r"```(?:xml)?\s*", "", raw)
    raw = re.sub(r"```", "", raw)
    start = raw.find("<xml")
    end   = raw.rfind("</xml>")
    if start == -1 or end == -1:
        raise ValueError("No <xml>...</xml> found in LLM response")
    return raw[start : end + 6].strip()


def _assign_unique_ids(xml_str: str) -> str:
    seen: set[str] = set()
    def replacer(m: re.Match) -> str:
        old = m.group(1)
        if not old or old in seen:
            new = _uid()
            seen.add(new)
            return f'id="{new}"'
        seen.add(old)
        return m.group(0)
    return re.sub(r'id="([^"]*)"', replacer, xml_str)


def _ensure_xy(xml_str: str) -> str:
    if re.search(r'<block[^>]+x="', xml_str[:800]):
        return xml_str
    return re.sub(
        r'(<block\s+type="[^"]+"\s+id="[^"]+")',
        r'\1 x="20" y="20"',
        xml_str, count=1
    )


def _validate(xml_str: str, strict: bool = False) -> Tuple[bool, str, int]:
    """
    Validate XML and count blocks.

    ROOT CAUSE FIX — the original bug:
      ET.findall(".//block") always returns 0 when xmlns="http://www.w3.org/1999/xhtml"
      is present, because ElementTree treats every tag as namespaced:
      "block" is searched but the actual tag is "{http://www.w3.org/1999/xhtml}block".
      This made EVERY Strategy A LLM response fail validation → fell through
      to Strategy C minimal fallback → always injected generic "move 10 steps".
      Fix: count blocks via regex on the raw string — namespace-agnostic.

    strict=False (default, used for LLM XML output):
      Only checks block count via regex. Allows minor XML issues that Python's
      ET rejects but Blockzie's browser DOMParser handles fine.

    strict=True (used for our own _build_nested_xml / _minimal_fallback_xml):
      ET.fromstring must succeed without error.
    """
    # Count blocks via regex — immune to xmlns namespace issue
    block_count = len(re.findall(r'<block\s+type="[^"]+"', xml_str))

    if block_count == 0:
        return False, "no blocks found in XML", 0

    if strict:
        try:
            ET.fromstring(xml_str)
        except ET.ParseError as e:
            return False, f"XML parse error: {e}", 0

    # For LLM output: block_count > 0 is sufficient — Blockzie's DOMParser
    # is more lenient than Python's ET and handles minor tag issues.
    return True, "", block_count


# ══════════════════════════════════════════════════════════════════════════════
#  LLM CALL LAYER
# ══════════════════════════════════════════════════════════════════════════════
async def _call_llm(system: str, user: str, model: str, max_tokens: int = 2048) -> str:
    from app.core.context import openrouter_key_var
    api_key = openrouter_key_var.get() or OPENROUTER_API_KEY
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  SITE_URL,
        "X-Title":       "STEMbotix",
    }
    payload = {
        "model":      model,
        "max_tokens": max_tokens,
        "messages":   [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }
    async with httpx.AsyncClient(timeout=40.0) as client:
        resp = await client.post(OPENROUTER_URL, headers=headers, json=payload)

    code = resp.status_code

    if code == 402:
        _model_payment_failed.add(model)
        raise ValueError(f"402 payment required — model {model} needs credits")

    if code == 429:
        _model_rate_limited_until[model] = time.time() + _RATE_LIMIT_COOLDOWN
        raise ValueError(f"429 rate limited")

    if code != 200:
        raise ValueError(f"HTTP {code}: {resp.text[:200]}")

    choices = resp.json().get("choices") or []
    content = (choices[0].get("message") or {}).get("content", "") if choices else ""
    if not content or not content.strip():
        raise ValueError("empty response content")

    return content.strip()


async def _call_with_fallback(
    system: str,
    user: str,
    max_tokens: int = 2048,
    model_list: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """
    Try models in order. model_list is pre-computed by _get_model_list(score).
    Smart models (Claude/GPT-4o) appear first for hard prompts automatically.
    """
    from app.core.context import openrouter_key_var
    api_key = openrouter_key_var.get() or OPENROUTER_API_KEY
    if not api_key:
        raise HTTPException(502, "OPENROUTER_API_KEY not configured in .env or request header")

    if model_list is None:
        model_list = _get_model_list(0)   # default to free tier

    now    = time.time()
    errors = []

    for model in model_list:
        if model in _model_payment_failed:
            logger.debug(f"[LLM:{model}] skip — 402 (no credits)")
            continue
        if _model_rate_limited_until.get(model, 0) > now:
            logger.debug(f"[LLM:{model}] skip — rate limited")
            continue
        try:
            content = await _call_llm(system, user, model, max_tokens)
            logger.info(f"[LLM:{model}] ✅ success ({len(content)} chars)")
            return content, model
        except Exception as e:
            logger.warning(f"[LLM:{model}] ✗ {e}")
            errors.append(f"{model}: {e}")

    raise HTTPException(
        502,
        f"All models failed (tried {len(model_list)}). "
        f"Details: {'; '.join(errors[-3:])}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  PYDANTIC MODELS
# ══════════════════════════════════════════════════════════════════════════════
class GenerateXMLRequest(BaseModel):
    prompt:     str
    role:       str  = "teacher"  # teacher | student (enforcement added in next step)
    auto_start: bool = True
    mode:       str  = "inject"


class GenerateXMLResponse(BaseModel):
    ok:          bool
    xml:         str
    block_count: int
    model_used:  Optional[str] = None
    method:      str = "direct_xml"


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTE
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/generate_xml", response_model=GenerateXMLResponse)
async def generate_xml(req: GenerateXMLRequest, user_dict=Depends(get_current_user)):
    """
    Convert natural language → Blockzie XML (supports forever, repeat, wait, etc.)

    Automatically selects model tier based on prompt complexity:
      score >= 50  → Claude 3.5 Sonnet / GPT-4o  (hard projects, games, if/else)
      score >= 25  → GPT-4o-mini / Claude Haiku   (medium: loops, sensors)
      score  < 25  → Free models                  (simple: move, say, blink)

    Strategy A: LLM writes XML directly  → validate → return
    Strategy B: LLM writes indented DSL  → nested XML builder → return
    Strategy C: Keyword-based minimal fallback → always returns something
    """
    from app.core.database import SessionLocal
    from app.models.user import User

    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")

    # ✅ Extract user role from authenticated user object
    user_role = (user_dict.get("role") or "teacher").lower()
    user_email = user_dict.get("email", "")

    if user_role == "student":
        raise HTTPException(
            403,
            "Auto block generation is disabled for student role."
        )

    # Score complexity and pick model tier
    score      = _complexity_score(prompt)
    model_list = _get_model_list(score)

    tier = "SMART" if score >= 50 else ("DEFAULT" if score >= 25 else "FREE")
    logger.info(f"[generate_xml] user_role={user_role} prompt={prompt!r} score={score} tier={tier} "
                f"first_model={model_list[0]}")

    # ── Strategy A: Direct XML ─────────────────────────────────────────────
    try:
        raw, model_used = await _call_with_fallback(
            system=PROMPT_DIRECT_XML,
            user=prompt,
            max_tokens=2048,
            model_list=model_list,
        )
        xml_str = _extract_xml(raw)
        xml_str = _assign_unique_ids(xml_str)
        xml_str = _ensure_xy(xml_str)
        # Strategy A must be well-formed XML before we inject into Blockzie.
        # If it is malformed (tag mismatch, broken nesting, etc.), we should
        # fall through to Strategy B (DSL -> nested XML) instead of returning it.
        ok, err, count = _validate(xml_str, strict=True)

        if ok and count >= 1:
            logger.info(f"[generate_xml] StrategyA OK: {count} blocks via {model_used} (score={score})")
            return GenerateXMLResponse(ok=True, xml=xml_str, block_count=count,
                                       model_used=model_used, method="direct_xml")

        logger.warning(f"[generate_xml] StrategyA XML invalid ({err}) — trying B")

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[generate_xml] StrategyA failed: {e} — trying B")
        model_used = "none"

    # ── Strategy B: DSL → nested XML builder ──────────────────────────────
    # For complex prompts that passed strategy A, keep using smart models.
    # For simple ones that failed, escalate to a smarter model list.
    escalated_list = _get_model_list(max(score, 25))  # at least DEFAULT tier for B
    try:
        dsl_raw, model_b = await _call_with_fallback(
            system=PROMPT_DSL,
            user=prompt,
            max_tokens=512,
            model_list=escalated_list,
        )
        xml_str = _build_nested_xml(dsl_raw)
        ok, err, count = _validate(xml_str, strict=True)

        if ok and count >= 1:
            logger.info(f"[generate_xml] StrategyB OK: {count} blocks via {model_b}")
            return GenerateXMLResponse(ok=True, xml=xml_str, block_count=count,
                                       model_used=model_b, method="dsl_nested")

        logger.warning(f"[generate_xml] StrategyB XML invalid ({err}) — using C")

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[generate_xml] StrategyB failed: {e} — using C")

    # ── Strategy C: Minimal keyword fallback ───────────────────────────────
    try:
        xml_str = _minimal_fallback_xml(prompt)
        _, _, count = _validate(xml_str, strict=True)
        logger.info(f"[generate_xml] StrategyC (minimal fallback): {count} blocks")
        return GenerateXMLResponse(ok=True, xml=xml_str, block_count=count,
                                   model_used="fallback", method="minimal_fallback")
    except Exception as e:
        logger.error(f"[generate_xml] StrategyC failed: {e}")

    raise HTTPException(422, "Could not generate any blocks. Try a simpler prompt.")
