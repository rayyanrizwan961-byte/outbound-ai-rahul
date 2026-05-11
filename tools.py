"""OutboundAI — All 9 LLM function tools for Rahul agent."""
import asyncio, logging, os, time
from typing import Optional
from livekit import agents, api
from livekit.agents import llm
from db import (check_slot, get_next_available, insert_appointment, log_call, log_error,
                get_calls_by_phone, get_appointments_by_phone,
                add_contact_memory, get_contact_memory, compress_contact_memory)

logger = logging.getLogger("rahul-tools")

class AppointmentTools(llm.ToolContext):
    """All tools available to Rahul."""
    def __init__(self, ctx: agents.JobContext, phone_number: Optional[str]=None,
                 lead_name: Optional[str]=None):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        self._call_start_time = time.time()
        self._sip_domain = os.getenv("VOBIZ_SIP_DOMAIN", "")
        self.recording_url: Optional[str] = None
        super().__init__(tools=[])

    def build_tool_list(self, enabled: list) -> list:
        all_methods = [
            self.check_availability, self.book_appointment, self.end_call,
            self.transfer_to_human, self.send_sms_confirmation, self.lookup_contact,
            self.remember_details, self.book_calcom, self.cancel_calcom,
        ]
        if not enabled: return all_methods
        name_map = {m.__name__: m for m in all_methods}
        return [name_map[n] for n in enabled if n in name_map]

    @llm.function_tool
    async def check_availability(self, date: str, time: str) -> str:
        """Check if a date/time slot is available. ALWAYS call before confirming.
        date: YYYY-MM-DD | time: HH:MM (24-hour)"""
        try:
            if await check_slot(date, time): return "available"
            next_slot = await get_next_available(date, time)
            return f"unavailable: next available is {next_slot}"
        except Exception:
            return "Unable to check availability — suggest a date and I will confirm."

    @llm.function_tool
    async def book_appointment(self, name: str, phone: str, date: str, time: str, service: str) -> str:
        """Book appointment ONLY after verbal confirmation. name | phone (with +91) | date: YYYY-MM-DD | time: HH:MM | service type"""
        try:
            booking_id = await insert_appointment(name, phone, date, time, service)
            return f"Booked! ID: {booking_id}. Meeting on {date} at {time}."
        except Exception:
            return "Technical issue saving booking — our team will confirm shortly."

    @llm.function_tool
    async def end_call(self, outcome: str, reason: str="") -> str:
        """ALWAYS call this when ending a call. outcome: booked|not_interested|wrong_number|voicemail|no_answer|callback_requested"""
        duration = int(time.time() - self._call_start_time)
        try:
            await log_call(phone_number=self.phone_number or "unknown",
                           lead_name=self.lead_name, outcome=outcome, reason=reason,
                           duration_seconds=duration, recording_url=self.recording_url)
        except Exception as exc:
            logger.error("log_call failed: %s", exc)
        try:
            await self.ctx.room.disconnect()
        except Exception:
            pass
        return "Call ended."

    @llm.function_tool
    async def transfer_to_human(self, reason: str) -> str:
        """Transfer call to human agent via SIP REFER. reason: why transferring"""
        destination = os.getenv("DEFAULT_TRANSFER_NUMBER", "")
        if not destination: return "Transfer unavailable — no fallback number configured."
        if "@" not in destination:
            clean = destination.replace("tel:", "").replace("sip:", "")
            destination = f"sip:{clean}@{self._sip_domain}" if self._sip_domain else f"tel:{clean}"
        elif not destination.startswith("sip:"):
            destination = f"sip:{destination}"
        participant_identity = f"sip_{self.phone_number}" if self.phone_number else None
        if not participant_identity:
            for p in self.ctx.room.remote_participants.values():
                participant_identity = p.identity
                break
        if not participant_identity: return "Transfer failed: could not identify caller."
        try:
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=destination, play_dialtone=False,
                ))
            return "Transferring you to a human agent now. Please hold."
        except Exception:
            return "Transfer failed. Please call us back directly."

    @llm.function_tool
    async def send_sms_confirmation(self, phone: str, message: str) -> str:
        """Send SMS after booking. Skips if Twilio not configured. phone | message"""
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_num = os.getenv("TWILIO_FROM_NUMBER", "")
        if not (sid and token and from_num): return "SMS skipped (Twilio not configured)."
        try:
            from twilio.rest import Client
            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(None, lambda: client.messages.create(
                body=message, from_=from_num, to=phone))
            return f"SMS sent to {phone}."
        except Exception:
            return "SMS delivery failed — booking is still confirmed."

    @llm.function_tool
    async def lookup_contact(self, phone: str) -> str:
        """Look up contact history. Call at START of every call BEFORE engaging. phone: with country code"""
        try:
            calls = await get_calls_by_phone(phone)
            appointments = await get_appointments_by_phone(phone)
            memories = await get_contact_memory(phone)
            if not calls and not appointments and not memories:
                return f"No history for {phone}. First-time contact."
            lines = [f"History for {phone}:"]
            if memories:
                lines.append(f"NOTES ({len(memories)}):")
                for m in memories[:10]: lines.append(f"  - {m['insight']}")
            if calls:
                lines.append(f"CALLS ({len(calls)}):")
                for c in calls[:5]:
                    ts = (c.get("timestamp") or "")[:16]
                    lines.append(f"  - {ts}: {c.get('outcome','?')} — {c.get('reason','')}")
            if appointments:
                lines.append(f"APPOINTMENTS ({len(appointments)}):")
                for a in appointments[:3]:
                    lines.append(f"  - {a.get('date')} {a.get('time')}: {a.get('service')} [{a.get('status')}]")
            return "\n".join(lines)
        except Exception:
            return "Could not load contact history."

    @llm.function_tool
    async def remember_details(self, insight: str) -> str:
        """Store useful info about this lead for future calls. Examples: preferences, objections, best time to call. insight: what to remember"""
        if not self.phone_number: return "Cannot remember — no phone number."
        try:
            await add_contact_memory(self.phone_number, insight)
            memories = await get_contact_memory(self.phone_number)
            if len(memories) >= 5:
                asyncio.create_task(self._compress_memories())
            return f"Remembered: {insight}"
        except Exception:
            return "Could not save detail."

    async def _compress_memories(self) -> None:
        try:
            memories = await get_contact_memory(self.phone_number)
            if len(memories) < 5: return
            import google.generativeai as genai
            api_key = os.getenv("GOOGLE_API_KEY", "")
            if not api_key: return
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemini-2.0-flash")
            bullets = "\n".join(f"- {m['insight']}" for m in memories)
            prompt = f"Compress these contact notes into 3-5 concise bullets. Keep all key facts.\n\n{bullets}"
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: model.generate_content(prompt))
            if response.text.strip():
                await compress_contact_memory(self.phone_number, response.text.strip())
        except Exception as exc:
            logger.warning("Memory compression failed: %s", exc)

    @llm.function_tool
    async def book_calcom(self, name: str, email: str, date: str, start_time: str, notes: str="") -> str:
        """Book Cal.com calendar slot after booking. name | email | date: YYYY-MM-DD | start_time: HH:MM"""
        api_key = os.getenv("CALCOM_API_KEY", "")
        event_type_id = os.getenv("CALCOM_EVENT_TYPE_ID", "")
        timezone = os.getenv("CALCOM_TIMEZONE", "Asia/Kolkata")
        if not api_key or not event_type_id:
            return "Cal.com not configured — add CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID."
        try:
            from datetime import datetime as _dt
            import httpx
            start_dt = _dt.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.cal.com/v1/bookings",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"eventTypeId": int(event_type_id), "start": start_iso, "timeZone": timezone,
                          "responses": {"name": name, "email": email, "notes": notes},
                          "metadata": {"source": "OutboundAI-Rahul"}})
            data = resp.json()
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("message") or str(data))
            return f"Cal.com booked. UID: {data.get('uid', '')}"
        except Exception as exc:
            return f"Cal.com booking failed: {exc}"

    @llm.function_tool
    async def cancel_calcom(self, booking_uid: str, reason: str="") -> str:
        """Cancel a Cal.com booking. booking_uid: from book_calcom"""
        api_key = os.getenv("CALCOM_API_KEY", "")
        if not api_key: return "Cal.com not configured."
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.delete(
                    f"https://api.cal.com/v1/bookings/{booking_uid}",
                    headers={"Authorization": f"Bearer {api_key}"},
                    params={"reason": reason} if reason else {})
            if resp.status_code not in (200, 204):
                raise ValueError(f"HTTP {resp.status_code}")
            return f"Cancelled Cal.com booking {booking_uid}."
        except Exception as exc:
            return f"Cancellation failed: {exc}"
