"""
Direct OBS API wrapper for StreamDirector.

This replaces the WebSocket-based AdvancedOBSAgent with direct C++ API calls
via the _streamdirector embedded module. No network connection needed.
"""

from typing import Any, Dict, List, Optional
import _streamdirector as api


class DirectOBSAPI:
    """Direct API wrapper that replaces WebSocket-based OBS control."""

    def __init__(self, host: str = "", port: int = 0, password: str = ""):
        # No connection needed - we use the embedded C++ API directly
        self.connected = True

    async def connect(self):
        self.connected = True
        return True

    async def disconnect(self):
        self.connected = False

    # --- Scene Management ---

    async def get_scenes(self) -> List[str]:
        names = api.get_scene_names()
        if names is None:
            return []
        return list(names)

    async def get_current_scene(self) -> str:
        result = api.get_current_scene()
        return result if result else ""

    async def set_scene(self, scene_name: str) -> bool:
        return api.set_current_scene(scene_name)

    # --- Streaming ---

    async def start_streaming(self) -> bool:
        api.start_streaming()
        return True

    async def stop_streaming(self) -> bool:
        api.stop_streaming()
        return True

    async def get_streaming_status(self) -> Dict[str, Any]:
        is_streaming = api.is_streaming()
        stats = api.get_stats()
        return {
            "is_streaming": is_streaming,
            "duration": 0,  # Not directly available via frontend API
            "bytes": 0,
            "total_frames": stats.get("streaming_total_frames", 0),
            "skipped_frames": stats.get("streaming_skipped_frames", 0),
        }

    # --- Recording ---

    async def start_recording(self) -> bool:
        api.start_recording()
        return True

    async def stop_recording(self) -> Optional[str]:
        api.stop_recording()
        return "Recording stopped"

    async def pause_recording(self) -> bool:
        api.pause_recording()
        return True

    async def resume_recording(self) -> bool:
        api.resume_recording()
        return True

    async def get_recording_status(self) -> Dict[str, Any]:
        is_recording = api.is_recording()
        return {
            "is_recording": is_recording,
            "is_paused": False,  # Not directly available
            "duration": 0,
            "bytes": 0,
        }

    # --- Audio ---

    async def get_sources(self) -> List[Dict[str, Any]]:
        audio_names = api.get_audio_sources()
        if audio_names is None:
            return []
        return [{"inputName": name, "inputKind": "Audio"} for name in audio_names]

    async def get_source_volume(self, source_name: str) -> Dict[str, Any]:
        result = api.get_source_volume(source_name)
        if result is None:
            return {"volume_db": 0.0, "volume": 0.0}
        return {
            "volume_db": _linear_to_db(result.get("volume", 0.0)),
            "volume": result.get("volume", 0.0),
        }

    async def set_source_volume(self, source_name: str, volume_db: float) -> bool:
        linear = _db_to_linear(volume_db)
        return api.set_source_volume(source_name, linear)

    async def get_source_mute(self, source_name: str) -> bool:
        result = api.get_source_volume(source_name)
        if result is None:
            return False
        return result.get("muted", False)

    async def set_source_mute(self, source_name: str, muted: bool) -> bool:
        return api.set_source_muted(source_name, muted)

    # --- Filters ---

    async def get_filters(self, source_name: str) -> List[Dict[str, Any]]:
        """Get filters on a source. Currently returns empty list as the C++ bridge doesn't expose filter enumeration yet."""
        return []

    async def add_filter(
        self,
        source_name: str,
        filter_name: str,
        filter_kind: str,
        filter_settings: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Add a filter to a source. Not yet implemented in the C++ bridge."""
        return False

    async def remove_filter(self, source_name: str, filter_name: str) -> bool:
        """Remove a filter from a source. Not yet implemented in the C++ bridge."""
        return False

    # --- Stats ---

    async def get_stats(self) -> Dict[str, Any]:
        stats = api.get_stats()
        if stats is None:
            return {}
        return {
            "activeFps": 0.0,  # Not directly available via frontend API
            "cpuUsage": 0.0,
            "memoryUsage": 0,
            "renderMissedFrames": 0,
            "renderTotalFrames": 0,
            "outputSkippedFrames": stats.get("streaming_skipped_frames", 0),
            "outputTotalFrames": stats.get("streaming_total_frames", 0),
        }


def _db_to_linear(db: float) -> float:
    """Convert decibels to linear volume (0.0-1.0)."""
    if db <= -100.0:
        return 0.0
    return 10.0 ** (db / 20.0)


def _linear_to_db(linear: float) -> float:
    """Convert linear volume to decibels."""
    import math
    if linear <= 0.0:
        return -100.0
    return 20.0 * math.log10(linear)
