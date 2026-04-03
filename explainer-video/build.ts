/**
 * build.ts — Single entry point for the video pipeline.
 *
 * 1. Imports the script (SSOT)
 * 2. Renders TTS audio for any new/changed narrations
 * 3. Writes manifest.json
 * 4. Shells out to Remotion to render the MP4
 *
 * Usage:
 *   npx tsx build.ts              # build (cached TTS)
 *   npx tsx build.ts --force-tts  # re-render all TTS audio
 *   npx tsx build.ts --tts-only   # just render TTS, skip video
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from "fs";
import { createHash } from "crypto";
import { join, dirname } from "path";
import { execSync } from "child_process";
import { parseBuffer } from "music-metadata";
import { script, extractNarrations, extractTimeline } from "./src/data/script";
import { CANVAS, TIMING } from "./src/design";

// ── Config ────────────────────────────────────────────────────────

const VOICE_ID = "1f7zwaddjtlht0nw02oa"; // Adam's cloned voice
const MODEL = "ResembleAI/chatterbox-turbo";
const API_URL = "https://api.deepinfra.com/v1/openai/audio/speech";
const TIMEOUT_MS = 90_000;
const MAX_RETRIES = 3;

const ROOT = dirname(import.meta.url.replace("file://", ""));
const AUDIO_DIR = join(ROOT, "public", "audio");
const MANIFEST_PATHS = [
  join(AUDIO_DIR, "manifest.json"),
  join(ROOT, "src", "data", "manifest.json"),
];
const CACHE_PATH = join(AUDIO_DIR, ".narration-hashes.json");
const OUTPUT = join(ROOT, "out", "lathe-explainer.mp4");

// ── Helpers ───────────────────────────────────────────────────────

type CacheHashes = Record<string, string>;
type ManifestEntry = { file: string; durationMs: number };

function hash(text: string): string {
  return createHash("sha256").update(text).digest("hex").slice(0, 16);
}

function loadCache(): CacheHashes {
  return existsSync(CACHE_PATH) ? JSON.parse(readFileSync(CACHE_PATH, "utf-8")) : {};
}

function getToken(): string {
  const p = join(process.env.HOME || "~", ".tokens", "deepinfra");
  return readFileSync(p, "utf-8").trim();
}

// ── TTS normalization ─────────────────────────────────────────────

function normalizeForTTS(text: string): string {
  let t = text;
  const swaps: [string, string][] = [
    ["OWUI", "O W U I"], ["HTTPS", "H T T P S"], ["HTTP", "H T T P"],
    ["API", "A P I"], ["SSH", "S S H"], ["VM", "V M"], ["IDE", "I D E"],
    ["URL", "U R L"], ["CLI", "C L I"], ["CSV", "C S V"], ["CSS", "C S S"],
    ["TLS", "T L S"], ["TTS", "T T S"], ["CI", "C I"], ["KB", "kilobytes"],
    ["AGENTS.md", "agents dot M D"], ["httpx", "H T T P X"],
  ];
  for (const [from, to] of swaps) t = t.replaceAll(from, to);
  t = t.replaceAll("Lathe", "lathe");
  t = t.replaceAll("Daytona", "Day tona");
  t = t.replaceAll(" — ", ", ").replaceAll("—", ", ");
  return t;
}

// ── TTS rendering (sequential — simpler, cache makes it fast) ────

async function renderTTS(force: boolean): Promise<Record<string, ManifestEntry>> {
  const token = getToken();
  const cache = loadCache();
  const narrations = extractNarrations(script);
  const manifest: Record<string, ManifestEntry> = {};
  const newCache: CacheHashes = {};
  let rendered = 0;

  console.log(`\n── TTS (${narrations.length} slides) ──\n`);

  for (const { id, narration } of narrations) {
    const filename = `${id}.mp3`;
    const filepath = join(AUDIO_DIR, filename);
    const h = hash(narration);
    newCache[id] = h;

    // Cache hit
    if (!force && existsSync(filepath) && cache[id] === h) {
      const buf = readFileSync(filepath);
      const meta = await parseBuffer(buf, { mimeType: "audio/mpeg" });
      const durationMs = Math.round((meta.format.duration ?? 0) * 1000);
      manifest[id] = { file: filename, durationMs };
      console.log(`  [cached] ${id} (${durationMs}ms)`);
      continue;
    }

    if (!force && existsSync(filepath)) {
      console.log(`  [stale] ${id} — narration changed`);
    }

    // Render
    const normalized = normalizeForTTS(narration);
    let success = false;

    for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

        const resp = await fetch(API_URL, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            model: MODEL,
            input: normalized,
            voice: VOICE_ID,
            response_format: "mp3",
          }),
          signal: controller.signal,
        });
        clearTimeout(timer);

        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);

        const buf = Buffer.from(await resp.arrayBuffer());
        writeFileSync(filepath, buf);

        const meta = await parseBuffer(buf, { mimeType: "audio/mpeg" });
        const durationMs = Math.round((meta.format.duration ?? 0) * 1000);
        manifest[id] = { file: filename, durationMs };
        console.log(`  [rendered] ${id} (${durationMs}ms)`);
        rendered++;
        success = true;
        break;
      } catch (err) {
        const wait = 2 ** (attempt + 1);
        console.log(`  [retry ${attempt + 1}/${MAX_RETRIES}] ${id}: ${err}, waiting ${wait}s`);
        await new Promise((r) => setTimeout(r, wait * 1000));
      }
    }

    if (!success) throw new Error(`Failed to render ${id} after ${MAX_RETRIES} retries`);
  }

  // Write manifest + cache
  const json = JSON.stringify(manifest, null, 2) + "\n";
  for (const p of MANIFEST_PATHS) {
    mkdirSync(dirname(p), { recursive: true });
    writeFileSync(p, json);
  }
  writeFileSync(CACHE_PATH, JSON.stringify(newCache, null, 2) + "\n");

  const totalMs = Object.values(manifest).reduce((s, e) => s + e.durationMs, 0);
  console.log(`\n  ${narrations.length} slides, ${rendered} rendered, ${(totalMs / 1000).toFixed(1)}s total`);

  return manifest;
}

// ── WebVTT generation ─────────────────────────────────────────────

function formatVTTTime(ms: number): string {
  const totalSec = ms / 1000;
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = Math.floor(totalSec % 60);
  const frac = Math.round(ms % 1000);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(frac).padStart(3, "0")}`;
}

function generateVTT(manifest: Record<string, ManifestEntry>): void {
  const fps = CANVAS.fps;
  const narrations = extractNarrations(script);
  const { parts } = extractTimeline(script, manifest, fps);
  const narrationMap = new Map(narrations.map((n) => [n.id, n.narration]));

  const lines: string[] = ["WEBVTT", ""];

  for (const part of parts) {
    for (const slide of part.slides) {
      const text = narrationMap.get(slide.id);
      if (!text) continue;
      const entry = manifest[slide.id];
      if (!entry) continue;

      const leadIn = slide.section && slide.slideIndex > 0
        ? TIMING.SECTION_LEAD_IN
        : TIMING.SLIDE_LEAD_IN;

      const startMs = ((slide.globalStart + leadIn) / fps) * 1000;
      const endMs = startMs + entry.durationMs;

      lines.push(slide.id);
      lines.push(`${formatVTTTime(startMs)} --> ${formatVTTTime(endMs)}`);
      lines.push(text);
      lines.push("");
    }
  }

  const vttPath = join(ROOT, "out", "lathe-explainer.vtt");
  writeFileSync(vttPath, lines.join("\n"));
  console.log(`  WebVTT: ${vttPath}`);
}

// ── Main ──────────────────────────────────────────────────────────

async function main() {
  const forceTTS = process.argv.includes("--force-tts");
  const ttsOnly = process.argv.includes("--tts-only");

  mkdirSync(AUDIO_DIR, { recursive: true });
  mkdirSync(join(ROOT, "out"), { recursive: true });

  const manifest = await renderTTS(forceTTS);

  console.log("\n── Generating WebVTT captions ──\n");
  generateVTT(manifest);

  if (ttsOnly) {
    console.log("\n── TTS only, skipping video render ──");
    return;
  }

  console.log("\n── Rendering video ──\n");
  execSync(
    `npx remotion render LatheExplainer ${OUTPUT} --codec h264`,
    { stdio: "inherit", cwd: ROOT },
  );
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
