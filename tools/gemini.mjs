#!/usr/bin/env node
// Minimal Gemini API client for designing the BarBoards dashboard.
// API key: env GEMINI_API_KEY, or the file  tools/.gemini-key  (gitignored).
// Get a free key at https://aistudio.google.com/apikey
//
// Usage:
//   node gemini.mjs ping                         -> sanity-check the key
//   node gemini.mjs models                       -> list models your key can use (+ their methods)
//   node gemini.mjs ask "your prompt"            -> text answer (design ideas/critique)
//   node gemini.mjs image "your prompt" out.png  -> generate an image, save to out.png
// Optional: GEMINI_TEXT_MODEL / GEMINI_IMAGE_MODEL env vars override the defaults.

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const BASE = "https://generativelanguage.googleapis.com/v1beta/models";
const TEXT_MODEL = process.env.GEMINI_TEXT_MODEL || "gemini-2.5-flash";
const IMAGE_MODEL = process.env.GEMINI_IMAGE_MODEL || "gemini-2.5-flash-image";

function getKey() {
  if (process.env.GEMINI_API_KEY) return process.env.GEMINI_API_KEY.trim();
  const f = join(here, ".gemini-key");
  if (existsSync(f)) return readFileSync(f, "utf8").trim();
  console.error("No API key. Set env GEMINI_API_KEY or put the key in tools/.gemini-key\n(get one free at https://aistudio.google.com/apikey).");
  process.exit(2);
}
const KEY = getKey();

async function post(model, body) {
  const r = await fetch(`${BASE}/${model}:generateContent?key=${KEY}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, j };
}

async function ask(prompt) {
  const { ok, status, j } = await post(TEXT_MODEL, { contents: [{ parts: [{ text: prompt }] }] });
  if (!ok) { console.error(`Gemini error ${status}:`, JSON.stringify(j.error || j).slice(0, 600)); process.exitCode = 1; return; }
  const text = (j.candidates?.[0]?.content?.parts || []).map(p => p.text).filter(Boolean).join("\n");
  console.log(text || JSON.stringify(j).slice(0, 600));
}

async function image(prompt, out = "gemini-out.png") {
  const { ok, status, j } = await post(IMAGE_MODEL, {
    contents: [{ parts: [{ text: prompt }] }],
    generationConfig: { responseModalities: ["TEXT", "IMAGE"] },
  });
  if (!ok) { console.error(`Gemini image error ${status}:`, JSON.stringify(j.error || j).slice(0, 600)); process.exitCode = 1; return; }
  const parts = j.candidates?.[0]?.content?.parts || [];
  const img = parts.find(p => p.inlineData?.data);
  if (!img) { console.error("No image in response:", JSON.stringify(j).slice(0, 600)); process.exitCode = 1; return; }
  writeFileSync(out, Buffer.from(img.inlineData.data, "base64"));
  console.log(`saved ${out} (${img.inlineData.mimeType})`);
}

async function models() {
  const r = await fetch(`${BASE}?key=${KEY}`);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { console.error(`error ${r.status}:`, JSON.stringify(j.error || j).slice(0, 600)); process.exitCode = 1; return; }
  console.log((j.models || []).map(m => `${m.name}  [${(m.supportedGenerationMethods || []).join(",")}]`).join("\n"));
}

const [, , cmd, ...rest] = process.argv;
if (cmd === "ping") await ask("Reply with exactly the word: pong");
else if (cmd === "ask") await ask(rest.join(" "));
else if (cmd === "image") await image(rest[0], rest[1] || "gemini-out.png");
else if (cmd === "models") await models();
else { console.error("usage: node gemini.mjs ping | models | ask \"prompt\" | image \"prompt\" out.png"); process.exit(2); }
