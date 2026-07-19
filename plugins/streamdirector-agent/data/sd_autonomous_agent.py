"""
StreamDirector Autonomous Agent - Hybrid approach.

Supports two modes:
1. A/B Scene Switching: Monitor two audio sources, auto-switch between two scenes
   based on which source has more activity. Works best with Studio Mode (both
   scenes' sources are active in the mixer).

2. Reactive monitoring: Auto-lower clipping audio, alert on frame drops/mic silence.
   LLM periodic decisions for novel situations.

Configuration via instruction text:
  "switch between Gaming and Just Chatting based on Desktop Audio vs Mic/Aux"
  "switch between Scene and Scene 2 based on Desktop Audio vs Mic/Aux"

Or simple start:
  "start" — just reactive monitoring + LLM decisions
"""

import os
import sys
import time
import json
import math
import re
import threading

_streamdirector = None
api = None

try:
    import _streamdirector as _sd
    _streamdirector = _sd
except ImportError:
    pass

# Thresholds
PEAK_CLIP_DB = -3.0
PEAK_LOUD_DB = -6.0
MIC_SILENCE_DB = -50.0
FRAME_DROP_PCT_WARN = 5.0
FRAME_DROP_PCT_CRIT = 10.0

LLM_CHECK_INTERVAL = 30
REACTIVE_CHECK_INTERVAL = 2
VOLUME_ADJUST_STEP = 3.0

# A/B switching thresholds
AB_SWITCH_HYSTERESIS_DB = 6.0
AB_SWITCH_SUSTAIN_SECS = 3.0
AB_MIN_LEVEL_DB = -40.0


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
    try:
        return _streamdirector.get_audio_levels()
    except Exception:
        return []


def _get_streaming_stats():
    try:
        return _streamdirector.get_stats()
    except Exception:
        return {}


def _get_obs_state():
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


def _parse_ab_config(instruction):
    """
    Parse A/B scene switching configuration from instruction text.
    Expected: "switch between <SceneA> and <SceneB> based on <SourceA> vs <SourceB>"
    """
    pattern = r'switch between (.+?) and (.+?) based on (.+?) vs (.+)'
    match = re.search(pattern, instruction, re.IGNORECASE)
    if match:
        return {
            "scene_a": match.group(1).strip(),
            "scene_b": match.group(2).strip(),
            "source_a": match.group(3).strip(),
            "source_b": match.group(4).strip(),
        }

    pattern2 = r'switch between (.+?) and (.+?) (?:on|when) (.+?) vs (.+)'
    match = re.search(pattern2, instruction, re.IGNORECASE)
    if match:
        return {
            "scene_a": match.group(1).strip(),
            "scene_b": match.group(2).strip(),
            "source_a": match.group(3).strip(),
            "source_b": match.group(4).strip(),
        }

    return None


def _find_source_level(levels, source_name):
    """Find a source's magnitude (RMS) level from the audio levels list."""
    source_lower = source_name.lower()
    for src in levels:
        if src.get("name", "").lower() == source_lower:
            return src.get("magnitude_db", -100)
    for src in levels:
        if source_lower in src.get("name", "").lower():
            return src.get("magnitude_db", -100)
    return -100


def _reactive_check(state, actions_taken):
    """Run reactive rules on current state."""
    actions = []

    levels = _get_audio_levels()
    for src in levels:
        name = src.get("name", "")
        peak = src.get("peak_db", -100)
        input_peak = src.get("input_peak_db", -100)

        if peak > PEAK_CLIP_DB:
            try:
                vol_info = _streamdirector.get_source_volume(name)
                if vol_info:
                    current_vol = vol_info.get("volume", 1.0)
                    current_db = _linear_to_db(current_vol)
                    new_db = current_db - VOLUME_ADJUST_STEP
                    new_vol = _db_to_linear(new_db)
                    _streamdirector.set_source_volume(name, new_vol)
                    actions.append(f"Auto-lowered {name} by {VOLUME_ADJUST_STEP}dB (peak {peak:.1f}dB)")
            except Exception:
                pass

        if state.get("streaming") and "mic" in name.lower():
            if input_peak < MIC_SILENCE_DB:
                actions.append(f"Warning: {name} silent (input {input_peak:.1f}dB)")

    stats = _get_streaming_stats()
    total = stats.get("streaming_total_frames", 0)
    dropped = stats.get("streaming_skipped_frames", 0)
    if total > 100:
        drop_pct = (dropped / total) * 100
        if drop_pct > FRAME_DROP_PCT_CRIT:
            actions.append(f"CRITICAL: {drop_pct:.1f}% frames dropped")
        elif drop_pct > FRAME_DROP_PCT_WARN:
            actions.append(f"Warning: {drop_pct:.1f}% frames dropped")

    return actions


def _build_llm_prompt(state, levels, stats):
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
  Frame drops: {drop_pct:.1f}%

Audio levels:
{chr(10).join(audio_info) if audio_info else '  No audio sources'}

Should I switch scenes? Respond with ONLY JSON:
{{"action": "switch_scene"|"none", "scene": "name", "reason": "brief"}}

Examples:
- Desktop loud, mic quiet -> {{"action": "none", "reason": "gaming scene fine"}}
- Mic active, desktop silent -> {{"action": "switch_scene", "scene": "Just Chatting", "reason": "talking"}}
- All good -> {{"action": "none", "reason": "all good"}}
"""
    return prompt


def _llm_decision(state, levels, stats, server_url):
    try:
        from sd_bitnet_agent import _call_llm
        prompt = _build_llm_prompt(state, levels, stats)
        system = "You are a stream director AI. Make brief scene switching decisions based on audio activity. Respond with ONLY JSON."
        return _call_llm(prompt, system, server_url, temperature=0.3)
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


def _run_ab_switching(config, stop_event, log_func, server_url):
    """Run A/B scene switching loop based on audio source comparison."""
    scene_a = config["scene_a"]
    scene_b = config["scene_b"]
    source_a = config["source_a"]
    source_b = config["source_b"]

    log_func(f"[autonomous] A/B Scene Switching mode")
    log_func(f"[autonomous] Scene A: '{scene_a}' (monitor: '{source_a}')")
    log_func(f"[autonomous] Scene B: '{scene_b}' (monitor: '{source_b}')")
    log_func(f"[autonomous] Threshold: {AB_SWITCH_HYSTERESIS_DB}dB for {AB_SWITCH_SUSTAIN_SECS}s")

    try:
        studio = _streamdirector.is_studio_mode()
        if studio:
            preview = _streamdirector.get_preview_scene()
            log_func(f"[autonomous] Studio Mode ON (preview: '{preview}')")
        else:
            log_func(f"[autonomous] Studio Mode OFF - enable for dual-scene audio monitoring")
    except Exception:
        pass

    scenes = _streamdirector.get_scene_names()
    scene_list = list(scenes) if scenes else []
    if scene_a not in scene_list:
        log_func(f"[autonomous] ERROR: Scene '{scene_a}' not found! Available: {scene_list}")
        return f"Scene '{scene_a}' not found"
    if scene_b not in scene_list:
        log_func(f"[autonomous] ERROR: Scene '{scene_b}' not found! Available: {scene_list}")
        return f"Scene '{scene_b}' not found"

    audio_sources = _streamdirector.get_audio_sources()
    audio_list = list(audio_sources) if audio_sources else []
    log_func(f"[autonomous] Audio sources: {audio_list}")

    a_dominant_since = None
    b_dominant_since = None
    switch_count = 0

    while not stop_event.is_set():
        levels = _get_audio_levels()
        level_a = _find_source_level(levels, source_a)
        level_b = _find_source_level(levels, source_b)

        now = time.time()
        current_scene = _streamdirector.get_current_scene()

        state = _get_obs_state()
        actions = _reactive_check(state, [])
        for action in actions:
            log_func(f"[autonomous] {action}")

        log_func(f"[autonomous] {source_a}={level_a:.1f}dB  {source_b}={level_b:.1f}dB  scene={current_scene}")

        if level_a > level_b + AB_SWITCH_HYSTERESIS_DB and level_a > AB_MIN_LEVEL_DB:
            if a_dominant_since is None:
                a_dominant_since = now
            b_dominant_since = None

            if now - a_dominant_since >= AB_SWITCH_SUSTAIN_SECS and current_scene != scene_a:
                log_func(f"[autonomous] >>> Switching to '{scene_a}' ({source_a} {level_a:.1f}dB > {source_b} {level_b:.1f}dB)")
                _streamdirector.set_current_scene(scene_a)
                switch_count += 1

        elif level_b > level_a + AB_SWITCH_HYSTERESIS_DB and level_b > AB_MIN_LEVEL_DB:
            if b_dominant_since is None:
                b_dominant_since = now
            a_dominant_since = None

            if now - b_dominant_since >= AB_SWITCH_SUSTAIN_SECS and current_scene != scene_b:
                log_func(f"[autonomous] >>> Switching to '{scene_b}' ({source_b} {level_b:.1f}dB > {source_a} {level_a:.1f}dB)")
                _streamdirector.set_current_scene(scene_b)
                switch_count += 1

        else:
            a_dominant_since = None
            b_dominant_since = None

        stop_event.wait(REACTIVE_CHECK_INTERVAL)

    log_func(f"[autonomous] A/B switching stopped. Total switches: {switch_count}")
    return f"A/B switching stopped. Total switches: {switch_count}"


def run_autonomous_agent(instruction="", server_url=None, stop_event=None, log_func=None):
    """
    Run the autonomous agent loop. Blocks until stop_event is set.

    Args:
        instruction: Configuration (e.g., "switch between X and Y based on A vs B")
        server_url: BitNet inference server URL
        stop_event: threading.Event to signal stop
        log_func: function to call for logging
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

    ab_config = _parse_ab_config(instruction) if instruction else None

    if ab_config:
        return _run_ab_switching(ab_config, stop_event, log_func, server_url)

    # Default mode: reactive + LLM
    log_func("[autonomous] Starting autonomous stream director...")
    log_func(f"[autonomous] Reactive: every {REACTIVE_CHECK_INTERVAL}s | LLM: every {LLM_CHECK_INTERVAL}s")
    log_func(f"[autonomous] Tip: 'switch between X and Y based on A vs B' for A/B mode")

    last_llm_check = 0

    while not stop_event.is_set():
        state = _get_obs_state()

        actions = _reactive_check(state, [])
        for action in actions:
            log_func(f"[autonomous] {action}")

        now = time.time()
        if now - last_llm_check > LLM_CHECK_INTERVAL:
            last_llm_check = now
            levels = _get_audio_levels()
            stats = _get_streaming_stats()

            log_func("[autonomous] Asking LLM for scene decision...")
            decision = _llm_decision(state, levels, stats, server_url)
            log_func(f"[autonomous] LLM: {decision}")

            try:
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
                            log_func(f"[autonomous] Scene '{scene}' not found")
                    elif action == "none":
                        log_func(f"[autonomous] No action - {reason}")
            except Exception as e:
                log_func(f"[autonomous] Parse error: {e}")

        stop_event.wait(REACTIVE_CHECK_INTERVAL)

    log_func("[autonomous] Stopped.")
    return "Autonomous agent stopped."
