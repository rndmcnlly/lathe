// Structured progress bar across the top of the video
// Shows Part > Slide nesting with current position highlighted

import React from "react";
import { useCurrentFrame, useVideoConfig, interpolate, Easing } from "remotion";
import { COLORS, FONTS, PART_THEMES } from "./design";
import { script, extractTimeline } from "./data/script";
import manifestData from "./data/manifest.json";

const manifest = manifestData as Record<string, { file: string; durationMs: number }>;

// ── Component ─────────────────────────────────────────────────────

const BAR_TOP = 16;
const BAR_HEIGHT = 6;
const BAR_MARGIN_X = 60;
const LABEL_HEIGHT = 20;
const TOTAL_HEIGHT = BAR_TOP + LABEL_HEIGHT + BAR_HEIGHT + 12;
const PART_GAP = 6;
const SLIDE_GAP = 2;

export const ProgressBar: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames, width } = useVideoConfig();

  // Don't render in stills
  if (durationInFrames === 1) return null;

  const { parts, totalFrames } = extractTimeline(script, manifest, fps);
  const barWidth = width - BAR_MARGIN_X * 2;

  // Fade in over first second, stay visible
  const opacity = interpolate(frame, [0, 30], [0, 1], {
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.quad),
  });

  // Find current part and slide
  let currentPartIndex = 0;
  let currentSlideIndex = 0;
  for (const part of parts) {
    for (const slide of part.slides) {
      if (frame >= slide.globalStart && frame < slide.globalStart + slide.duration) {
        currentPartIndex = slide.partIndex;
        currentSlideIndex = slide.slideIndex;
      }
    }
  }

  // Total gap space to subtract from available width
  const totalPartGaps = (parts.length - 1) * PART_GAP;
  const usableWidth = barWidth - totalPartGaps;

  return (
    <div
      style={{
        position: "absolute",
        top: 0,
        left: 0,
        right: 0,
        height: TOTAL_HEIGHT,
        opacity,
        zIndex: 100,
      }}
    >
      {/* Scrim behind the bar for readability */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          height: TOTAL_HEIGHT + 16,
          background: `linear-gradient(to bottom, ${COLORS.bg}cc, ${COLORS.bg}00)`,
        }}
      />

      {/* Parts */}
      <div
        style={{
          position: "absolute",
          top: BAR_TOP,
          left: BAR_MARGIN_X,
          width: barWidth,
          display: "flex",
          gap: PART_GAP,
        }}
      >
        {parts.map((part, pi) => {
          const partFrac = part.duration / totalFrames;
          const partWidth = usableWidth * partFrac;
          const theme = PART_THEMES[part.partId as keyof typeof PART_THEMES];
          const isCurrentPart = pi === currentPartIndex;

          // Gap space for slides within this part
          const slideGaps = (part.slides.length - 1) * SLIDE_GAP;
          const slideUsable = partWidth - slideGaps;

          return (
            <div key={part.partId} style={{ width: partWidth }}>
              {/* Act label */}
              <div
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  fontFamily: FONTS.body,
                  color: isCurrentPart ? theme.accent : `${COLORS.textMuted}88`,
                  marginBottom: 4,
                  letterSpacing: "0.06em",
                  textTransform: "uppercase",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                }}
              >
                {part.label}
              </div>

              {/* Slide segments */}
              <div
                style={{
                  display: "flex",
                  gap: SLIDE_GAP,
                  height: BAR_HEIGHT,
                }}
              >
                {part.slides.map((slide, si) => {
                  const slideFrac = slide.duration / part.duration;
                  const slideWidth = slideUsable * slideFrac;

                  const isCurrent = pi === currentPartIndex && si === currentSlideIndex;
                  const isPast =
                    pi < currentPartIndex ||
                    (pi === currentPartIndex && si < currentSlideIndex);

                  // Progress within current slide
                  const slideProgress = isCurrent
                    ? Math.min(1, (frame - slide.globalStart) / slide.duration)
                    : isPast
                      ? 1
                      : 0;

                  return (
                    <div
                      key={slide.id}
                      style={{
                        width: slideWidth,
                        height: BAR_HEIGHT,
                        borderRadius: BAR_HEIGHT / 2,
                        backgroundColor: `${theme.accent}22`,
                        overflow: "hidden",
                        position: "relative",
                      }}
                    >
                      {/* Fill */}
                      <div
                        style={{
                          position: "absolute",
                          top: 0,
                          left: 0,
                          width: `${slideProgress * 100}%`,
                          height: "100%",
                          borderRadius: BAR_HEIGHT / 2,
                          backgroundColor: isCurrent
                            ? theme.accent
                            : isPast
                              ? `${theme.accent}88`
                              : "transparent",
                          boxShadow: isCurrent
                            ? `0 0 8px ${theme.accent}66`
                            : "none",
                        }}
                      />
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
