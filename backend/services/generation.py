"""The one image-generation path.

``/api/agent/generate`` (+ ``/generate-batch`` jobs, so the MCP tools and
``agent_cli``) and the Assets tab's ``/api/assets/swarmui/generate`` both
call :func:`generate`: request overrides over the global SwarmUI settings,
img2img/inpaint refs, prompt injection (project style, location, shot,
character blocks), the panel characters' LoRAs and IP-Adapter sheet, the
content rating, the SwarmUI call, the transparent-background fallback, the
PNG recipe chunks, the asset store and binding the image to its panel.

Blocking work (project loads, image decoding, PNG re-encoding, asset files,
the project save) runs in worker threads; the project lock is taken only
inside :func:`bind_panel_image`, never across an await.
"""
import asyncio
import base64
import io
import json
import logging
from typing import Any, Dict, List, Optional

from PIL import Image

from ..core.config import LoraConfig, settings
from ..core.swarmui_client import swarm_client
from ..storage import asset_store as _astore
from ..storage.manager import InvalidProjectIdError, ProjectNotFoundError, storage

logger = logging.getLogger(__name__)


class GenerationError(RuntimeError):
    """SwarmUI failed (HTTP 502 on /api/agent/generate). ``rating`` is the
    content rating the request ran with."""

    def __init__(self, message: str, rating: Optional[str] = None):
        super().__init__(message)
        self.rating = rating


# camera-shot descriptors injected into prompts when a panel declares a shot:
# (danbooru tags, prose)
SHOT_TAGS = {
    "wide": ("wide shot, scenic view, full scenery", "全景远景，展现完整环境"),
    "medium": ("medium shot, waist up", "中景，半身构图"),
    "close-up": ("close-up, face focus", "近景特写，聚焦面部"),
    "extreme-close-up": ("extreme close-up, macro detail", "极致特写，放大细节"),
    "splash": ("splash illustration, dramatic composition, dynamic angle",
               "整页大画面，戏剧性构图，充满张力"),
}


def _store():
    # looked up per call: the singleton can be swapped (tests, AUTOMANGA_DATA_DIR)
    return _astore.asset_store


def is_tag_format(prompt_format: Optional[str]) -> bool:
    """Injection separator: danbooru-style prompts join with ', ', prose joins
    with '。'. Unspecified formats default to the tag style."""
    return (prompt_format or "danbooru") != "prose"


def ref_to_dataurl(ref: str) -> Optional[str]:
    """Load an image ref as a data URL. Only data: URLs and managed assets
    are accepted — an arbitrary filesystem path here would be an
    unauthenticated local-file read primitive. Blocking (file read)."""
    try:
        if ref.startswith("data:"):
            return ref
        if ref.startswith("/api/assets/file/"):
            path = _store().path_for(ref.rsplit("/", 1)[-1])
            if not path:
                return None
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
        return None
    except Exception:
        return None


def embed_png_parameters(img_bytes: bytes, meta: Dict[str, Any]) -> bytes:
    """Embed the generation recipe as PNG tEXt chunks so exported images
    stay self-describing outside the asset index: the SD-ecosystem
    "parameters" text block (prompt / Negative prompt / key: value lines)
    plus a machine-readable "automanga" JSON block. Non-PNG bytes come back
    unchanged."""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        if (img.format or "").upper() != "PNG":
            return img_bytes
        from PIL import PngImagePlugin
        prompt = meta.get("prompt") or ""
        negative = meta.get("negative_prompt") or ""
        settings_line = ", ".join(
            f"{k}: {v}" for k, v in meta.items()
            if v is not None and k not in ("prompt", "negative_prompt"))
        block = "\n".join(x for x in (prompt,
                                      f"Negative prompt: {negative}" if negative else None,
                                      settings_line) if x)
        info = PngImagePlugin.PngInfo()
        info.add_text("parameters", block)
        info.add_text("automanga", json.dumps(meta, ensure_ascii=False))
        buf = io.BytesIO()
        img.save(buf, format="PNG", pnginfo=info)
        return buf.getvalue()
    except Exception as e:
        logger.warning("png parameter embedding skipped: %s", e)
        return img_bytes


def bind_panel_image(project_id: str, panel_id: str, image_data: str,
                     meta: Optional[Dict[str, Any]] = None) -> str:
    """Set a panel's image under the project lock; returns the final ref (an
    asset URL, or the ref as given). A data URL is stored as an asset file
    first so project.json stays lean. An unknown project is left alone.
    Blocking: call it from async code through asyncio.to_thread."""
    try:
        with storage.edit_project(project_id) as project:
            if image_data.startswith("data:"):
                entry = _store().save_dataurl(image_data, project_id=project_id or None,
                                              kind="panel", meta=meta)
                image_data = _store().url_for(entry["asset_id"])
            for panel in project.get("panels", []):
                if panel.get("id") == panel_id:
                    panel["image"] = image_data
                    break
    except (InvalidProjectIdError, ProjectNotFoundError):
        pass   # unknown project: the image is returned as it came
    return image_data


def _resolve_loras(payload: Dict[str, Any]) -> Optional[List[LoraConfig]]:
    lora_payload = payload.get("loras")
    if lora_payload:
        return [LoraConfig(name=l.get("name", ""), weight=l.get("weight", 1.0))
                for l in lora_payload if l.get("name")]
    if settings.swarmui.currentLoras:
        # settings loras are LoraConfig models already (put_agent_settings
        # normalizes) — copy as-is, never dict-style .get() on them
        return [l for l in settings.swarmui.currentLoras if l.name]
    return None


def _store_variant(b64_image: str, *, remove_background: bool, png_meta: Dict[str, Any],
                   asset_meta: Dict[str, Any], project_id: Optional[str],
                   kind: str) -> Dict[str, Any]:
    """Decode one SwarmUI result, apply the alpha fallback, embed the recipe
    and save the bytes as an asset. Blocking (CPU + file I/O)."""
    img_bytes = base64.b64decode(b64_image.split(",", 1)[-1])
    alpha_method = None
    if remove_background:
        # transparent-background fallback when SwarmUI's remover is broken
        from ..render.alpha_fallback import ensure_alpha
        img_bytes, alpha_method = ensure_alpha(img_bytes)
    # (the key keeps its place in png_meta: the parameters line order is stable)
    img_bytes = embed_png_parameters(img_bytes, {**png_meta, "alpha_method": alpha_method})
    img = Image.open(io.BytesIO(img_bytes))
    has_alpha = img.mode == "RGBA" and img.getchannel("A").getextrema()[0] < 255
    ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get((img.format or "").upper(), "png")
    entry = _store().save_bytes(
        img_bytes, ext=ext, project_id=project_id or None, kind=kind,
        meta={**asset_meta, "alpha_method": alpha_method,
              "width": img.size[0], "height": img.size[1], "has_alpha": has_alpha})
    return {"asset_id": entry["asset_id"],
            "url": _store().url_for(entry["asset_id"]),
            "width": img.size[0], "height": img.size[1],
            "has_alpha": has_alpha, "alpha_method": alpha_method}


def _find_panel(project: Optional[Dict[str, Any]], panel_id: Optional[str]):
    if not project or not panel_id:
        return None
    return next((p for p in project.get("panels") or [] if p.get("id") == panel_id), None)


async def generate(payload: Dict[str, Any], *, prompt_blocks: bool = True,
                   strict_project: bool = True,
                   address: Optional[str] = None) -> Dict[str, Any]:
    """Generate ``payload["variants"]`` images and store them as assets.

    ``payload`` takes the /api/agent/generate fields (prompt, negative_prompt,
    project_id, panel_id, width/height/steps/cfg_scale/seed, model, loras,
    prompt_format, init_image/init_creativity/mask_image, remove_background,
    variants, apply_variant, apply_to_panel, kind, inject_characters).

    ``prompt_blocks``: inject the project style and the panel's location,
    shot and character prompt blocks (the Assets tab builds those into its
    prompt itself). The panel characters' LoRAs/trigger words, the
    IP-Adapter sheet and the content rating apply either way.
    ``strict_project``: an invalid project id, or binding to a panel that is
    not there, is a ValueError (HTTP 400); otherwise the image is generated
    without a project and not bound. ``address``: a one-shot SwarmUI
    address (already validated by the caller).

    Raises ValueError (incl. RatingRefused) for a bad request and
    GenerationError when SwarmUI fails. Returns ``{assets, applied, model,
    injected, content_rating, notice?}``.
    """
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    variants = max(1, min(4, int(payload.get("variants") or 1)))
    apply_variant = int(payload.get("apply_variant") or 0)
    if not (0 <= apply_variant < variants):
        raise ValueError(f"apply_variant must be 0-{variants - 1}")
    seed = payload.get("seed")
    sw = settings.swarmui
    tags = is_tag_format(payload.get("prompt_format"))
    sep = ", " if tags else "。"

    # per-request overrides fall back to global settings
    model = payload.get("model") or sw.currentModel or None
    loras = _resolve_loras(payload)
    steps = payload.get("steps") or sw.currentSteps
    cfg_scale = payload.get("cfg_scale") or sw.currentCfgScale
    width = payload.get("width") or sw.currentWidth
    height = payload.get("height") or sw.currentHeight
    remove_background = bool(payload.get("remove_background"))
    kind = payload.get("kind") or "panel"
    project_id = payload.get("project_id") or None
    panel_id = payload.get("panel_id") or None

    init_dataurl = mask_dataurl = None
    if payload.get("init_image"):
        init_dataurl = await asyncio.to_thread(ref_to_dataurl, payload["init_image"])
        if init_dataurl is None:
            raise ValueError(f"init_image not found: {payload['init_image']}")
    if payload.get("mask_image"):
        if not init_dataurl:
            raise ValueError("mask_image requires init_image")
        mask_dataurl = await asyncio.to_thread(ref_to_dataurl, payload["mask_image"])
        if mask_dataurl is None:
            raise ValueError(f"mask_image not found: {payload['mask_image']}")

    project = None
    if project_id:
        try:
            project = await asyncio.to_thread(storage.load_project, project_id)
        except InvalidProjectIdError:
            if strict_project:
                raise
    panel = _find_panel(project, panel_id)

    # automatic injection: project style suffix + character blocks + LoRAs
    # (+ a solo panel's character sheet as an IP-Adapter reference)
    injected: List[Dict[str, Any]] = []
    ref_kwargs: Dict[str, Any] = {}
    if payload.get("inject_characters", True) and project:
        from ..prompting import (apply_conditioning, blocks_for_panel, ipadapter_args,
                                 location_block_for_panel, reference_for_panel,
                                 style_suffix)
        if prompt_blocks:
            style = style_suffix(project)
            if style:
                prompt = f"{prompt}{sep}{style}"
                injected.append({"kind": "style", "text": style})
        if panel:
            if prompt_blocks:
                location = location_block_for_panel(project, panel)
                if location:
                    prompt = f"{prompt}{sep}{location}"
                    injected.append({"kind": "location", "text": location})
                shot = panel.get("shot")
                if shot in SHOT_TAGS:
                    extra = SHOT_TAGS[shot][0 if tags else 1]
                    prompt = f"{prompt}{sep}{extra}"
                    injected.append({"kind": "shot", "text": extra})
                for block in blocks_for_panel(project, panel):
                    prompt = f"{prompt}{sep}{block}"
                    injected.append({"kind": "character", "text": block})
            # the panel characters' trained LoRAs + trigger words, merged with the
            # request/global LoRAs (a panel character's weight wins)
            prompt, loras, lora_injected = apply_conditioning(prompt, loras, project, panel,
                                                              sep=sep)
            injected.extend(lora_injected)
            # a solo panel's character sheet guides it through IP-Adapter
            # (the character's refWeight > 0; the sheet itself is not echoed)
            ref_kwargs = ipadapter_args(reference_for_panel(project, panel))
            if ref_kwargs:
                ref_name = next(str(n).strip() for n in panel.get("characters") or []
                                if str(n).strip())
                injected.append({"kind": "reference", "name": ref_name,
                                 "weight": ref_kwargs["ipadapter_weight"],
                                 "preset": ref_kwargs["ipadapter"]})

    # the content rating (backend/content_rating.py): the project's rating
    # (else the global default) for this panel's cast; a mature prompt with
    # minor terms is refused (RatingRefused is a ValueError: HTTP 400)
    from ..content_rating import apply_rating
    rated = apply_rating(prompt, payload.get("negative_prompt") or "", project=project,
                         panel=panel, free_prompt=payload.get("prompt") or "", sep=sep)
    prompt, negative = rated["prompt"], rated["negative"]
    injected.append(rated["injected"])
    if rated["notice"]:
        logger.info("generate panel=%s: %s", panel_id, rated["notice"])

    # binding target checked before the (slow) SwarmUI call
    bind = bool(payload.get("apply_to_panel", True) and panel_id and project_id)
    if bind and panel is None:
        if strict_project:
            raise ValueError(f"panel {panel_id} not found in project {project_id}")
        bind = False   # lenient: generate, just don't bind

    ipadapter_meta = ({"preset": ref_kwargs["ipadapter"], "weight": ref_kwargs["ipadapter_weight"]}
                      if ref_kwargs else None)
    assets_out = []
    for v in range(variants):
        v_seed = (seed + v) if seed is not None else None
        result = await swarm_client.generate_image(
            prompt=prompt, negative_prompt=negative, model=model,
            steps=steps, cfg_scale=cfg_scale, width=width, height=height,
            sampler_name=sw.currentSamplerName, loras=loras, seed=v_seed,
            remove_background=remove_background,
            init_image=init_dataurl, init_creativity=payload.get("init_creativity"),
            mask_image=mask_dataurl, address=address, **ref_kwargs)
        if not result.get("success"):
            raise GenerationError(f"SwarmUI: {result.get('error', 'unknown error')}",
                                  rating=rated["rating"])
        assets_out.append(await asyncio.to_thread(
            _store_variant, result["image"], remove_background=remove_background,
            png_meta={"prompt": prompt, "negative_prompt": negative, "seed": v_seed,
                      "model": model, "steps": steps, "cfg_scale": cfg_scale,
                      "sampler": sw.currentSamplerName, "width": width, "height": height,
                      "prompt_format": payload.get("prompt_format"), "kind": kind,
                      "alpha_method": None, "ipadapter": ipadapter_meta, "content_rating": rated["rating"]},
            asset_meta={"prompt": prompt, "seed": v_seed,
                        "remove_background": remove_background, "model": model,
                        "prompt_format": payload.get("prompt_format"),
                        "negative_prompt": negative, "content_rating": rated["rating"],
                        "injected": injected},
            project_id=project_id, kind=kind))

    if bind:
        await asyncio.to_thread(bind_panel_image, project_id, panel_id,
                                assets_out[apply_variant]["url"])
    out = {"assets": assets_out, "applied": bind,
           "model": model, "injected": injected, "content_rating": rated["rating"]}
    if rated["notice"]:
        out["notice"] = rated["notice"]
    return out
