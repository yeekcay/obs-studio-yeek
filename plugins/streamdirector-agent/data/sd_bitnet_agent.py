"""
BitNet custom agent loop for StreamDirector.

Bypasses CrewAI entirely. Uses a local BitNet (or any llama.cpp-compatible)
inference server to parse natural language instructions into tool calls,
then executes them directly via the _streamdirector C++ API.

Architecture:
    User instruction → LLM prompt with tool definitions → BitNet inference
    → Parse JSON tool call → Execute via _streamdirector API → Return result

The inference server must be running separately (e.g. bitnet.cpp's llama-server
or any OpenAI-compatible endpoint). Default: http://127.0.0.1:8080
"""

import json
import os
import re
import urllib.request
import urllib.error

try:
    import _streamdirector as api
except ImportError:
    api = None


# --- Tool definitions exposed to the LLM ---

TOOL_DEFINITIONS = [
    {
        "name": "start_streaming",
        "description": "Start OBS streaming",
        "parameters": {},
    },
    {
        "name": "stop_streaming",
        "description": "Stop OBS streaming",
        "parameters": {},
    },
    {
        "name": "start_recording",
        "description": "Start OBS recording",
        "parameters": {},
    },
    {
        "name": "stop_recording",
        "description": "Stop OBS recording",
        "parameters": {},
    },
    {
        "name": "get_status",
        "description": "Get current streaming and recording status",
        "parameters": {},
    },
    {
        "name": "list_scenes",
        "description": "List all available OBS scenes and the current scene",
        "parameters": {},
    },
    {
        "name": "switch_scene",
        "description": "Switch to a specific scene by name",
        "parameters": {
            "scene_name": {"type": "string", "description": "Name of the scene to switch to"},
        },
    },
    {
        "name": "list_audio_sources",
        "description": "List all audio sources with their volume and mute status",
        "parameters": {},
    },
    {
        "name": "set_volume",
        "description": "Set volume for an audio source (in dB, -100 to 0)",
        "parameters": {
            "source_name": {"type": "string", "description": "Name of the audio source"},
            "volume_db": {"type": "number", "description": "Volume in decibels (-100 to 0)"},
        },
    },
    {
        "name": "set_mute",
        "description": "Mute or unmute an audio source",
        "parameters": {
            "source_name": {"type": "string", "description": "Name of the audio source"},
            "muted": {"type": "boolean", "description": "True to mute, False to unmute"},
        },
    },
    {
        "name": "get_stats",
        "description": "Get OBS performance stats (CPU, FPS, dropped frames)",
        "parameters": {},
    },
]

# --- Tool execution ---

def _execute_tool(tool_name: str, args: dict) -> str:
    """Execute a tool call directly via the _streamdirector C++ API."""
    if api is None:
        return "Error: _streamdirector module not available"

    try:
        if tool_name == "start_streaming":
            if api.is_streaming():
                return "Streaming is already active."
            api.start_streaming()
            return "Streaming started successfully."

        elif tool_name == "stop_streaming":
            if not api.is_streaming():
                return "Streaming is not active."
            api.stop_streaming()
            return "Streaming stopped successfully."

        elif tool_name == "start_recording":
            if api.is_recording():
                return "Recording is already active."
            api.start_recording()
            return "Recording started successfully."

        elif tool_name == "stop_recording":
            if not api.is_recording():
                return "Recording is not active."
            api.stop_recording()
            return "Recording stopped successfully."

        elif tool_name == "get_status":
            streaming = api.is_streaming()
            recording = api.is_recording()
            return f"Streaming: {'active' if streaming else 'inactive'}, Recording: {'active' if recording else 'inactive'}"

        elif tool_name == "list_scenes":
            names = api.get_scene_names()
            if not names:
                return "No scenes found."
            current = api.get_current_scene()
            return f"Scenes: {', '.join(names)}. Current: {current}"

        elif tool_name == "switch_scene":
            scene_name = args.get("scene_name", "")
            if not scene_name:
                return "Error: scene_name is required."
            success = api.set_current_scene(scene_name)
            return f"Switched to scene: {scene_name}" if success else f"Scene not found: {scene_name}"

        elif tool_name == "list_audio_sources":
            sources = api.get_audio_sources()
            if not sources:
                return "No audio sources found."
            results = []
            for name in sources:
                vol = api.get_source_volume(name)
                if vol:
                    import math
                    linear = vol.get("volume", 0.0)
                    db = 20.0 * math.log10(linear) if linear > 0 else -100.0
                    muted = vol.get("muted", False)
                    results.append(f"{name}: {db:.1f}dB, {'muted' if muted else 'active'}")
            return "Audio sources: " + "; ".join(results)

        elif tool_name == "set_volume":
            source_name = args.get("source_name", "")
            volume_db = args.get("volume_db", 0.0)
            if not source_name:
                return "Error: source_name is required."
            linear = 10.0 ** (volume_db / 20.0) if volume_db > -100 else 0.0
            success = api.set_source_volume(source_name, linear)
            return f"Set {source_name} volume to {volume_db}dB" if success else f"Failed to set volume for {source_name}"

        elif tool_name == "set_mute":
            source_name = args.get("source_name", "")
            muted = args.get("muted", True)
            if not source_name:
                return "Error: source_name is required."
            success = api.set_source_muted(source_name, muted)
            return f"{'Muted' if muted else 'Unmuted'} {source_name}" if success else f"Failed to set mute for {source_name}"

        elif tool_name == "get_stats":
            stats = api.get_stats()
            if not stats:
                return "No stats available."
            total = stats.get("streaming_total_frames", 0)
            skipped = stats.get("streaming_skipped_frames", 0)
            drop_pct = (skipped / total * 100) if total > 0 else 0.0
            return f"Total frames: {total}, Dropped: {skipped} ({drop_pct:.1f}%)"

        else:
            return f"Unknown tool: {tool_name}"

    except Exception as e:
        return f"Error executing {tool_name}: {e}"


# --- LLM inference via HTTP ---

def _call_llm(prompt: str, system_prompt: str, server_url: str, temperature: float = 0.1) -> str:
    """
    Call a local BitNet / llama.cpp inference server.
    Uses the /completion endpoint (native llama.cpp) which works better
    with small 1-bit models for few-shot prompting.
    Falls back to /v1/chat/completions if /completion fails.
    """
    # Primary: /completion endpoint with full prompt (best for small models)
    full_prompt = f"{system_prompt}\n\n{prompt}"
    try:
        payload = json.dumps({
            "prompt": full_prompt,
            "temperature": temperature,
            "n_predict": 64,
            "stop": ["\nInstruction:", "\n\n", "Instruction:"],
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{server_url}/completion",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("content", "").strip()
    except Exception:
        pass

    # Fallback: OpenAI-compatible /v1/chat/completions
    try:
        chat_payload = json.dumps({
            "model": "bitnet",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": 64,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{server_url}/v1/chat/completions",
            data=chat_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"LLM_ERROR: Could not connect to inference server at {server_url}. Error: {e}"


# --- Response parsing ---

TOOL_NAME_ALIASES = {
    "status": "get_status",
    "get_streaming_status": "get_status",
    "streaming_status": "get_status",
    "stats": "get_stats",
    "get_stat": "get_stats",
    "performance": "get_stats",
    "scenes": "list_scenes",
    "get_scenes": "list_scenes",
    "scene_list": "list_scenes",
    "audio": "list_audio_sources",
    "get_audio": "list_audio_sources",
    "audio_sources": "list_audio_sources",
    "mute": "set_mute",
    "unmute": "set_mute",
    "volume": "set_volume",
    "change_scene": "switch_scene",
    "set_scene": "switch_scene",
    "switch": "switch_scene",
    "go_live": "start_streaming",
    "go_offline": "stop_streaming",
    "end_stream": "stop_streaming",
    "end_streaming": "stop_streaming",
}

VALID_TOOL_NAMES = {t["name"].lower(): t["name"] for t in TOOL_DEFINITIONS}


def _normalize_tool_name(name: str) -> str:
    """Normalize a tool name from model output to a valid tool name."""
    name = name.strip().lower().replace(" ", "_").replace("-", "_")
    if name in VALID_TOOL_NAMES:
        return VALID_TOOL_NAMES[name]
    if name in TOOL_NAME_ALIASES:
        return TOOL_NAME_ALIASES[name]
    return ""


def _parse_tool_call(text: str):
    """
    Parse the LLM output to extract a tool call.
    Supports JSON format: {"tool": "name", "args": {...}}
    Also supports simpler formats like: TOOL: name(args)
    """
    # Clean up common model output issues
    text = text.replace("{...}", "{}").replace("{ ... }", "{}")
    
    # Try line-by-line JSON parsing first (works best with stop sequences)
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("{") or '"tool"' not in line:
            continue
        try:
            data = json.loads(line)
            tool_name = data.get("tool", data.get("name", data.get("function", "")))
            tool_args = data.get("args", data.get("arguments", data.get("parameters", {})))
            if tool_name:
                tool_name = _normalize_tool_name(str(tool_name))
                if tool_name:
                    return tool_name, tool_args
        except (json.JSONDecodeError, AttributeError):
            pass

    # Try JSON extraction with regex as fallback
    json_patterns = [
        r'\{[^{}]*"tool"[^{}]*\}',  # Simple single-level JSON
        r'\{.*?"tool".*?\}',  # Greedy match for nested braces
        r'```json\s*(\{.*?\})\s*```',  # JSON in code block
        r'```\s*(\{.*?\})\s*```',  # JSON in plain code block
    ]

    for pattern in json_patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                json_str = match.group(1) if match.lastindex else match.group(0)
                # Fix common JSON issues from small models
                json_str = json_str.replace("{...}", "{}").replace("{ ... }", "{}")
                data = json.loads(json_str)
                tool_name = data.get("tool", data.get("name", data.get("function", "")))
                tool_args = data.get("args", data.get("arguments", data.get("parameters", {})))
                if tool_name:
                    tool_name = _normalize_tool_name(str(tool_name))
                    if tool_name:
                        return tool_name, tool_args
            except (json.JSONDecodeError, AttributeError):
                continue

    # Try TOOL: name(args) format
    match = re.match(r'\s*TOOL:\s*(\w+)\s*\((.*?)\)\s*$', text, re.IGNORECASE)
    if match:
        tool_name = match.group(1)
        args_str = match.group(2).strip()
        tool_args = {}
        if args_str:
            # Parse simple key=value pairs
            for pair in args_str.split(','):
                if '=' in pair:
                    key, val = pair.split('=', 1)
                    key = key.strip()
                    val = val.strip().strip('"\'')
                    # Try to convert to appropriate type
                    if val.lower() in ('true', 'false'):
                        tool_args[key] = val.lower() == 'true'
                    else:
                        try:
                            tool_args[key] = float(val)
                        except ValueError:
                            tool_args[key] = val
        return tool_name, tool_args

    # Try direct tool name match (single word response)
    text_clean = text.strip().lower().rstrip('.')
    normalized = _normalize_tool_name(text_clean)
    if normalized:
        return normalized, {}

    return None, None


# --- System prompt builder ---

def _build_system_prompt() -> str:
    """Build the system prompt with few-shot examples for small models."""
    return """You are an OBS control agent. Respond with ONLY JSON. No explanation.

Instruction: go live
{"tool": "start_streaming", "args": {}}

Instruction: start streaming
{"tool": "start_streaming", "args": {}}

Instruction: stop streaming
{"tool": "stop_streaming", "args": {}}

Instruction: stop the stream
{"tool": "stop_streaming", "args": {}}

Instruction: go offline
{"tool": "stop_streaming", "args": {}}

Instruction: start recording
{"tool": "start_recording", "args": {}}

Instruction: stop recording
{"tool": "stop_recording", "args": {}}

Instruction: what is my status
{"tool": "get_status", "args": {}}

Instruction: list scenes
{"tool": "list_scenes", "args": {}}

Instruction: switch to Gaming scene
{"tool": "switch_scene", "args": {"scene_name": "Gaming"}}

Instruction: switch to Scene 2
{"tool": "switch_scene", "args": {"scene_name": "Scene 2"}}

Instruction: mute my mic
{"tool": "set_mute", "args": {"source_name": "Mic/Aux", "muted": true}}

Instruction: unmute my mic
{"tool": "set_mute", "args": {"source_name": "Mic/Aux", "muted": false}}

Instruction: lower game volume to -20
{"tool": "set_volume", "args": {"source_name": "Desktop Audio", "volume_db": -20}}

Instruction: show audio sources
{"tool": "list_audio_sources", "args": {}}

Instruction: get stats
{"tool": "get_stats", "args": {}}"""


# --- Keyword fast path (bypasses LLM for common commands) ---

def _try_keyword_match(instruction: str):
    """
    Try to match common instructions directly without calling the LLM.
    Returns (tool_name, tool_args, matched) or (None, None, False).
    """
    text = instruction.lower().strip().strip("'\"")

    # Streaming
    if text in ("go live", "start stream", "start streaming", "go online"):
        return "start_streaming", {}, True
    if text in ("stop stream", "stop streaming", "stop the stream", "end stream",
                "go offline", "end streaming", "stop going live"):
        return "stop_streaming", {}, True

    # Recording
    if text in ("start recording", "begin recording", "record"):
        return "start_recording", {}, True
    if text in ("stop recording", "end recording", "stop record"):
        return "stop_recording", {}, True

    # Status
    if text in ("status", "what is my status", "what's my status",
                "current status", "stream status", "check status"):
        return "get_status", {}, True

    # Scenes
    if text in ("list scenes", "scenes", "show scenes", "what scenes",
                "get scenes", "scene list"):
        return "list_scenes", {}, True

    # Scene switching — extract scene name
    if text.startswith("switch to ") or text.startswith("change to ") or text.startswith("switch scene to "):
        scene_name = instruction.strip()
        for prefix in ("switch to ", "change to ", "switch scene to ",
                       "Switch to ", "Change to ", "Switch scene to "):
            if scene_name.startswith(prefix):
                scene_name = scene_name[len(prefix):]
                break
        scene_name = scene_name.strip().strip("'\"")
        if scene_name:
            return "switch_scene", {"scene_name": scene_name}, True

    # Audio sources
    if text in ("list audio", "audio sources", "show audio", "list audio sources",
                "audio", "get audio"):
        return "list_audio_sources", {}, True

    # Mute/unmute
    if text in ("mute", "mute mic", "mute my mic", "mute microphone"):
        return "set_mute", {"source_name": "Mic/Aux", "muted": True}, True
    if text in ("unmute", "unmute mic", "unmute my mic", "unmute microphone"):
        return "set_mute", {"source_name": "Mic/Aux", "muted": False}, True

    # Stats
    if text in ("stats", "get stats", "performance", "frame stats",
                "dropped frames", "show stats"):
        return "get_stats", {}, True

    return None, None, False


# --- Main agent loop ---

def run_bitnet_agent(instruction: str, server_url: str = None) -> str:
    """
    Run a single-turn agent: instruction → LLM → tool call → execution → result.
    Uses keyword matching first for common commands, falls back to LLM.

    Args:
        instruction: Natural language instruction from the user
        server_url: URL of the BitNet/llama.cpp inference server.
                    Defaults to env var BITNET_SERVER_URL or http://127.0.0.1:8080

    Returns:
        Result string from tool execution or error message
    """
    if api is None:
        return "Error: _streamdirector module not available"

    if server_url is None:
        server_url = os.environ.get("BITNET_SERVER_URL", "http://127.0.0.1:8080")

    api.log(f"[bitnet-agent] Instruction: {instruction}")

    # Try keyword fast path first
    tool_name, tool_args, matched = _try_keyword_match(instruction)
    if matched:
        api.log(f"[bitnet-agent] Keyword match: {tool_name}({tool_args})")
        result = _execute_tool(tool_name, tool_args)
        api.log(f"[bitnet-agent] Result: {result}")

        # Auto start/stop recording with streaming
        instruction_lower = instruction.lower().strip()
        if tool_name == "start_streaming" and not api.is_recording():
            api.log("[bitnet-agent] Auto-starting recording for 'go live'")
            rec_result = _execute_tool("start_recording", {})
            result += f"\n{rec_result}"
        elif tool_name == "stop_streaming" and api.is_recording():
            api.log("[bitnet-agent] Auto-stopping recording for 'stop stream'")
            rec_result = _execute_tool("stop_recording", {})
            result += f"\n{rec_result}"

        return result

    # Fall back to LLM for complex instructions
    api.log(f"[bitnet-agent] No keyword match, using LLM. Server: {server_url}")

    system_prompt = _build_system_prompt()

    # Get current OBS state for context
    try:
        streaming = api.is_streaming()
        recording = api.is_recording()
        current_scene = api.get_current_scene()
        context = f"Current state: streaming={streaming}, recording={recording}, scene={current_scene}"
    except Exception:
        context = "Could not read current OBS state."

    full_prompt = f"Context: {context}\nInstruction: {instruction}"

    api.log("[bitnet-agent] Calling LLM...")
    response = _call_llm(full_prompt, system_prompt, server_url)

    if response.startswith("LLM_ERROR"):
        api.log(f"[bitnet-agent] {response}")
        return response

    api.log(f"[bitnet-agent] LLM response: {response}")

    # Parse tool call
    tool_name, tool_args = _parse_tool_call(response)

    if tool_name is None:
        # Could not parse a tool call, return the raw response
        api.log(f"[bitnet-agent] Could not parse tool call from: {response}")
        return f"Agent could not determine a tool call. Raw response: {response}"

    api.log(f"[bitnet-agent] Executing: {tool_name}({tool_args})")

    # Execute the tool
    result = _execute_tool(tool_name, tool_args)
    api.log(f"[bitnet-agent] Result: {result}")

    # Check if we need a follow-up (e.g., "go live" should also start recording)
    instruction_lower = instruction.lower().strip()
    if instruction_lower in ("go live", "start stream", "start streaming") and tool_name == "start_streaming":
        if not api.is_recording():
            api.log("[bitnet-agent] Auto-starting recording for 'go live'")
            rec_result = _execute_tool("start_recording", {})
            result += f"\n{rec_result}"

    elif instruction_lower in ("stop stream", "stop streaming", "stop the stream", "end stream", "go offline") and tool_name == "stop_streaming":
        if api.is_recording():
            api.log("[bitnet-agent] Auto-stopping recording for 'stop stream'")
            rec_result = _execute_tool("stop_recording", {})
            result += f"\n{rec_result}"

    return result


def run_bitnet_agent_multi_turn(instruction: str, server_url: str = None, max_turns: int = 3) -> str:
    """
    Multi-turn agent loop: allows the LLM to make multiple tool calls in sequence.
    After each tool execution, the result is fed back to the LLM which can decide
    to call another tool or finish.

    Args:
        instruction: Natural language instruction from the user
        server_url: URL of the BitNet/llama.cpp inference server
        max_turns: Maximum number of LLM round-trips

    Returns:
        Final result string
    """
    if api is None:
        return "Error: _streamdirector module not available"

    if server_url is None:
        server_url = os.environ.get("BITNET_SERVER_URL", "http://127.0.0.1:8080")

    api.log(f"[bitnet-agent] Multi-turn instruction: {instruction}")
    api.log(f"[bitnet-agent] Server: {server_url}")

    system_prompt = _build_system_prompt() + """

After receiving a tool result, you can either:
1. Call another tool if more actions are needed
2. Respond with {"tool": "done", "args": {}} to finish

Always respond with exactly one JSON tool call."""

    # Build conversation history
    messages = []

    # Initial context
    try:
        streaming = api.is_streaming()
        recording = api.is_recording()
        current_scene = api.get_current_scene()
        context = f"Current state: streaming={streaming}, recording={recording}, scene={current_scene}"
    except Exception:
        context = "Could not read current OBS state."

    messages.append({"role": "user", "content": f"Context: {context}\nInstruction: {instruction}"})

    results = []

    for turn in range(max_turns):
        api.log(f"[bitnet-agent] Turn {turn + 1}/{max_turns}")

        # Build prompt from conversation history
        prompt_parts = []
        for msg in messages:
            prefix = "User" if msg["role"] == "user" else "Assistant"
            prompt_parts.append(f"{prefix}: {msg['content']}")
        full_prompt = "\n".join(prompt_parts)

        response = _call_llm(full_prompt, system_prompt, server_url)

        if response.startswith("LLM_ERROR"):
            api.log(f"[bitnet-agent] {response}")
            return response

        api.log(f"[bitnet-agent] LLM response: {response}")

        tool_name, tool_args = _parse_tool_call(response)

        if tool_name is None:
            api.log(f"[bitnet-agent] Could not parse tool call, treating as final answer")
            return response

        if tool_name == "done":
            api.log("[bitnet-agent] Agent signaled completion")
            break

        api.log(f"[bitnet-agent] Executing: {tool_name}({tool_args})")
        result = _execute_tool(tool_name, tool_args)
        api.log(f"[bitnet-agent] Result: {result}")
        results.append(result)

        # Feed result back to LLM
        messages.append({"role": "assistant", "content": response})
        messages.append({"role": "user", "content": f"Tool result: {result}\nCall another tool or respond with done."})

    if results:
        return "\n".join(results)
    return "No actions taken."
