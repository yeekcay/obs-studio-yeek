/*
 * StreamDirector Alerts - Frontend JavaScript
 * Connects to local WebSocket server (ws://localhost:9191)
 * Receives alert events and config from the Python Twitch EventSub backend
 */

const WS_URL = "ws://localhost:9191";
const DEFAULT_DURATION = 5000;

const eventQueue = [];
let showingAlert = false;
let ws = null;
let reconnectTimer = null;

/* Default config - overridden by backend config when connected */
let alertConfig = {};

function replaceTokens(str, data) {
  return str.replace(/\{(\w+)\}/g, (match, key) => {
    return data[key] !== undefined ? data[key] : "";
  });
}

function updateStatus(message, connected) {
  const overlay = document.getElementById("statusOverlay");
  const text = document.getElementById("statusText");
  if (!overlay || !text) return;
  text.textContent = message;
  if (connected) {
    overlay.classList.add("hidden");
  } else {
    overlay.classList.remove("hidden");
  }
}

function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  updateStatus("Connecting to alerts backend...", false);

  try {
    ws = new WebSocket(WS_URL);
  } catch (e) {
    updateStatus("Cannot connect to alerts backend", false);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    updateStatus("Connected - waiting for events", true);
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      handleEvent(msg);
    } catch (e) {
      console.error("Failed to parse event:", e);
    }
  };

  ws.onerror = () => {
    updateStatus("Connection error", false);
  };

  ws.onclose = () => {
    updateStatus("Disconnected from backend", false);
    ws = null;
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connectWebSocket, 3000);
}

function handleEvent(msg) {
  if (msg.type === "status") {
    updateStatus(msg.message, msg.connected === true);
    return;
  }

  if (msg.type === "config") {
    alertConfig = msg.data || {};
    console.log("Alert config loaded:", Object.keys(alertConfig).length, "event types");
    return;
  }

  if (msg.type === "alert") {
    addAlertToQueue(msg.data);
  }
}

function addAlertToQueue(data) {
  const eventType = data.event_type || "";
  const config = alertConfig[eventType];

  if (!config) {
    console.log("No config for event type:", eventType);
    return;
  }

  if (config.enabled === false) {
    console.log("Alert disabled for:", eventType);
    return;
  }

  const alertData = {
    ...data,
    title: replaceTokens(config.title || "Alert", data),
    message: replaceTokens(config.message || "", data),
    emoji: config.emoji || "🔔",
    color: config.color || "#ffffff",
    duration: config.duration || DEFAULT_DURATION,
    image: config.image || "",
    sound: config.sound || "",
    eventClass: eventType.split(".")[1] || "follow",
  };

  eventQueue.push(alertData);
  if (!showingAlert) {
    showNextAlert();
  }
}

function showNextAlert() {
  if (eventQueue.length === 0) {
    showingAlert = false;
    return;
  }

  showingAlert = true;
  const data = eventQueue.shift();

  const template = document.getElementById("alertTemplate");
  const clone = template.content.cloneNode(true);

  const alertEl = clone.querySelector(".alert");
  alertEl.classList.add(data.eventClass);

  const imgEl = clone.querySelector(".alert-image");
  if (data.image) {
    imgEl.src = data.image;
  } else if (data.emoji) {
    imgEl.src = createEmojiImage(data.emoji);
  } else {
    imgEl.style.display = "none";
  }

  const titleEl = clone.querySelector(".alert-title");
  titleEl.textContent = data.title;
  titleEl.style.color = data.color;

  const msgEl = clone.querySelector(".alert-message");
  msgEl.textContent = data.message;

  const container = document.getElementById("alertContainer");
  container.appendChild(clone);

  /* Play sound if configured */
  if (data.sound) {
    const audio = document.getElementById("alertSound");
    audio.src = data.sound;
    audio.play().catch(() => {});
  }

  const duration = data.duration || DEFAULT_DURATION;

  setTimeout(() => {
    alertEl.classList.remove("bounce-in");
    alertEl.classList.add("bounce-out");

    setTimeout(() => {
      alertEl.remove();
      showNextAlert();
    }, 600);
  }, duration);
}

function createEmojiImage(emoji) {
  const canvas = document.createElement("canvas");
  canvas.width = 120;
  canvas.height = 120;
  const ctx = canvas.getContext("2d");
  ctx.font = "80px sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(emoji, 60, 60);
  return canvas.toDataURL();
}

/* Start connecting immediately */
connectWebSocket();
