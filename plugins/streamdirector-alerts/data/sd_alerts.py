#!/usr/bin/env python3
"""
StreamDirector Alerts Backend
Connects to Twitch EventSub WebSocket and forwards events to the local alerts page
via a WebSocket server on localhost:9191.

Usage:
    python sd_alerts.py --token <oauth_token>

The token should be a Twitch OAuth user token with eventsub read scopes.
Get one with: twitch token eventsub  (requires twitch-cli)
Or generate one at https://twitchtokengenerator.com with scopes:
    channel:read:redemptions user:read:follows channel:read:subscriptions channel:read:raids
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time
import logging
import urllib.request
import urllib.parse

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("sd-alerts")

# WebSocket server for the alerts HTML page
ALERTS_WS_PORT = 9191

# Twitch EventSub WebSocket URL
TWITCH_EVENTSUB_WS = "wss://eventsub.wss.twitch.tv/ws"

# Twitch OAuth scopes needed
TWITCH_SCOPES = [
    "moderator:read:followers",
    "channel:read:redemptions",
    "channel:read:subscriptions",
    "bits:read",
    "channel:manage:raids",
]

# Token file path
def get_token_file_path():
    base = os.path.join(os.environ.get("APPDATA", ""), "streamdirector", "streamdirector-alerts")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "twitch_tokens.json")

def load_tokens():
    path = get_token_file_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_tokens(access_token, refresh_token):
    path = get_token_file_path()
    data = {"access_token": access_token, "refresh_token": refresh_token}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

def get_client_credentials():
    """Read client_id and client_secret from twitch-cli config."""
    possible_paths = [
        os.path.join(os.environ.get("APPDATA", ""), "twitch-cli", ".twitch-cli.env"),
        os.path.join(os.environ.get("USERPROFILE", ""), ".config", "twitch-cli", "twitch-cli.env"),
        os.path.join(os.environ.get("HOME", ""), ".config", "twitch-cli", "twitch-cli.env"),
    ]
    config_path = None
    for p in possible_paths:
        if os.path.exists(p):
            config_path = p
            break
    if not config_path:
        log.error(f"twitch-cli config not found in: {possible_paths}")
        return None, None
    try:
        client_id = None
        client_secret = None
        with open(config_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("CLIENTID="):
                    client_id = line.split("=", 1)[1].strip()
                elif line.startswith("CLIENTSECRET="):
                    client_secret = line.split("=", 1)[1].strip()
                elif line.startswith("clientid="):
                    client_id = line.split("=", 1)[1].strip()
                elif line.startswith("clientsecret="):
                    client_secret = line.split("=", 1)[1].strip()
        if client_id and client_secret:
            log.info(f"Loaded client credentials from {config_path}")
            return client_id, client_secret
        log.error(f"Could not parse client_id/secret from {config_path}")
        return None, None
    except Exception as e:
        log.error(f"Error reading twitch-cli config: {e}")
        return None, None

async def device_code_login():
    """Run Twitch device code flow to get user access token."""
    client_id, client_secret = get_client_credentials()
    if not client_id:
        log.error("No client_id found. Configure twitch-cli first: twitch-cli.exe configure")
        return None, None

    scopes_str = " ".join(TWITCH_SCOPES)

    # Request device code
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "scopes": scopes_str,
    }).encode()

    try:
        req = urllib.request.Request(
            "https://id.twitch.tv/oauth2/device",
            data=data,
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        log.error(f"Device code request failed: {e}")
        return None, None

    device_code = result.get("device_code", "")
    user_code = result.get("user_code", "")
    verify_uri = result.get("verification_uri", "https://www.twitch.tv/activate")
    interval = result.get("interval", 5)
    expires_in = result.get("expires_in", 1800)

    log.info(f"\n{'='*50}")
    log.info(f"Go to: {verify_uri}")
    log.info(f"Enter code: {user_code}")
    log.info(f"{'='*50}\n")
    log.info(f"Waiting for authorization (expires in {expires_in}s)...")

    # Poll for token
    deadline = time.time() + expires_in
    while time.time() < deadline:
        await asyncio.sleep(interval)

        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }).encode()

        try:
            req = urllib.request.Request(
                "https://id.twitch.tv/oauth2/token",
                data=data,
                method="POST"
            )
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                access_token = result.get("access_token", "")
                refresh_token = result.get("refresh_token", "")
                if access_token:
                    log.info("Login successful!")
                    save_tokens(access_token, refresh_token)
                    return access_token, refresh_token
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                error_data = json.loads(body)
            except Exception:
                error_data = {}
            error_code = error_data.get("status", e.code)
            error_message = error_data.get("message", body[:100])
            if "pending" in error_message:
                # User hasn't authorized yet, keep polling
                pass
            elif error_message == "slow_down":
                interval += 5
                log.warning("Rate limited, slowing down...")
            elif error_code == 400 and error_message == "Device code expired.":
                log.error("Device code expired.")
                return None, None
            elif error_code == 400:
                log.error(f"Bad request: {error_message}")
                return None, None
            else:
                log.warning(f"Polling: {error_code} - {error_message}")
        except Exception as e:
            log.warning(f"Polling error: {e}")

    log.error("Login timed out.")
    return None, None

def refresh_access_token(refresh_token):
    """Refresh an expired access token using the refresh token."""
    client_id, client_secret = get_client_credentials()
    if not client_id or not refresh_token:
        return None

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()

    try:
        req = urllib.request.Request(
            "https://id.twitch.tv/oauth2/token",
            data=data,
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            new_access = result.get("access_token", "")
            new_refresh = result.get("refresh_token", refresh_token)
            if new_access:
                save_tokens(new_access, new_refresh)
                log.info("Token refreshed successfully.")
                return new_access
    except Exception as e:
        log.error(f"Token refresh failed: {e}")
    return None

# Event types we subscribe to
SUBSCRIBED_EVENTS = {
    "channel.follow": "2",
    "channel.cheer": "1",
    "channel.subscribe": "1",
    "channel.subscription.message": "1",
    "channel.subscription.gift": "1",
    "channel.raid": "1",
    "channel.channel_points_custom_reward_redemption.add": "1",
}


class AlertsBridge:
    """Bridges Twitch EventSub events to the local alerts WebSocket clients."""

    def __init__(self, token: str, config_path: str = "", refresh_token: str = ""):
        self.token = token
        self.refresh_token = refresh_token
        self.twitch_ws = None
        self.server_clients = set()
        self.session_id = None
        self.running = True
        self.reconnect_delay = 1
        self.config_path = config_path
        self.alert_config = {}
        self.load_config()

    async def broadcast(self, message: dict):
        """Send a message to all connected alerts page clients."""
        data = json.dumps(message)
        dead = set()
        for ws in self.server_clients:
            try:
                await ws.send(data)
            except Exception:
                dead.add(ws)
        self.server_clients -= dead

    def load_config(self):
        """Load alert configuration from JSON file."""
        # Try user config dir first, then plugin data dir
        config_paths = []
        if self.config_path:
            config_paths.append(self.config_path)
        config_paths.append(os.path.join(os.environ.get("APPDATA", ""), "streamdirector", "streamdirector-alerts", "alerts_config.json"))
        # Default config in plugin data dir
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_paths.append(os.path.join(script_dir, "alerts_config.json"))

        for path in config_paths:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        full_config = json.load(f)
                    self.alert_config = full_config.get("alerts", {})
                    log.info(f"Loaded alert config from {path} ({len(self.alert_config)} event types)")
                    return
                except Exception as e:
                    log.warning(f"Failed to load config from {path}: {e}")

        log.warning("No alerts_config.json found - using empty config")
        self.alert_config = {}

    async def handle_server_client(self, ws, path=None):
        """Handle a new alerts page client connection."""
        self.server_clients.add(ws)
        log.info(f"Alerts page client connected ({len(self.server_clients)} total)")
        # Send config to the page
        await ws.send(json.dumps({"type": "config", "data": self.alert_config}))
        await self.broadcast({"type": "status", "message": "Connected to Twitch EventSub", "connected": True})
        try:
            async for msg in ws:
                try:
                    data = json.loads(msg)
                    if data.get("type") == "alert":
                        # Reload config to pick up any changes from the Configure tab
                        self.load_config()
                        # Re-send config to all clients so they use the latest
                        await self.broadcast({"type": "config", "data": self.alert_config})
                        # Forward the test alert to all clients
                        await self.broadcast(data)
                        log.info(f"Test alert forwarded: {data.get('data', {}).get('event_type', 'unknown')}")
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self.server_clients.discard(ws)
            log.info(f"Alerts page client disconnected ({len(self.server_clients)} total)")

    async def connect_twitch(self):
        """Connect to Twitch EventSub WebSocket and maintain the connection."""
        import websockets

        while self.running:
            try:
                log.info(f"Connecting to Twitch EventSub WebSocket: {TWITCH_EVENTSUB_WS}")
                async with websockets.connect(TWITCH_EVENTSUB_WS) as ws:
                    self.twitch_ws = ws
                    self.reconnect_delay = 1
                    log.info("Connected to Twitch EventSub!")

                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self.handle_twitch_message(msg)
                        except json.JSONDecodeError:
                            log.warning(f"Failed to parse Twitch message: {raw[:200]}")
                        except Exception as e:
                            log.error(f"Error handling Twitch message: {e}")

            except Exception as e:
                log.error(f"Twitch WebSocket error: {e}")

            self.twitch_ws = None
            self.session_id = None

            if self.running:
                log.info(f"Reconnecting to Twitch in {self.reconnect_delay}s...")
                await self.broadcast({"type": "status", "message": "Reconnecting to Twitch...", "connected": False})
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, 30)

    async def handle_twitch_message(self, msg: dict):
        """Handle a message from Twitch EventSub WebSocket."""
        msg_type = msg.get("metadata", {}).get("message_type", "")

        if msg_type == "session_welcome":
            self.session_id = msg.get("payload", {}).get("session", {}).get("id", "")
            log.info(f"Twitch EventSub session established: {self.session_id[:16]}...")
            await self.subscribe_to_events()
            await self.broadcast({"type": "status", "message": "Connected to Twitch EventSub", "connected": True})

        elif msg_type == "session_keepalive":
            pass  # Keepalive, no action needed

        elif msg_type == "session_reconnect":
            log.info("Twitch requested reconnect...")
            # The reconnect URL is in the payload
            reconnect_url = msg.get("payload", {}).get("session", {}).get("reconnect_url", "")
            if reconnect_url:
                log.info(f"Reconnect URL provided: {reconnect_url[:50]}...")

        elif msg_type == "notification":
            payload = msg.get("payload", {})
            event = payload.get("event", {})
            subscription = payload.get("subscription", {})
            event_type = subscription.get("type", "")

            log.info(f"Event received: {event_type}")
            await self.process_event(event_type, event)

        elif msg_type == "revocation":
            log.warning("Subscription revoked! Token may be invalid.")
            await self.broadcast({"type": "status", "message": "Token revoked - check your OAuth token", "connected": False})

        else:
            log.debug(f"Unknown Twitch message type: {msg_type}")

    async def subscribe_to_events(self):
        """Subscribe to events via Twitch EventSub REST API."""
        if not self.session_id:
            return

        import aiohttp

        # First validate the token to get the correct Client ID and user ID
        validate_headers = {
            "Authorization": f"Bearer {self.token}",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://id.twitch.tv/oauth2/validate", headers=validate_headers) as resp:
                    if resp.status != 200:
                        log.error(f"Token validation failed: {resp.status}")
                        await self.broadcast({"type": "status", "message": "Invalid OAuth token", "connected": False})
                        return
                    data = await resp.json()
                    user_id = data.get("user_id", "")
                    client_id = data.get("client_id", "")
                    log.info(f"Token valid for user_id: {user_id}, client_id: {client_id}")

                # Now use the correct Client ID from the token
                headers = {
                    "Authorization": f"Bearer {self.token}",
                    "Client-Id": client_id,
                    "Content-Type": "application/json",
                }

                # Subscribe to each event type
                for event_type, version in SUBSCRIBED_EVENTS.items():
                    sub_body = {
                        "type": event_type,
                        "version": version,
                        "condition": self.get_condition(event_type, user_id),
                        "transport": {
                            "method": "websocket",
                            "session_id": self.session_id,
                        },
                    }

                    try:
                        async with session.post(
                            "https://api.twitch.tv/helix/eventsub/subscriptions",
                            headers=headers,
                            json=sub_body,
                        ) as resp:
                            if resp.status in (200, 202):
                                log.info(f"Subscribed to {event_type}")
                            elif resp.status == 409:
                                log.info(f"Already subscribed to {event_type}")
                            else:
                                text = await resp.text()
                                log.warning(f"Failed to subscribe to {event_type}: {resp.status} {text[:200]}")
                    except Exception as e:
                        log.error(f"Error subscribing to {event_type}: {e}")

                    await asyncio.sleep(0.2)  # Rate limit

        except Exception as e:
            log.error(f"Error during subscription: {e}")

    def get_condition(self, event_type: str, user_id: str) -> dict:
        """Get the condition for a given event type."""
        if event_type == "channel.raid":
            return {"to_broadcaster_user_id": user_id}
        elif event_type == "channel.follow":
            return {"broadcaster_user_id": user_id, "moderator_user_id": user_id}
        elif event_type == "channel.subscription.gift":
            return {"broadcaster_user_id": user_id}
        elif event_type == "channel.subscription.message":
            return {"broadcaster_user_id": user_id}
        elif event_type == "channel.channel_points_custom_reward_redemption.add":
            return {"broadcaster_user_id": user_id}
        else:
            return {"broadcaster_user_id": user_id}

    async def process_event(self, event_type: str, event: dict):
        """Process a Twitch event and forward it to the alerts page."""
        # Normalize the event data for the frontend
        alert_data = {"event_type": event_type}

        if event_type == "channel.follow":
            alert_data.update({
                "user_name": event.get("user_name", ""),
                "followed_at": event.get("followed_at", ""),
            })

        elif event_type == "channel.cheer":
            alert_data.update({
                "user_name": event.get("user_name", ""),
                "bits": event.get("bits", 0),
                "message": event.get("message", ""),
            })

        elif event_type == "channel.subscribe":
            alert_data.update({
                "user_name": event.get("user_name", ""),
                "tier": event.get("tier", ""),
                "is_gift": event.get("is_gift", False),
            })

        elif event_type == "channel.subscription.message":
            alert_data.update({
                "user_name": event.get("user_name", ""),
                "cumulative_months": event.get("cumulative_months", 0),
                "streak_months": event.get("streak_months", 0),
                "message": event.get("message", {}).get("text", ""),
            })

        elif event_type == "channel.subscription.gift":
            alert_data.update({
                "user_name": event.get("user_name", ""),
                "gift_count": event.get("gift_count", 1),
                "cumulative_total": event.get("cumulative_total", 0),
            })

        elif event_type == "channel.raid":
            alert_data.update({
                "from_broadcaster_user_name": event.get("from_broadcaster_user_name", ""),
                "viewers": event.get("viewers", 0),
            })

        elif event_type == "channel.channel_points_custom_reward_redemption.add":
            alert_data.update({
                "user_name": event.get("user_name", ""),
                "reward_title": event.get("reward", {}).get("title", ""),
                "user_input": event.get("user_input", ""),
            })

        await self.broadcast({"type": "alert", "data": alert_data})
        log.info(f"Alert sent: {event_type} -> {json.dumps(alert_data)[:200]}")

    async def auto_refresh_token(self):
        """Auto-refresh the access token every 45 minutes."""
        REFRESH_INTERVAL = 45 * 60  # 45 minutes
        while self.running:
            await asyncio.sleep(REFRESH_INTERVAL)
            if not self.running:
                break
            log.info("Auto-refreshing Twitch token...")
            new_token = refresh_access_token(self.refresh_token)
            if new_token:
                self.token = new_token
                log.info("Token auto-refreshed. Will use new token on next Twitch reconnect.")
            else:
                log.error("Auto-refresh failed. Token may expire soon.")

    async def run(self):
        """Run both the WebSocket server and Twitch EventSub client."""
        import websockets

        # Kill any stale process holding our port
        import subprocess
        try:
            result = subprocess.run(
                ["netstat", "-aon"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if ":9191" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1]
                    log.info(f"Killing stale process on port 9191 (PID {pid})...")
                    subprocess.run(["taskkill", "/f", "/pid", pid],
                                   capture_output=True, timeout=5)
                    await asyncio.sleep(1)
        except Exception as e:
            log.warning(f"Port cleanup attempt: {e}")

        # Start local WebSocket server for the alerts page (with retry)
        server = None
        for attempt in range(5):
            try:
                server = await websockets.serve(
                    self.handle_server_client,
                    "127.0.0.1",
                    ALERTS_WS_PORT,
                    ping_interval=20,
                    ping_timeout=10,
                )
                break
            except OSError as e:
                if attempt < 4:
                    log.warning(f"Port {ALERTS_WS_PORT} in use, retrying in 1s (attempt {attempt+1}/5)...")
                    await asyncio.sleep(1)
                else:
                    log.error(f"Could not bind to port {ALERTS_WS_PORT} after 5 attempts: {e}")
                    raise

        log.info(f"Alerts WebSocket server listening on ws://localhost:{ALERTS_WS_PORT}")

        # Start Twitch EventSub connection
        twitch_task = asyncio.create_task(self.connect_twitch())

        # Start token auto-refresh task (refresh every 45 minutes)
        if self.refresh_token:
            refresh_task = asyncio.create_task(self.auto_refresh_token())
        else:
            refresh_task = None
            log.warning("No refresh token - token will expire and need manual re-login")

        # Wait for shutdown signal
        while self.running:
            await asyncio.sleep(1)

            # Check for stop file
            stop_file = os.path.join(os.environ.get("APPDATA", ""), "streamdirector", "streamdirector-alerts", "STOP")
            if os.path.exists(stop_file):
                log.info("Stop file detected, shutting down...")
                self.running = False
                break

        # Cleanup
        twitch_task.cancel()
        if refresh_task:
            refresh_task.cancel()
        server.close()
        await server.wait_closed()
        log.info("Alerts backend stopped.")


def main():
    parser = argparse.ArgumentParser(description="StreamDirector Alerts Backend")
    parser.add_argument("--token", default="", help="Twitch OAuth token")
    parser.add_argument("--refresh-token", default="", help="Twitch OAuth refresh token")
    parser.add_argument("--config", default="", help="Path to alerts_config.json")
    parser.add_argument("--login", action="store_true", help="Run device code flow login and exit")
    args = parser.parse_args()

    if args.login:
        log.info("StreamDirector Alerts - Twitch Login (Device Code Flow)")
        try:
            access, refresh = asyncio.run(device_code_login())
            if access:
                log.info("Login complete! Tokens saved to twitch_tokens.json")
                sys.exit(0)
            else:
                log.error("Login failed.")
                sys.exit(1)
        except Exception as e:
            log.error(f"Login error: {e}")
            sys.exit(1)

    if not args.token:
        # Try loading saved tokens
        tokens = load_tokens()
        args.token = tokens.get("access_token", "")
        args.refresh_token = args.refresh_token or tokens.get("refresh_token", "")
        if not args.token:
            log.error("No token provided. Use --login to authenticate, or --token to pass one.")
            sys.exit(1)

    log.info("StreamDirector Alerts Backend starting...")
    log.info(f"Token: {args.token[:8]}...{args.token[-4:]}")

    bridge = AlertsBridge(args.token, config_path=args.config, refresh_token=args.refresh_token)

    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        log.info("Interrupted, shutting down...")
    except Exception as e:
        log.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
