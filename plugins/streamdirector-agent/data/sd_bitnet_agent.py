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

def _call_llm(prompt: str, system_prompt: str, server_url: str, temperature: float = 0.3) -> str:
    """
    Call a local llama.cpp / BitNet compatible server.
    Uses the OpenAI-compatible /v1/chat/completions endpoint.
    Falls back to /completion if that fails (llama.cpp native endpoint).
    """
    # Try OpenAI-compatible endpoint first
    payload = json.dumps({
        "model": "bitnet",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": 512,
    }).encode("utf-8")

    # Try /v1/chat/completions first
    try:
        req = urllib.request.Request(
            f"{server_url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, KeyError, json.JSONDecodeError):
        pass

    # Fallback: llama.cpp native /completion endpoint
    try:
        full_prompt = f"{system_prompt}\n\nUser: {prompt}\nAssistant:"
        fallback_payload = json.dumps({
            "prompt": full_prompt,
            "temperature": temperature,
            "n_predict": 512,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{server_url}/completion",
            data=fallback_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("content", "").strip()
    except Exception as e:
        return f"LLM_ERROR: Could not connect to inference server at {server_url}. Error: {e}"


# --- Response parsing ---

def _parse_tool_call(text: str):
    """
    Parse the LLM output to extract a tool call.
    Supports JSON format: {"tool": "name", "args": {...}}
    Also supports simpler formats like: TOOL: name(args)
    """
    # Try JSON extraction first
    json_patterns = [
        r'\{[^{}]*"tool"[^{}]*\}',  # Simple single-level JSON
        r'```json\s*(\{.*?\})\s*```',  # JSON in code block
        r'```\s*(\{.*?\})\s*```',  # JSON in plain code block
    ]

    for pattern in json_patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                json_str = match.group(1) if match.lastindex else match.group(0)
                data = json.loads(json_str)
                tool_name = data.get("tool", data.get("name", data.get("function", "")))
                tool_args = data.get("args", data.get("arguments", data.get("parameters", {})))
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
    tool_names = {t["name"].lower(): t["name"] for t in TOOL_DEFINITIONS}
    if text_clean in tool_names:
        return tool_names[text_clean], {}

    return None, None


# --- System prompt builder ---

def _build_system_prompt() -> str:
    """Build the system prompt with tool definitions."""
    tools_desc = []
    for tool in TOOL_DEFINITIONS:
        params = tool["parameters"]
        if params:
            param_str = ", ".join(f'{k}: {v.get("type", "string")}' for k, v in params.items())
            tools_desc.append(f'  - {tool["name"]}({param_str}): {tool["description"]}')
        else:
            tools_desc.append(f'  - {tool["name"]}(): {tool["description"]}')

    return f"""You are an OBS (streaming software) control agent. You receive natural language instructions and respond with a single tool call in JSON format.

Available tools:
{chr(10).join(tools_desc)}

Respond with ONLY a JSON tool call. No explanation needed.
Format: {{"tool": "tool_name", "args": {{...}}}}

Examples:
- "go live" -> {{"tool": "start_streaming", "args": {{}}}}
- "stop the stream" -> {{"tool": "stop_streaming", "args": {{}}}}
- "switch to scene 2" -> {{"tool": "switch_scene", "args": {{"scene_name": "Scene 2"}}}}
- "mute my mic" -> {{"tool": "set_mute", "args": {{"source_name": "Mic/Aux", "muted": true}}}}
- "lower game volume to -20" -> {{"tool": "set_volume", "args": {{"source_name": "Desktop Audio", "volume_db": -20}}}}
- "what's my status" -> {{"tool": "get_status", "args": {{}}}}
- "start recording" -> {{"tool": "start_recording", "args": {{}}}}

If the instruction is ambiguous, pick the most likely tool. Always respond with exactly one tool call as JSON."""


# --- Main agent loop ---

def run_bitnet_agent(instruction: str, server_url: str = None) -> str:
    """
    Run a single-turn agent: instruction → LLM → tool call → execution → result.

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
    api.log(f"[bitnet-agent] Server: {server_url}")

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
