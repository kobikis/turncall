# TurnCall WebRTC Client

Browser-based voice agent using Pipecat's official JS client with SmallWebRTC transport.

## Setup

```bash
cd examples/webrtc-client
npm install
npm run dev
```

Opens at http://localhost:5174

## Usage

1. Make sure TurnCall server is running (`make run`)
2. Fill in:
   - **Server URL**: `http://localhost:8090`
   - **API Key**: your `tc_...` key
   - **Agent ID**: UUID from the setup script
3. Click **Start Call**
4. Speak to the agent

## How it works

```
Browser (Pipecat JS Client)
  -> SmallWebRTCTransport creates RTCPeerConnection + data channel
  -> POST /v1/webrtc/connect with SDP offer + agent_id in requestData
  -> Server creates Pipecat pipeline via SmallWebRTCRequestHandler
  -> Returns SDP answer
  -> PATCH /v1/webrtc/connect for ICE candidate trickle
  -> WebRTC peer connection established
  -> Audio flows via WebRTC (16kHz)
  -> Data channel carries Pipecat protocol messages
```

## Quick run

```bash
./run.sh
```
