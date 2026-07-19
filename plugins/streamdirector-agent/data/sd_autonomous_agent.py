"""
StreamDirector Autonomous Agent - Hybrid approach.

Rule-based reactive monitoring (instant) + LLM periodic decisions (every N seconds).

Reactive rules handle:
- Audio volume spikes (auto-lower if clipping)
- Dropped frames (alert + suggest fix)
- Mic silence detection (alert)

LLM handles:
- Periodic "should I switch scenes?" decisions
- Novel situations the rules don't cover
"""

import os
import sys
import time
import json
import math
import threading

_streamdirector = None
api = None

try:
    import _streamdirector as _sd
    _streamdirector = _sd
except ImportError:
    pass

# Thresholds
PEAK_CLIP_DB = -3.0          # Above this = clipping, auto-lower
PEAK_LOUD_DB = -6.0          # Above this = loud
MIC_SILENCE_DB = -50.0       # Below this = mic is silent
FRAME_DROP_PCT_WARN = 5.0    # Warn at 5% dropped frames
FRAME_DROP_PCT_CRIT = 10.0   # Critical at 10%

LLM_CHECK_INTERVAL = 30      # seconds between LLM decisions
REACTIVE_CHECK_INTERVAL = 2  # seconds between reactive checks
VOLUME_ADJUST_STEP = 3.0     # dB to lower when clipping


def _init_api():
    global api
    if api is not None:
        return
    try:
        from sd_direct_api import DirectOBSAPI
        api = DirectOBSAPI()
    except Exception:
        pass


def _get_audio_levels():
    """Get current audio levels from the C++ API."""
    try:
        return _streamdirector.get_audio_levels()
    except Exception:
        return []


def _get_streaming_stats():
    """Get current streaming stats."""
    try:
        return _streamdirector.get_stats()
    except Exception:
        return {}


def _get_obs_state():
    """Get current OBS state summary."""
    try:
        streaming = _streamdirector.is_streaming()
        recording = _streamdirector.is_recording()
        current_scene = _streamdirector.get_current_scene()
        scenes = _streamdirector.get_scene_names()
        return {
            "streaming": streaming,
            "recording": recording,
            "current_scene": current_scene,
            "scenes": list(scenes) if scenes else [],
        }
    except Exception as e:
        return {"error": str(e)}


def _reactive_check(state, actions_taken):
    """
    Run reactive rules on current state. Returns list of actions taken.
    This runs every 2 seconds and handles instant responses.
    """
    actions = []

    # Check audio levels for clipping
    levels = _get_audio_levels()
    for src in levels:
        name = src.get("name", "")
        peak = src.get("peak_db", -100)
        input_peak = src.get("input_peak_db", -100)

        # Auto-lower volume if clipping
        if peak > PEAK_CLIP_DB:
            try:
                vol_info = _streamdirector.get_source_volume(name)
                if vol_info:
                    current_vol = vol_info.get("volume", 1.0)
                    # Convert to dB, lower, convert back
                    current_db = _linear_to_db(current_vol)
                    new_db = current_db - VOLUME_ADJUST_STEP
                    new_vol = _db_to_linear(new_db)
                    _streamdirector.set_source_volume(name, new_vol)
                    actions.append(f"Auto-lowered {name} volume by {VOLUME_ADJUST_STEP}dB (peak was {peak:.1f}dB)")
            except Exception:
                pass

        # Alert on mic silence during streaming
        if state.get("streaming") and "mic" in name.lower():
            if input_peak < MIC_SILENCE_DB:
                actions.append(f"Warning: {name} appears silent (input peak {input_peak:.1f}dB)")

    # Check frame drops
    stats = _get_streaming_stats()
    total = stats.get("streaming_total_frames", 0)
    dropped = stats.get("streaming_skipped_frames", 0)
    if total > 100:
        drop_pct = (dropped / total) * 100
        if drop_pct > FRAME_DROP_PCT_CRIT:
            actions.append(f"CRITICAL: {drop_pct:.1f}% frames dropped ({dropped}/{total})")
        elif drop_pct > FRAME_DROP_PCT_WARN:
            actions.append(f"Warning: {drop_pct:.1f}% frames dropped ({dropped}/{total})")

    return actions


def _build_llm_prompt(state, levels, stats):
    """Build a prompt for the LLM to make a scene/strategy decision."""
    scene_list = state.get("scenes", [])
    current = state.get("current_scene", "unknown")
    streaming = state.get("streaming", False)

    audio_info = []
    for src in levels:
        name = src.get("name", "")
        peak = src.get("peak_db", -100)
        mag = src.get("magnitude_db", -100)
        audio_info.append(f"  {name}: peak={peak:.1f}dB, avg={mag:.1f}dB")

    total = stats.get("streaming_total_frames", 0)
    dropped = stats.get("streaming_skipped_frames", 0)
    drop_pct = (dropped / total * 100) if total > 0 else 0

    prompt = f"""Current stream state:
  Streaming: {streaming}
  Current scene: {current}
  Available scenes: {', '.join(scene_list)}
  Frame drops: {drop_pct:.1f}% ({dropped}/{total})

Audio levels:
{chr(10).join(audio_info) if audio_info else '  No audio sources'}

Based on the above, should I switch scenes or take any action?
Respond with ONLY JSON: {{"action": "switch_scene"|"adjust_volume"|"none", "scene": "name", "reason": "brief"}}

Examples:
- Desktop audio is loud, mic is quiet -> {{"action": "none", "reason": "gaming scene is fine"}}
- Mic is active, desktop is silent -> {{"action": "switch_scene", "scene": "Just Chatting", "reason": "talking only"}}
- No action needed -> {{"action": "none", "reason": "all good"}}
"""
    return prompt


def _llm_decision(state, levels, stats, server_url):
    """Ask the LLM for a periodic decision."""
    try:
        from sd_bitnet_agent import _call_llm
        prompt = _build_llm_prompt(state, levels, stats)
        system = "You are a stream director AI. Make brief decisions about scene switching based on audio activity. Respond with ONLY JSON."
        response = _call_llm(prompt, system, server_url, temperature=0.3)
        return response
    except Exception as e:
        return f"LLM_ERROR: {e}"


def _db_to_linear(db):
    if db <= -100.0:
        return 0.0
    return 10.0 ** (db / 20.0)


def _linear_to_db(linear):
    if linear <= 0.0:
        return -100.0
    return 20.0 * math.log10(linear)


def run_autonomous_agent(server_url=None, stop_event=None, log_func=None):
    """
    Run the autonomous agent loop. Blocks until stop_event is set.

    Args:
        server_url: BitNet inference server URL
        stop_event: threading.Event to signal stop
        log_func: function to call for logging (defaults to _streamdirector.log)
    """
    if _streamdirector is None:
        return "Error: _streamdirector module not available"

    if server_url is None:
        server_url = os.environ.get("BITNET_SERVER_URL", "http://127.0.0.1:8080")

    if stop_event is None:
        stop_event = threading.Event()

    if log_func is None:
        log_func = _streamdirector.log

    _init_api()

    log_func("[autonomous] Starting autonomous stream director...")
    log_func(f"[autonomous] Reactive checks: every {REACTIVE_CHECK_INTERVAL}s")
    log_func(f"[autonomous] LLM decisions: every {LLM_CHECK_INTERVAL}s")
    log_func(f"[autonomous] Server: {server_url}")

    last_llm_check = 0
    prev_total_frames = 0
    prev_dropped_frames = 0

    while not stop_event.is_set():
        state = _get_obs_state()

        # Reactive checks (every 2 seconds)
        actions = _reactive_check(state, [])
        for action in actions:
            log_func(f"[autonomous] {action}")

        # LLM periodic decision (every 30 seconds)
        now = time.time()
        if now - last_llm_check > LLM_CHECK_INTERVAL:
            last_llm_check = now
            levels = _get_audio_levels()
            stats = _get_streaming_stats()

            log_func("[autonomous] Asking LLM for scene decision...")
            decision = _llm_decision(state, levels, stats, server_url)
            log_func(f"[autonomous] LLM decision: {decision}")

            # Try to parse and execute the decision
            try:
                # Extract JSON from response
                import re
                match = re.search(r'\{.*?"action".*?\}', decision, re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
                    action = data.get("action", "none")
                    scene = data.get("scene", "")
                    reason = data.get("reason", "")

                    if action == "switch_scene" and scene:
                        scenes = state.get("scenes", [])
                        if scene in scenes:
                            _streamdirector.set_current_scene(scene)
                            log_func(f"[autonomous] Switched to '{scene}' - {reason}")
                        else:
                            log_func(f"[autonomous] Scene '{scene}' not found (available: {scenes})")
                    elif action == "none":
                        log_func(f"[autonomous] No action needed - {reason}")
            except Exception as e:
                log_func(f"[autonomous] Could not parse LLM decision: {e}")

        # Wait for next check
        stop_event.wait(REACTIVE_CHECK_INTERVAL)

    log_func("[autonomous] Stopped.")
    return "Autonomous agent stopped."
