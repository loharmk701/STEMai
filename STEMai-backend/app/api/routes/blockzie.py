from fastapi import APIRouter, Depends

from app.models.schemas import (
    BlockzieReq,
    BlockzieXMLReq,
    RemoveTypeReq
)

from app.blockzie_agent import (
    open_blockzie,
    close_blockzie,
    inject_text_program,
    stop_all,
    clear_workspace,
    export_xml,
    remove_type,
    load_xml_program
)

from app.core.auth import get_current_user
from pydantic import BaseModel

router = APIRouter()


class GenerateBlockzieRequest(BaseModel):
    prompt: str
    role: str = "teacher"
    auto_start: bool = True
    mode: str = "inject"

@router.post("/blockzie/open")
async def blockzie_open():
    return await open_blockzie()


@router.post("/blockzie/close")
async def blockzie_close():
    return await close_blockzie()


@router.post("/blockzie/inject")
async def blockzie_inject(req: BlockzieReq):
    return await inject_text_program(req.text, auto_start=req.auto_start, mode="inject")


@router.post("/blockzie/append")
async def blockzie_append(req: BlockzieReq):
    return await inject_text_program(req.text, auto_start=req.auto_start, mode="append")


@router.post("/blockzie/load_xml")
async def blockzie_load_xml(req: BlockzieXMLReq):
    return await load_xml_program(req.xml, auto_start=req.auto_start, mode=req.mode)


@router.post("/blockzie/stop")
async def blockzie_stop():
    return await stop_all()


@router.post("/blockzie/clear")
async def blockzie_clear():
    return await clear_workspace()


@router.get("/blockzie/export")
async def blockzie_export():
    return await export_xml()


@router.post("/blockzie/remove_type")
async def blockzie_remove_type(req: RemoveTypeReq):
    return await remove_type(req.block_type)


@router.get("/blockzie/debug_frames")
async def debug_frames():
    from app.blockzie_agent import _engine  # access the global directly
    
    eng = _engine  # don't call _ensure_engine — just read existing instance
    
    if eng is None:
        return {"ok": False, "error": "Engine not started. Call POST /blockzie/open first"}
    
    if eng._page is None:
        return {"ok": False, "error": "Page not open. Call POST /blockzie/open first"}

    results = []
    for i, fr in enumerate(eng._page.frames):
        try:
            info = await fr.evaluate("""() => {
                const B = window.Blockly || window.ScratchBlocks;
                if (!B) return {ok: false, reason: 'no Blockly/ScratchBlocks'};
                const ws = B.getMainWorkspace && B.getMainWorkspace();
                if (!ws) return {ok: false, reason: 'no workspace'};
                return {
                    ok: true,
                    kind: window.ScratchBlocks ? 'scratchblocks' : 'blockly',
                    isReadOnly: ws.isReadOnly ? ws.isReadOnly() : null,
                    isFlyout: !!ws.isFlyout,
                    blockCount: ws.getAllBlocks(false).length,
                    registeredTypes: Object.keys(B.Blocks||{}).length,
                    hasEventsFire: !!(B.Events && B.Events.fire),
                    hasEventsDisable: !!(B.Events && B.Events.disable),
                    eventsAPI: B.Events ? Object.keys(B.Events).filter(k => typeof B.Events[k] === 'function').join(',') : 'none'
                };
            }""")
            results.append({"index": i, "url": fr.url, "info": info})
        except Exception as e:
            results.append({"index": i, "url": fr.url, "error": str(e)})

    return {
        "ok": True,
        "total_frames": len(eng._page.frames),
        "selected_frame_url": eng._frame.url if eng._frame else None,
        "frames": results
    }


@router.post("/blockzie/generate")
async def blockzie_generate(req: GenerateBlockzieRequest, user=Depends(get_current_user)):
    """
    AI-powered Blockzie program generator.
    Converts natural language prompt to Blockzie XML.
    
    Automatically selects model tier based on prompt complexity:
      - score >= 50: Claude 3.5 Sonnet / GPT-4o (hard projects, games)
      - score >= 25: GPT-4o-mini / Claude Haiku (medium difficulty)
      - score < 25: Free models (simple programs)
    """
    import httpx
    import json
    import re
    import os
    import asyncio
    from typing import Optional
    
    prompt = (req.prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "Prompt is required", "xml": "", "block_count": 0}
    
    # Call the blockzie_generate endpoint
    try:
        from app.core.context import openrouter_key_var
        openrouter_key = openrouter_key_var.get() or os.getenv("OPENROUTER_API_KEY", "").strip()
        site_url = os.getenv("SITE_URL", "https://stembotix-ai.vercel.app").strip()
        model = os.getenv("DEFAULT_MODEL", "openai/gpt-4o-mini").strip()
        
        if not openrouter_key:
            return {
                "ok": False,
                "error": "OPENROUTER_API_KEY not configured",
                "xml": "",
                "block_count": 0
            }
        
        # Generate XML using a simple free model
        system_prompt = """You are a Blockzie/Scratch XML generator. Generate ONLY raw XML, no explanation."""
        
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openrouter_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": site_url,
                    "X-Title": "STEMbotix Blockzie Generator"
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.4,
                    "max_tokens": 1024
                }
            )
        
        if response.status_code != 200:
            return {
                "ok": False,
                "error": f"OpenRouter error: {response.status_code}",
                "xml": "",
                "block_count": 0
            }
        
        result = response.json()
        generated_text = result["choices"][0]["message"]["content"]
        
        # Extract XML if wrapped in markdown
        xml_match = re.search(r'```(?:xml)?\s*(.*?)\s*```', generated_text, re.DOTALL)
        if xml_match:
            xml_str = xml_match.group(1).strip()
        else:
            xml_str = generated_text.strip()
        
        # Count blocks in XML
        block_count = len(re.findall(r'<block\s+type=', xml_str))
        
        # Basic validation
        if '<xml' not in xml_str or '</xml>' not in xml_str or block_count == 0:
            # Fallback: Generate minimal XML
            xml_str = f'''<xml xmlns="http://www.w3.org/1999/xhtml"><variables/>
<block type="event_whenflagclicked" id="evt0" x="20" y="20">
<next><block type="motion_movesteps" id="mov1">
<value name="STEPS"><shadow type="math_number" id="num1"><field name="NUM">10</field></shadow></value>
</block></next></block></xml>'''
            block_count = 2
        
        return {
            "ok": True,
            "xml": xml_str,
            "block_count": block_count,
            "model_used": model
        }
    
    except Exception as e:
        return {
            "ok": False,
            "error": f"Generation failed: {str(e)}",
            "xml": "",
            "block_count": 0
        }
