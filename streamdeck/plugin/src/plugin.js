// ─────────────────────────────────────────────────────────────────────────────
// PunchBuddy Stream-Deck-Plugin (Node.js)
//
// Verbindet sich beim Tastendruck mit dem LOKALEN Unix-Domain-Socket von
// PunchBuddy (Standard: /tmp/punchbuddy.sock) und sendet den ausgewählten
// Befehl. KEIN Netzwerk – kein TCP, kein Loopback, daher von Netzwerk-Filtern
// (Microsoft Defender) prinzipiell nicht erfassbar.
//
// Kommunikation mit der Stream-Deck-App läuft über das von Elgato dokumentierte
// WebSocket-Protokoll (Registrierung via Start-Argumente). Einzige externe
// Abhängigkeit: 'ws' (wird beim Build eingebündelt). 'net' ist Node-intern.
// ─────────────────────────────────────────────────────────────────────────────
"use strict";

const net = require("net");
const WebSocket = require("ws");

const DEFAULT_SOCK = "/tmp/punchbuddy.sock";

// ── Start-Argumente parsen ──────────────────────────────────────────────────
// Stream Deck startet das Plugin mit:
//   -port <p> -pluginUUID <uuid> -registerEvent <event> -info <json>
function argval(flag) {
  const i = process.argv.indexOf(flag);
  return i >= 0 ? process.argv[i + 1] : null;
}
const PORT = argval("-port");
const PLUGIN_UUID = argval("-pluginUUID");
const REGISTER_EVENT = argval("-registerEvent");

if (!PORT || !PLUGIN_UUID || !REGISTER_EVENT) {
  console.error("[PunchBuddy] Fehlende Stream-Deck-Startargumente – Abbruch.");
  process.exit(1);
}

// Letzte bekannte Settings je Tastenkontext (Fallback, falls keyDown sie nicht mitliefert).
const settingsByContext = Object.create(null);

const ws = new WebSocket("ws://127.0.0.1:" + PORT);

ws.on("open", () => {
  ws.send(JSON.stringify({ event: REGISTER_EVENT, uuid: PLUGIN_UUID }));
  console.log("[PunchBuddy] registriert.");
});

ws.on("message", (raw) => {
  let msg;
  try { msg = JSON.parse(raw.toString()); } catch { return; }
  const { event, context, payload } = msg;

  switch (event) {
    case "willAppear":
    case "didReceiveSettings":
      settingsByContext[context] = (payload && payload.settings) || {};
      break;
    case "willDisappear":
      delete settingsByContext[context];
      break;
    case "keyDown": {
      const s = (payload && payload.settings) || settingsByContext[context] || {};
      handleKey(context, s);
      break;
    }
    default:
      break;
  }
});

ws.on("error", (e) => console.error("[PunchBuddy] WS-Fehler:", e.message));
ws.on("close", () => process.exit(0));

// ── Befehl aus den Settings bauen ───────────────────────────────────────────
function buildLine(s) {
  let cmd = (s.command || "").trim();
  if (!cmd) return null;
  if (cmd === "preset") {
    const n = parseInt(s.presetArg, 10);
    if (!Number.isNaN(n)) cmd = "preset " + n;
  }
  return cmd;
}

// ── Tastendruck → Socket ────────────────────────────────────────────────────
function handleKey(context, s) {
  const line = buildLine(s);
  if (!line) { showAlert(context); return; }

  const sockPath = (s.socketPath && s.socketPath.trim()) || DEFAULT_SOCK;
  let resp = "";
  let settled = false;
  const finish = (ok) => {
    if (settled) return;
    settled = true;
    ok ? showOk(context) : showAlert(context);
  };

  const client = net.createConnection({ path: sockPath }, () => {
    client.write(line + "\n");
  });
  client.setTimeout(3000);
  client.on("data", (d) => { resp += d.toString(); });
  client.on("end", () => finish(resp.startsWith("OK")));
  client.on("close", () => finish(resp.startsWith("OK")));
  client.on("timeout", () => { client.destroy(); finish(false); });
  client.on("error", (e) => {
    console.error("[PunchBuddy] Socket-Fehler:", e.message);
    finish(false);
  });
}

// ── Rückmeldung an die Taste ────────────────────────────────────────────────
function send(obj) { try { ws.send(JSON.stringify(obj)); } catch { /* ignore */ } }
function showOk(context) { send({ event: "showOk", context }); }
function showAlert(context) { send({ event: "showAlert", context }); }
