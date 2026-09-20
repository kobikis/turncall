"""Automated behavioural testing for agents (#68).

The engine is `pipecat.evals`, already a dependency. Only the transport is
swapped, so a run exercises the real STT/LLM/TTS construction, the real VAD and
smart-turn wiring, the real tool bridge and knowledge retrieval — which is
where the regressions have actually lived.
"""
