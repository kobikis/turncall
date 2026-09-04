/**
 * TurnCall WebRTC Client
 *
 * Uses @pipecat-ai/client-js PipecatClient with SmallWebRTCTransport
 * to connect to TurnCall's /v1/webrtc/connect endpoint.
 */

import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";

let client = null;

function setStatus(msg, className = "") {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = className;
}

window.start = async function () {
  const serverUrl = document.getElementById("serverUrl").value;
  const apiKey = document.getElementById("apiKey").value;
  const assistantId = document.getElementById("assistantId").value;

  if (!apiKey || !assistantId) {
    setStatus("Please fill in API key and Agent ID", "error");
    return;
  }

  document.getElementById("startBtn").disabled = true;
  setStatus("Connecting...");

  try {
    const transport = new SmallWebRTCTransport({
      iceServers: [
        { urls: "stun:stun.l.google.com:19302" },
        { urls: "stun:stun1.l.google.com:19302" },
      ],
    });

    client = new PipecatClient({
      transport,
      enableMic: true,
      enableCam: false,
      callbacks: {
        onConnected: () => {
          setStatus("Connected! Speak now.", "connected");
          document.getElementById("stopBtn").disabled = false;
        },
        onDisconnected: () => {
          setStatus("Disconnected");
          cleanup();
        },
        onTransportStateChanged: (state) => {
          console.log("Transport state:", state);
          if (state === "error") {
            setStatus("Connection error", "error");
            cleanup();
          }
        },
        onBotReady: () => {
          setStatus("Bot ready! Speak now.", "connected");
        },
        onTrackStarted: (track, participant) => {
          if (participant?.local) return;
          if (track.kind === "audio") {
            const audio = new Audio();
            audio.srcObject = new MediaStream([track]);
            audio.play().catch((e) => console.warn("Audio play:", e));
          } else if (track.kind === "video") {
            // Avatar video (HeyGen). No-op for audio-only agents.
            const video = document.getElementById("avatar");
            video.srcObject = new MediaStream([track]);
            video.style.display = "block";
            video.play().catch((e) => console.warn("Video play:", e));
          }
        },
      },
    });

    // Connect using the SmallWebRTC transport with our server endpoint
    await client.connect({
      webrtcRequestParams: {
        endpoint: `${serverUrl}/v1/webrtc/connect`,
        headers: new Headers({
          Authorization: `Bearer ${apiKey}`,
          "Content-Type": "application/json",
        }),
        requestData: {
          agent_id: assistantId,
        },
      },
    });
  } catch (e) {
    console.error("Connection error:", e);
    setStatus(`Error: ${e.message}`, "error");
    cleanup();
  }
};

window.stop = async function () {
  try {
    if (client) {
      await client.disconnect();
    }
  } catch (e) {
    console.warn("Disconnect error:", e);
  }
  cleanup();
  setStatus("Call ended");
};

function cleanup() {
  client = null;
  document.getElementById("startBtn").disabled = false;
  document.getElementById("stopBtn").disabled = true;
}
