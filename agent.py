"""
OutboundAI — Agent Worker
Agent: Rahul | Voice: Fenrir | Language: Hinglish
Business: Professional Website Developer

HOW TO RUN (local testing):
  1. pip install -r requirements.txt
  2. cp .env.example .env  (fill in your credentials)
  3. python agent.py start

IMPORTANT: This must run as a persistent process.
For production: deploy to Railway.app / Fly.io / any VPS.
"""

import asyncio
import json
import logging
import os
import ssl
import certifi
from typing import Optional

from dotenv import load_dotenv

# ── Patch SSL with certifi FIRST ─────────────────────────────────────────────
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions
try:
    from livekit.agents import RoomOptions as _RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False

from livekit.plugins import noise_cancellation, silero

from db import init_db, log_error, get_enabled_tools
from prompts import build_prompt
from tools import AppointmentTools

load_dotenv(".env")
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger("rahul-agent")

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")

# ── Google plugin import ─────────────────────────────────────────────────────
_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None

try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
        logger.info("google.realtime.RealtimeModel loaded (stable)")
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
        logger.info("google.beta.realtime.RealtimeModel loaded (beta)")
    except AttributeError:
        pass
    try:
        _google_llm = _gp.LLM
        _google_tts = _gp.TTS
    except AttributeError:
        pass
except ImportError:
    logger.warning("livekit-plugins-google not installed — run: pip install livekit-plugins-google>=1.0.0")

_deepgram_stt = None
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
except ImportError:
    pass


def load_db_settings_to_env() -> None:
    """Load settings from Supabase into os.environ at startup."""
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return
    try:
        from supabase import create_client
        client = create_client(url, key)
        result = client.table("settings").select("key, value").execute()
        for row in (result.data or []):
            if row.get("value"):
                os.environ[row["key"]] = row["value"]
        logger.info("Settings loaded from Supabase")
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":    logger.info(msg)
    elif level == "warning": logger.warning(msg)
    else:                  logger.error(msg)
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


def _build_session(tools: list, system_prompt: str) -> AgentSession:
    """
    Build AgentSession with Gemini Live realtime.
    Uses all 3 silence-prevention configs (critical for natural calls).
    """
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    gemini_voice = os.getenv("GEMINI_TTS_VOICE", "Fenrir")  # Confident male voice
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"

    RealtimeClass = _google_realtime or (_google_beta_realtime if use_realtime else None)

    if use_realtime and RealtimeClass is not None:
        logger.info("MODE: Gemini Live realtime | model=%s | voice=%s", gemini_model, gemini_voice)

        _realtime_input_cfg = None
        _session_resumption_cfg = None
        _ctx_compression_cfg = None

        try:
            from google.genai import types as _gt
            # Rule 3: Must use END_SENSITIVITY_LOW (not .LOW — AttributeError!)
            _realtime_input_cfg = _gt.RealtimeInputConfig(
                automatic_activity_detection=_gt.AutomaticActivityDetection(
                    end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=2000,
                    prefix_padding_ms=200,
                ),
            )
            _session_resumption_cfg = _gt.SessionResumptionConfig(transparent=True)
            _ctx_compression_cfg = _gt.ContextWindowCompressionConfig(
                trigger_tokens=25600,
                sliding_window=_gt.SlidingWindow(target_tokens=12800),
            )
            logger.info("Silence-prevention configs applied (VAD LOW + transparent resumption + compression)")
        except Exception as cfg_err:
            logger.warning("Silence-prevention config failed (non-fatal): %s", cfg_err)

        realtime_kwargs = dict(
            model=gemini_model,
            voice=gemini_voice,
            instructions=system_prompt,
        )
        if _realtime_input_cfg:
            realtime_kwargs["realtime_input_config"] = _realtime_input_cfg
            realtime_kwargs["session_resumption"] = _session_resumption_cfg
            realtime_kwargs["context_window_compression"] = _ctx_compression_cfg

        return AgentSession(llm=RealtimeClass(**realtime_kwargs), tools=tools)

    # Pipeline fallback (Deepgram + Gemini LLM + Google TTS)
    if _google_llm is None:
        raise RuntimeError(
            "No Google AI backend. Install: pip install 'livekit-plugins-google>=1.0'"
        )
    logger.info("MODE: Pipeline (Deepgram STT + Gemini LLM + Google TTS)")
    stt = _deepgram_stt(model="nova-3", language="multi") if _deepgram_stt else None
    tts = _google_tts() if _google_tts else None
    return AgentSession(
        stt=stt,
        llm=_google_llm(model="gemini-2.0-flash"),
        tts=tts,
        vad=silero.VAD.load(),
        tools=tools,
    )


class RahulAgent(Agent):
    """Rahul — professional website developer, Hinglish outbound caller."""
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions)


async def entrypoint(ctx: agents.JobContext) -> None:
    """
    Main job entrypoint — called once per outbound call.
    CRITICAL: Dial-first pattern (wait_until_answered=True BEFORE session.start())
    """
    await _log("info", f"Job received — room: {ctx.room.name}")

    # Parse job metadata
    phone_number: Optional[str] = None
    lead_name = "aap"
    business_name = "aapka business"
    service_type = "website development"
    custom_prompt: Optional[str] = None
    voice_override: Optional[str] = None
    model_override: Optional[str] = None
    tools_override: Optional[str] = None

    if ctx.job.metadata:
        try:
            data = json.loads(ctx.job.metadata)
            phone_number   = data.get("phone_number")
            lead_name      = data.get("lead_name", lead_name)
            business_name  = data.get("business_name", business_name)
            service_type   = data.get("service_type", service_type)
            custom_prompt  = data.get("system_prompt")
            voice_override = data.get("voice_override")
            model_override = data.get("model_override")
            tools_override = data.get("tools_override")
        except (json.JSONDecodeError, AttributeError):
            await _log("warning", "Invalid JSON in job metadata")

    await _log("info", f"Calling {phone_number} | lead={lead_name}")

    # Build system prompt for Rahul
    system_prompt = build_prompt(
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
        custom_prompt=custom_prompt,
    )

    # Apply overrides
    if voice_override:
        os.environ["GEMINI_TTS_VOICE"] = voice_override
    if model_override:
        os.environ["GEMINI_MODEL"] = model_override

    if tools_override:
        try:
            enabled_tools = json.loads(tools_override)
        except Exception:
            enabled_tools = await get_enabled_tools()
    else:
        enabled_tools = await get_enabled_tools()

    # Tool context
    tool_ctx = AppointmentTools(ctx, phone_number, lead_name)

    # ── STEP 1: Connect to LiveKit room ─────────────────────────────────────
    await ctx.connect()
    await _log("info", f"Connected to room: {ctx.room.name}")

    # ── STEP 2: DIAL FIRST (before session.start!) ──────────────────────────
    # Rule 1: wait_until_answered=True blocks until call is answered.
    # Starting session before this causes Gemini to timeout during ring time.
    if phone_number:
        trunk_id = os.getenv("OUTBOUND_TRUNK_ID", "")
        if not trunk_id:
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot dial. Set it in .env or dashboard Settings.")
            ctx.shutdown()
            return

        await _log("info", f"Dialing {phone_number} via trunk {trunk_id}...")
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,   # BLOCKS until answered
                )
            )
        except Exception as exc:
            await _log("error", f"SIP dial failed for {phone_number}: {exc}")
            ctx.shutdown()
            return

        await _log("info", f"ANSWERED — {phone_number} picked up. Starting AI now.")

    # ── STEP 3: Build + start Gemini Live session ────────────────────────────
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    await _log("info", f"Building AI session | model={gemini_model}")

    active_tools = tool_ctx.build_tool_list(enabled_tools)
    await _log("info", f"Tools: {[t.__name__ for t in active_tools]}")

    session = _build_session(tools=active_tools, system_prompt=system_prompt)

    # ── STEP 4: Start session with noise cancellation ────────────────────────
    # Rule 2: NEVER close_on_disconnect=True — SIP dropouts kill session
    if _HAS_ROOM_OPTIONS:
        from livekit.agents import RoomOptions as _RO
        await session.start(
            room=ctx.room,
            agent=RahulAgent(instructions=system_prompt),
            room_options=_RO(input_options=RoomInputOptions(
                noise_cancellation=noise_cancellation.BVCTelephony()
            )),
        )
    else:
        await session.start(
            room=ctx.room,
            agent=RahulAgent(instructions=system_prompt),
            room_input_options=RoomInputOptions(
                noise_cancellation=noise_cancellation.BVCTelephony()
            ),
        )

    await _log("info", "Rahul agent session started — generating Hinglish greeting")

    # ── STEP 5: Greeting ────────────────────────────────────────────────────
    # Rule 4: Gemini 3.1/2.5 native-audio models speak autonomously from prompt.
    # generate_reply() is BLOCKED for these models — skip it.
    active_model = os.getenv("GEMINI_MODEL", "")
    if "3.1" in active_model or "2.5" in active_model:
        await _log("info", "Native audio model — Rahul will greet autonomously from prompt")
    else:
        greeting = (
            f"Call connected. Greet the lead in Hinglish and ask if you're speaking with {lead_name}."
            if phone_number else "Greet the caller warmly in Hinglish."
        )
        try:
            await session.generate_reply(instructions=greeting)
        except Exception as gr_exc:
            await _log("warning", f"generate_reply skipped: {gr_exc}")

    # ── STEP 6: Keep alive until SIP participant disconnects ─────────────────
    # Watch the specific SIP participant identity, not the whole room.
    if phone_number:
        sip_identity = f"sip_{phone_number}"
        _disconnect_event = asyncio.Event()

        def _on_participant_disconnected(participant: rtc.RemoteParticipant):
            if participant.identity == sip_identity:
                _disconnect_event.set()

        def _on_room_disconnected():
            _disconnect_event.set()

        ctx.room.on("participant_disconnected", _on_participant_disconnected)
        ctx.room.on("disconnected", _on_room_disconnected)

        try:
            await asyncio.wait_for(_disconnect_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            await _log("warning", "Call hit 1-hour safety timeout")

        await _log("info", f"SIP participant disconnected — cleaning up for {phone_number}")
        await session.aclose()
    else:
        _done = asyncio.Event()
        ctx.room.on("disconnected", lambda: _done.set())
        try:
            await asyncio.wait_for(_done.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    print("=" * 60)
    print("  OutboundAI — Rahul Agent Worker")
    print("  Voice: Fenrir | Language: Hinglish")
    print("  Model: gemini-3.1-flash-live-preview")
    print("=" * 60)
    init_db()
    load_db_settings_to_env()
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",  # MUST match dashboard dispatch
        )
    )
