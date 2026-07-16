import assert from "node:assert/strict";
import { script, extractTimeline, getTotalFrames, toComposition } from "./src/data/script";
import { CANVAS, TIMING } from "./src/design";

const manifest = {};
const timeline = extractTimeline(script, manifest, CANVAS.fps);
const composition = toComposition(script, manifest, CANVAS.fps);
const slides = timeline.parts.flatMap((part) => part.slides);

assert.equal(composition.totalFrames, timeline.totalFrames);
assert.equal(getTotalFrames(script, manifest, CANVAS.fps), timeline.totalFrames);
assert.equal(slides[0].leadIn, TIMING.SLIDE_LEAD_IN);

for (const slide of slides.slice(1)) {
  assert.equal(
    slide.leadIn,
    slide.section ? TIMING.SECTION_LEAD_IN : TIMING.SLIDE_LEAD_IN,
    `unexpected lead-in for ${slide.id}`,
  );
}

console.log(`Timing check passed: ${slides.length} slides, ${timeline.totalFrames} frames`);
