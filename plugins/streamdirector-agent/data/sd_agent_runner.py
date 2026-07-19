"""
StreamDirector Agent Runner - Embedded Python module.

This module is called by the C++ PythonBridge to run CrewAI agents
using direct OBS API calls instead of WebSocket.
"""

import os
import sys
import traceback
from typing import Optional


def _ensure_agent_path():
    """Ensure the obs-agent src directory and venv site-packages are on sys.path."""
    import _streamdirector as _api

    # Determine the obs-agent root directory
    plugin_data_dir = os.path.dirname(__file__)
    candidates = [
        # Dev build: plugin data is under build_x64_dev/rundir/RelWithDebInfo/data/obs-plugins/streamdirector-agent
        # obs-agent is at the repo root — 6 levels up from plugin data dir
        os.path.join(plugin_data_dir, "..", "..", "..", "..", "..", "..", "obs-agent"),
        # Alternative: maybe shallower
        os.path.join(plugin_data_dir, "..", "..", "..", "..", "..", "obs-agent"),
        os.path.join(plugin_data_dir, "..", "..", "..", "..", "obs-agent"),
        # Bundled with plugin
        os.path.join(plugin_data_dir, "agent"),
        # Environment variable override
        os.environ.get("SD_AGENT_PATH", ""),
        # Hardcoded fallback for this dev environment
        r"D:\Code\OhBeeS\obs-studio-yeek\obs-agent",
    ]

    agent_root = None
    for path in candidates:
        if not path:
            continue
        abs_path = os.path.abspath(path)
        if os.path.exists(os.path.join(abs_path, "src", "crew", "__init__.py")):
            agent_root = abs_path
            src_path = os.path.join(abs_path, "src")
            if src_path not in sys.path:
                sys.path.insert(0, src_path)
                _api.log(f"Added agent src path: {src_path}")
            break

    if not agent_root:
        _api.log("WARNING: Could not find obs-agent/src/crew directory.")
        return

    # Add the venv site-packages so CrewAI and its dependencies are importable
    venv_site_packages = os.path.join(agent_root, "venv", "Lib", "site-packages")
    if os.path.exists(venv_site_packages):
        if venv_site_packages not in sys.path:
            sys.path.insert(0, venv_site_packages)
            _api.log(f"Added venv site-packages: {venv_site_packages}")

        # Add DLL directories so native .pyd extensions can find their dependencies
        venv_dll_dirs = [
            os.path.join(agent_root, "venv", "Lib", "site-packages"),
            os.path.join(agent_root, "venv", "Scripts"),
            os.path.join(agent_root, "venv"),
            # Pydantic core and other native extensions may need these
            os.path.join(venv_site_packages, "pydantic_core"),
            os.path.join(venv_site_packages, "cryptography", "hazmat", "bindings"),
        ]
        for dll_dir in venv_dll_dirs:
            if os.path.exists(dll_dir):
                try:
                    os.add_dll_directory(dll_dir)
                    _api.log(f"Added DLL directory: {dll_dir}")
                except (OSError, AttributeError):
                    pass
    else:
        _api.log(f"WARNING: venv site-packages not found at {venv_site_packages}")

    # Load .env from the agent root if python-dotenv is available
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(agent_root, ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path)
            _api.log(f"Loaded .env from {env_path}")
    except ImportError:
        _api.log("python-dotenv not available, skipping .env loading")


def _patch_obs_connection():
    """
    Monkey-patch the OBSConnection singleton to use DirectOBSAPI
    instead of the WebSocket-based AdvancedOBSAgent.
    """
    try:
        from crew.obs_crew_tools import OBSConnection
        import sd_direct_api

        direct_api = sd_direct_api.DirectOBSAPI()

        def patched_get_agent(self):
            return direct_api

        async def patched_ensure_connected(self):
            return direct_api

        OBSConnection.get_agent = patched_get_agent
        OBSConnection.ensure_connected = patched_ensure_connected

    except ImportError:
        pass


def _build_responsive_crew(llm, all_tools, instruction):
    """Build a single-agent crew that only does what the user asks."""
    from crewai import Agent, Crew, Process, Task

    agent = Agent(
        role="StreamDirector Agent",
        goal="Execute the user's instruction using OBS tools. Only take actions the user explicitly asks for. If the user asks for status, just report status — do NOT start/stop recording or streaming.",
        backstory="""You are a direct OBS control agent embedded inside StreamDirector.
        You have tools to manage scenes, audio, recording, streaming, filters, and monitor stats.
        IMPORTANT RULES:
        - Only take actions the user EXPLICITLY requests.
        - If the user asks for 'status', use obs_snapshot and obs_stats_monitor to report, then stop.
        - If the user asks to 'start recording', use obs_recording_controller with action 'start'.
        - If the user asks to 'switch scene', use obs_scene_manager with the scene name.
        - NEVER start recording or streaming unless the user explicitly asks.
        - NEVER adjust audio levels unless the user explicitly asks.
        - Use 'list' as source_name with obs_audio_controller to list audio sources before operating on them.
        - Use 'list' as scene_name with obs_scene_manager to list available scenes.""",
        tools=all_tools,
        verbose=True,
        allow_delegation=False,
        llm=llm,
    )

    task = Task(
        description=f"User instruction: {instruction}\n\nExecute this instruction using the available OBS tools. Only do what the user asks — do not take autonomous actions beyond the request.",
        expected_output="A concise response to the user about what was done or the current status.",
        agent=agent,
    )

    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
    )
    return crew, {}


def _build_autonomous_crew(llm, all_tools, instruction):
    """Build a multi-agent crew that autonomously manages the stream.

    This crew can:
    - Start streaming and recording if not active
    - Switch scenes based on audio activity (e.g. switch to a scene when mic is active)
    - Balance audio levels
    - Monitor stream health
    - Coordinate all aspects of the broadcast
    """
    from crewai import Agent, Crew, Process, Task

    scene_tool, audio_tool, recording_tool, streaming_tool, \
        stats_tool, filter_tool, snapshot_tool = all_tools

    # Agent 1: Stream Director — coordinates and makes scene decisions
    stream_director = Agent(
        role="Stream Director",
        goal="Coordinate the live stream, make scene switching decisions based on audio activity, and ensure engaging content flow.",
        backstory="""You are an experienced live stream director. You monitor audio activity
        to decide when to switch scenes. For example, if the microphone is active (someone is talking),
        you might switch to a 'talking head' scene. If audio is quiet, switch to an intermission or
        'just chatting' scene. You also decide when to start and stop streaming based on the user's
        instruction. You coordinate all other agents.""",
        tools=[scene_tool, snapshot_tool, audio_tool],
        verbose=True,
        allow_delegation=True,
        llm=llm,
    )

    # Agent 2: Audio Engineer — balances audio, applies noise suppression
    audio_engineer = Agent(
        role="Audio Engineer",
        goal="Ensure all audio sources are balanced, clear, and free from noise.",
        backstory="""You are a professional audio engineer. You check all audio source levels,
        adjust them to optimal range (-20dB to -15dB), and ensure no source is unintentionally muted.
        You can detect which sources are active and report this to the Stream Director for
        scene-switching decisions.""",
        tools=[audio_tool, filter_tool],
        verbose=True,
        allow_delegation=False,
        llm=llm,
    )

    # Agent 3: Technical Producer — manages streaming/recording and monitors performance
    tech_producer = Agent(
        role="Technical Producer",
        goal="Manage streaming and recording operations, monitor system performance, and ensure technical stability.",
        backstory="""You are a technical producer responsible for the broadcast infrastructure.
        You start/stop streaming and recording as needed, monitor CPU usage, dropped frames, and
        system health. You report issues and take corrective action.""",
        tools=[recording_tool, streaming_tool, stats_tool],
        verbose=True,
        allow_delegation=False,
        llm=llm,
    )

    # Task 1: Check audio and report active sources
    audio_task = Task(
        description=f"""User instruction: {instruction}

        1. List all audio sources using obs_audio_controller with source_name 'list'
        2. Check volume levels for each source
        3. Identify which sources are currently active (not muted, volume > 0)
        4. Adjust levels to optimal range if they are too loud or too quiet
        5. Report which audio sources are active — this will inform scene switching decisions

        Context: The user wants the stream to be managed autonomously. Use the instruction as guidance
        for what kind of stream this is.""",
        expected_output="Audio report listing all sources, their levels, and which are active.",
        agent=audio_engineer,
    )

    # Task 2: Manage streaming and recording
    tech_task = Task(
        description=f"""User instruction: {instruction}

        1. Check current streaming status by calling obs_streaming_controller with action='status'
        2. Check current recording status by calling obs_recording_controller with action='status'
        3. CRITICAL: If the user's instruction implies going live (e.g. "go live", "start streaming", "start the stream"),
           you MUST call obs_streaming_controller with action='start' to actually start streaming.
           Do NOT just say "starting the stream" — you must actually call the tool with action='start'.
        4. CRITICAL: If not already recording, you MUST call obs_recording_controller with action='start'
           to actually start recording. Do NOT just say "starting recording" — actually call the tool.
        5. After starting, call obs_streaming_controller with action='status' again to CONFIRM streaming is active.
        6. Call obs_stats_monitor to check system stats (CPU, frames, etc.)
        7. Report the confirmed streaming/recording state and any technical issues

        Context: The user wants autonomous stream management. If they said something like 'go live'
        or 'start the stream', you MUST actually call the start action on the tools — do not just
        describe what you would do. If they said 'status', just report status without starting anything.""",
        expected_output="Technical status report confirming streaming and recording are active, with system stats.",
        agent=tech_producer,
    )

    # Task 3: Coordinate — switch scenes based on audio activity, summarize
    coord_task = Task(
        description=f"""User instruction: {instruction}

        As the Stream Director, coordinate the broadcast:
        1. Review the audio report to see which sources are active
        2. Review the technical status
        3. Based on audio activity, decide if a scene switch is appropriate:
           - If microphone is active and current scene is an interscene/brb scene, switch to the main content scene
           - If no audio activity, consider switching to a 'starting soon' or 'BRB' scene if one exists
           - Use obs_scene_manager with scene_name 'list' to see available scenes first
        4. Only switch scenes if it makes sense given the user's instruction
        5. Provide a summary of the stream status and any actions taken

        Context: The user wants autonomous management. Use their instruction to understand the stream's purpose.""",
        expected_output="Stream coordination summary with scene decisions and overall status.",
        agent=stream_director,
        context=[audio_task, tech_task],
    )

    crew = Crew(
        agents=[audio_engineer, tech_producer, stream_director],
        tasks=[audio_task, tech_task, coord_task],
        process=Process.sequential,
        verbose=True,
    )

    inputs = {
        "stream_topic": instruction,
    }
    return crew, inputs


def run_agent(instruction: str, mode: str = "responsive") -> str:
    """
    Run the StreamDirector agent with the given instruction.

    Args:
        instruction: Natural language instruction for the agent.
        mode: Agent mode - "responsive" (only do what asked) or "autonomous" (full crew pipeline).

    Returns:
        String result from the agent execution.
    """
    import _streamdirector as api
    api.log(f"run_agent called with: {instruction}")

    try:
        _ensure_agent_path()

        # Add venv to PATH so native DLLs can be found
        venv_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "..", "obs-agent", "venv")
        venv_path = os.path.abspath(venv_path)
        if not os.path.exists(venv_path):
            venv_path = r"D:\Code\OhBeeS\obs-studio-yeek\obs-agent\venv"
        venv_site = os.path.join(venv_path, "Lib", "site-packages")
        if os.path.exists(venv_site):
            os.environ["PATH"] = venv_site + os.pathsep + venv_path + os.pathsep + os.environ.get("PATH", "")
            for d in [venv_site, venv_path, os.path.join(venv_site, "pydantic_core"),
                      os.path.join(venv_site, "cryptography", "hazmat", "bindings")]:
                if os.path.exists(d):
                    try:
                        os.add_dll_directory(d)
                    except (OSError, AttributeError):
                        pass

        # Try to import and use the CrewAI-based agent
        try:
            api.log("Attempting to import crewai...")
            import crewai
            from crewai import Agent, Crew, Process, Task
            api.log("crewai imported OK.")
            api.log("Attempting to import crew.obs_crew_tools...")
            from crew.obs_crew_tools import (
                OBSAudioTool, OBSFilterTool, OBSRecordingTool,
                OBSSceneTool, OBSSnapshotTool, OBSStatsTool, OBSStreamingTool,
            )
            api.log("Attempting to import crew.llm_config...")
            from crew.llm_config import get_ollama_llm
            api.log("CrewAI modules found, attempting full agent mode...")
        except ImportError as e:
            api.log(f"CrewAI not available ({e}), falling back to simple command mode.")
            api.log(f"ImportError details: {type(e).__name__}: {e}")
            return run_simple_command(instruction)

        # Set up the LLM
        try:
            llm = get_ollama_llm()
            api.log("LLM initialized successfully.")
        except Exception as e:
            api.log(f"Failed to initialize LLM: {e}")
            return f"Failed to initialize LLM: {e}\n" \
                   f"Make sure Ollama is running at http://localhost:11434"

        # Patch OBSConnection to use direct API instead of WebSocket
        _patch_obs_connection()
        api.log("OBS connection patched for direct API.")

        # Build tools (shared across modes)
        scene_tool = OBSSceneTool()
        audio_tool = OBSAudioTool()
        recording_tool = OBSRecordingTool()
        streaming_tool = OBSStreamingTool()
        stats_tool = OBSStatsTool()
        filter_tool = OBSFilterTool()
        snapshot_tool = OBSSnapshotTool()
        all_tools = [scene_tool, audio_tool, recording_tool, streaming_tool,
                     stats_tool, filter_tool, snapshot_tool]

        if mode == "autonomous":
            # Fast path: simple streaming/recording commands don't need the full crew
            cmd_lower = instruction.lower().strip()
            simple_stream_cmds = [
                "go live", "start stream", "start streaming", "stop stream",
                "stop streaming", "stop the stream", "end stream",
                "end streaming", "go offline", "start recording",
                "stop recording", "stop the recording",
            ]
            if cmd_lower in simple_stream_cmds:
                api.log(f"Fast path: handling '{cmd_lower}' directly without full crew.")
                if cmd_lower in ("go live", "start stream", "start streaming"):
                    if not api.is_streaming():
                        api.start_streaming()
                    if not api.is_recording():
                        api.start_recording()
                    return "Stream started: streaming and recording are now active."
                elif cmd_lower in ("stop stream", "stop streaming", "stop the stream",
                                   "end stream", "end streaming", "go offline"):
                    if api.is_streaming():
                        api.stop_streaming()
                    if api.is_recording():
                        api.stop_recording()
                    return "Stream stopped: streaming and recording have been stopped."
                elif cmd_lower == "start recording":
                    if not api.is_recording():
                        api.start_recording()
                    return "Recording started."
                elif cmd_lower in ("stop recording", "stop the recording"):
                    if api.is_recording():
                        api.stop_recording()
                    return "Recording stopped."

            api.log("Building autonomous stream crew...")
            crew, inputs = _build_autonomous_crew(llm, all_tools, instruction)
        else:
            api.log("Building responsive agent...")
            crew, inputs = _build_responsive_crew(llm, all_tools, instruction)

        api.log("Crew created, starting kickoff...")

        import asyncio
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            if inputs:
                result = loop.run_until_complete(crew.kickoff_async(inputs=inputs))
            else:
                result = loop.run_until_complete(crew.kickoff_async())
        finally:
            loop.close()

        api.log("Agent completed.")
        return str(result)

    except Exception as e:
        tb = traceback.format_exc()
        return f"Agent error: {e}\n\nTraceback:\n{tb}"


def run_simple_command(command: str) -> str:
    """
    Run a simple OBS command directly without the full agent pipeline.

    This is a lightweight alternative for direct control.
    Supports natural language matching for common operations.
    """
    try:
        import _streamdirector as api
    except ImportError:
        return "StreamDirector API bridge not available"

    original = command.strip()
    cmd_lower = original.lower()

    if cmd_lower == "status":
        streaming = api.is_streaming()
        recording = api.is_recording()
        return f"Streaming: {streaming}, Recording: {recording}"

    elif cmd_lower == "scenes":
        names = api.get_scene_names()
        if names:
            current = api.get_current_scene()
            return f"Scenes: {', '.join(names)}. Current: {current}"
        return "No scenes found"

    elif cmd_lower.startswith("switch "):
        scene = original[7:].strip()  # Preserve original case for scene name
        success = api.set_current_scene(scene)
        return f"Switched to scene: {scene}" if success else f"Scene not found: {scene}"

    # Natural language scene switching: "switch to <scene>", "change to <scene>", "go to <scene>"
    elif cmd_lower.startswith("switch to "):
        scene = original[10:].strip()
        success = api.set_current_scene(scene)
        return f"Switched to scene: {scene}" if success else f"Scene not found: {scene}"

    elif cmd_lower.startswith("change to "):
        scene = original[10:].strip()
        success = api.set_current_scene(scene)
        return f"Switched to scene: {scene}" if success else f"Scene not found: {scene}"

    elif cmd_lower.startswith("go to "):
        scene = original[6:].strip()
        success = api.set_current_scene(scene)
        return f"Switched to scene: {scene}" if success else f"Scene not found: {scene}"

    # Try fuzzy scene match: if the input matches a scene name exactly
    elif cmd_lower != original:
        names = api.get_scene_names()
        if names:
            for name in names:
                if name.lower() == cmd_lower:
                    success = api.set_current_scene(name)
                    return f"Switched to scene: {name}" if success else f"Scene not found: {name}"

    elif cmd_lower == "start streaming":
        api.start_streaming()
        return "Streaming started"

    elif cmd_lower == "stop streaming":
        api.stop_streaming()
        return "Streaming stopped"

    elif cmd_lower == "start recording":
        api.start_recording()
        return "Recording started"

    elif cmd_lower == "stop recording":
        api.stop_recording()
        return "Recording stopped"

    elif cmd_lower == "pause recording":
        api.pause_recording()
        return "Recording paused"

    elif cmd_lower == "resume recording":
        api.resume_recording()
        return "Recording resumed"

    elif cmd_lower == "audio":
        sources = api.get_audio_sources()
        if sources:
            info = []
            for name in sources:
                vol = api.get_source_volume(name)
                if vol:
                    muted = "muted" if vol.get("muted") else "active"
                    info.append(f"{name}: {muted}")
            return "Audio sources: " + "; ".join(info)
        return "No audio sources found"

    elif cmd_lower.startswith("mute "):
        source = original[5:].strip()
        success = api.set_source_muted(source, True)
        return f"Muted: {source}" if success else f"Source not found: {source}"

    elif cmd_lower.startswith("unmute "):
        source = original[7:].strip()
        success = api.set_source_muted(source, False)
        return f"Unmuted: {source}" if success else f"Source not found: {source}"

    elif cmd_lower == "help":
        return ("Available commands:\n"
                "  status - Show streaming/recording status\n"
                "  scenes - List all scenes\n"
                "  switch <name> - Switch to a scene\n"
                "  switch to <name> / change to <name> / go to <name> - Switch scene (natural)\n"
                "  start streaming / stop streaming\n"
                "  start recording / stop recording\n"
                "  pause recording / resume recording\n"
                "  audio - List audio sources\n"
                "  mute <source> / unmute <source>\n"
                "  help - Show this help")

    return f"Unknown command: {original}. Type 'help' for available commands."
