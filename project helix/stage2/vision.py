"""Bounded OpenRouter visual interpretation; never emits legal or engineering conclusions."""
from __future__ import annotations

import base64
import json
import os
import re
import time
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MODEL = "qwen/qwen3.8-flash"  # Verified in the isolated research spike; one image / one call.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
FORBIDDEN = re.compile(r"\b(compliant|code compliant|safe|structurally sound|electrical capacity|approved for|business fit)\b", re.I)
INTERIOR_PROMPT = """This is a 3 by 2 contact sheet of six camera directions from ONE Matterport panorama location. Inspect every tile. Describe only directly visible interior features: entrances, doors, circulation, stairs, partitions, ceiling/space configuration, visible utilities, fixtures and obstructions. Return JSON only: {\"observations\":[{\"feature\":string,\"evidence\":string,\"face_index\":integer 0..5,\"bbox_xyxy_normalized\":[number,number,number,number]|null,\"status\":\"visible\"|\"uncertain\"}]} . Bboxes use 0..1 normalized coordinates relative to the individual face tile (not the full sheet). Do not infer hidden systems, condition, safety, capacity, code/legal compliance, or business use. If evidence is insufficient, return no observation."""
EXTERIOR_PROMPT = """Describe only features directly visible in this Vexcel property image. Return JSON only: {\"observations\":[{\"feature\":string,\"evidence\":string,\"bbox_xyxy_normalized\":[number,number,number,number]|null,\"status\":\"visible\"|\"uncertain\"}]} . Bboxes use 0..1 normalized image coordinates. For aerial views consider roof/building configuration, parking, driveways, vehicle access, adjacent structures, and exterior obstructions; for oblique views consider visible entrances, doors, loading bays and facade configuration. Do not infer hidden systems, structural condition, safety, code/legal compliance, or business use. If evidence is insufficient, return no observation."""


def validate_visual_observation(item, image_ref):
    if not isinstance(item, dict) or not image_ref or not image_ref.get("image_id"):
        return None
    feature, evidence = item.get("feature"), item.get("evidence")
    if not isinstance(feature, str) or not feature.strip() or not isinstance(evidence, str) or not evidence.strip():
        return None
    if FORBIDDEN.search(feature) or FORBIDDEN.search(evidence):
        return None
    box = item.get("bbox_xyxy_normalized")
    if box is not None:
        if not isinstance(box, list) or len(box) != 4 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not 0 <= v <= 1000 for v in box):
            return None
        # Vision endpoints commonly encode normalized image coordinates on a
        # 0..1000 grid despite the prompt's normalized-coordinate request.
        # Normalize that documented image grid before exposing it downstream.
        if any(v > 1 for v in box):
            box = [round(v / 1000, 4) for v in box]
        if box[0] >= box[2] or box[1] >= box[3]:
            return None
    if item.get("status") not in ("visible", "uncertain"):
        return None
    return {"feature":feature.strip(),"evidence":evidence.strip(),"bbox_xyxy_normalized":box,
            "status":item["status"],"source":"visual_observation","image_id":image_ref["image_id"],
            "model_id":image_ref.get("model_id"),"location_id":image_ref.get("location_id"),
            "evidence_ref":image_ref.get("evidence_ref"),"captured_at":image_ref.get("captured_at"),
            "retrieved_at":image_ref.get("retrieved_at"),"interpretation":"visual description only; not a measurement or compliance finding"}


def _completion(image_bytes, content_type, prompt, *, opener=urlopen):
    key=os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return {"status":"unavailable","reason":"OpenRouter credentials are unavailable for image analysis.","observations":[]}
    if not image_bytes or len(image_bytes)>MAX_IMAGE_BYTES or not content_type.startswith("image/"):
        return {"status":"unknown","reason":"Image is absent, too large or has an unsupported content type.","observations":[]}
    started=time.monotonic()
    for attempt in range(3):
        retry_prompt=prompt if attempt<2 else prompt+" Return one compact JSON object only; do not include markdown, explanations, or text outside JSON."
        payload={"model":MODEL,"temperature":0,"max_tokens":1600 if attempt==0 else 2400,"reasoning_effort":"none",
          "messages":[{"role":"user","content":[{"type":"text","text":retry_prompt},{"type":"image_url","image_url":{"url":f"data:{content_type};base64,{base64.b64encode(image_bytes).decode()}"}}]}]}
        if attempt>0:
            payload["response_format"]={"type":"json_object"}
        req=Request("https://openrouter.ai/api/v1/chat/completions",data=json.dumps(payload).encode(),
          headers={"Authorization":f"Bearer {key}","Content-Type":"application/json","HTTP-Referer":"https://helix.local","X-Title":"Helix Stage 2"},method="POST")
        try:
            with opener(req,timeout=45) as response: body=json.loads(response.read())
        except HTTPError as error:
            status="rate_limited" if error.code==429 else "temporarily_unavailable" if error.code>=500 else "unavailable"
            try: error.close()
            except Exception: pass
            if attempt<2 and (error.code==429 or error.code>=500):
                time.sleep(0.5)
                continue
            return {"status":status,"error":f"HTTP {error.code}","latency_ms":round((time.monotonic()-started)*1000),"observations":[]}
        except (URLError,TimeoutError,OSError,ValueError) as error:
            if attempt<2 and isinstance(error,(URLError,TimeoutError)):
                time.sleep(0.5)
                continue
            return {"status":"temporarily_unavailable","error":type(error).__name__,"latency_ms":round((time.monotonic()-started)*1000),"observations":[]}
        try:
            choice=(body.get("choices") or [{}])[0]
            content=((choice.get("message") or {}).get("content") or "").strip()
            if content.startswith("```"):
                content=re.sub(r"^```(?:json)?\s*|\s*```$","",content,flags=re.I).strip()
            parsed=json.loads(content)
            items=parsed.get("observations") or []
            out=[item for item in items[:20] if isinstance(item,dict)]
        except (ValueError,AttributeError,TypeError):
            if attempt<2:
                continue
            return {"status":"unknown","reason":"Vision response did not match the required JSON evidence contract.","latency_ms":round((time.monotonic()-started)*1000),"observations":[]}
        truncated=choice.get("finish_reason")=="length"
        if truncated and attempt<2:
            continue
        return {"status":"partially_complete" if truncated else "available","model":MODEL,"latency_ms":round((time.monotonic()-started)*1000),
                "usage":body.get("usage"),"observations":out,"truncated":truncated,"attempts":attempt+1}
    return {"status":"unknown","reason":"Vision response did not match the required JSON evidence contract.","latency_ms":round((time.monotonic()-started)*1000),"observations":[]}


def analyze_one_image(image_bytes, content_type, image_ref, *, opener=urlopen, prompt=None):
    """Analyze one provider image in the normal Investigate flow."""
    result=_completion(image_bytes,content_type,prompt or INTERIOR_PROMPT,opener=opener)
    out=[]
    for item in result.get("observations",[]):
        validated=validate_visual_observation(item,image_ref)
        if validated: out.append(validated)
    result["observations"]=out
    result["image_metadata"]=image_ref.get("image_metadata")
    return result


def analyze_matterport_panorama(face_images, image_ref, *, opener=urlopen):
    """Analyze all six Matterport cube faces together while retaining face-level localization."""
    if not face_images:
        return {"status":"unknown","reason":"No panorama faces were retrieved.","observations":[]}
    try:
        from PIL import Image, ImageDraw, ImageOps
        tile=384; label_h=28; cols=3; rows=2
        sheet=Image.new("RGB",(cols*tile,rows*(tile+label_h)),(24,40,36)); draw=ImageDraw.Draw(sheet)
        for index,(blob,content_type) in enumerate(face_images):
            with Image.open(BytesIO(blob)) as original:
                face=ImageOps.fit(original.convert("RGB"),(tile,tile))
            x=(index%cols)*tile; y=(index//cols)*(tile+label_h)
            sheet.paste(face,(x,y+label_h)); draw.text((x+8,y+6),f"Matterport camera face {index+1}",fill=(255,255,255))
        buf=BytesIO(); sheet.save(buf,format="JPEG",quality=86,optimize=True)
        contact=buf.getvalue()
    except Exception as error:
        return {"status":"unknown","reason":f"Panorama image assembly unavailable ({type(error).__name__}).","observations":[]}
    result=_completion(contact,"image/jpeg",INTERIOR_PROMPT,opener=opener)
    out=[]
    for item in result.get("observations",[]):
        face=item.get("face_index")
        if isinstance(face,bool) or not isinstance(face,int) or not 0<=face<6:
            continue
        ref={**image_ref,"image_id":f"{image_ref['image_id']}:face{face+1}",
             "evidence_ref":f"{image_ref.get('evidence_ref')} camera face {face+1}"}
        validated=validate_visual_observation(item,ref)
        if validated:
            validated.pop("face_index",None); validated["panorama_face"]=face+1
            out.append(validated)
    result["observations"]=out
    result["faces_analyzed"]=len(face_images)
    result["image_metadata"]={"contact_sheet_dimensions":{"width":1152,"height":824},"face_count":len(face_images)}
    return result
