"""WebRTC signaling API schemas."""

from uuid import UUID

from pydantic import BaseModel, Field


class CreateWebRTCSessionRequest(BaseModel):
    agent_id: UUID
    sdp_offer: str = Field(..., description="SDP offer from browser RTCPeerConnection")
