"use strict";
const video = document.querySelector("#video");
const play = document.querySelector("#play");
const airplay = document.querySelector("#airplay");
const reconnect = document.querySelector("#reconnect");
const state = document.querySelector("#state");
const message = document.querySelector("#message");
const key = new URL(location.href).searchParams.get("key");
const streamURL = new URL("/live.m3u8", location.href);
if (key) streamURL.searchParams.set("key", key);
let connecting = false;

function note(text, error = false) {
  message.textContent = text;
  message.classList.toggle("error", error);
}

async function connect() {
  if (connecting) return;
  if (!video.canPlayType("application/vnd.apple.mpegurl")) {
    state.textContent = "Safari required";
    play.textContent = "Open in Safari";
    note("Use iPhone, iPad, or Mac Safari for this player. You can also open the stream URL in VLC.", true);
    return;
  }
  connecting = true;
  play.disabled = true;
  reconnect.disabled = true;
  play.textContent = "Connecting…";
  state.textContent = "Connecting…";
  note("Finding a live connection. The first connection may take a few minutes.");
  video.pause();
  video.removeAttribute("src");
  video.load();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 330000);
  try {
    const response = await fetch(streamURL, {cache: "no-store", signal: controller.signal});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `Connection failed (HTTP ${response.status}).`);
    }
    const manifest = await response.text();
    if (!manifest.trimStart().startsWith("#EXTM3U")) throw new Error("The server did not return a playable stream.");
    video.src = streamURL.href;
    play.disabled = false;
    play.textContent = "Watch live";
    state.textContent = "Ready to play";
    note("Tap Watch live. For the big screen, choose your Apple TV using AirPlay.");
  } catch (error) {
    state.textContent = "Connection unavailable";
    play.textContent = "Unavailable";
    note(error.name === "AbortError" ? "The connection timed out. Check the server, then tap Reconnect." : error.message, true);
  } finally {
    clearTimeout(timer);
    connecting = false;
    reconnect.disabled = false;
  }
}

play.addEventListener("click", () => {
  if (!video.paused) { video.pause(); return; }
  video.play().catch(() => note("Tap the video's own play control to start playback.", true));
});
reconnect.addEventListener("click", connect);
if (typeof video.webkitShowPlaybackTargetPicker === "function") {
  airplay.hidden = false;
  airplay.addEventListener("click", () => video.webkitShowPlaybackTargetPicker());
}
video.addEventListener("playing", () => {
  state.textContent = video.webkitCurrentPlaybackTargetIsWireless ? "Playing on AirPlay" : "Live";
  play.textContent = "Pause";
  note("You’re watching 五星体育 live.");
});
video.addEventListener("pause", () => {
  if (!connecting) { state.textContent = "Paused"; play.textContent = "Watch live"; }
});
video.addEventListener("waiting", () => { if (!connecting) state.textContent = "Buffering…"; });
video.addEventListener("webkitcurrentplaybacktargetiswirelesschanged", () => {
  state.textContent = video.webkitCurrentPlaybackTargetIsWireless ? "Playing on AirPlay" : "Ready on this device";
});
video.addEventListener("error", () => {
  if (connecting) return;
  state.textContent = "Playback interrupted";
  play.textContent = "Watch live";
  note("Tap Reconnect to reload the stream. If this happens only on Apple TV, check that it can reach your server’s LAN address.", true);
});
connect();
