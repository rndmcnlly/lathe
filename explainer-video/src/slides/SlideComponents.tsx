// Visual components for each slide type

import React from "react";
import {
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  spring,
  Easing,
  OffthreadVideo,
  staticFile,
} from "remotion";
import { COLORS, FONTS, PART_THEMES, type PartId } from "../design";

// ── Title Slide ──────────────────────────────────────────────────

type TitleProps = { title: string; subtitle?: string; partId: PartId };

export const TitleSlide: React.FC<TitleProps> = ({ title, subtitle, partId }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const theme = PART_THEMES[partId];

  const isStill = durationInFrames === 1;
  const titleProgress = isStill ? 1 : spring({ frame, fps, config: { damping: 200 } });
  const subtitleProgress = isStill
    ? 1
    : spring({ frame: frame - 8, fps, config: { damping: 200 } });

  const titleY = interpolate(titleProgress, [0, 1], [40, 0]);
  const subtitleY = interpolate(subtitleProgress, [0, 1], [30, 0]);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 24,
      }}
    >
      {/* Decorative line */}
      <div
        style={{
          width: interpolate(titleProgress, [0, 1], [0, 120]),
          height: 3,
          backgroundColor: theme.accent,
          borderRadius: 2,
          marginBottom: 16,
        }}
      />
      <div
        style={{
          fontSize: 96,
          fontWeight: 800,
          color: COLORS.text,
          opacity: titleProgress,
          transform: `translateY(${titleY}px)`,
          letterSpacing: "-0.02em",
        }}
      >
        {title}
      </div>
      {subtitle && (
        <div
          style={{
            fontSize: 36,
            fontWeight: 400,
            color: COLORS.textMuted,
            opacity: Math.max(0, subtitleProgress),
            transform: `translateY(${subtitleY}px)`,
          }}
        >
          {subtitle}
        </div>
      )}
    </div>
  );
};

// ── Headline Slide ───────────────────────────────────────────────

type HeadlineProps = { text: string; partId: PartId };

export const HeadlineSlide: React.FC<HeadlineProps> = ({ text, partId }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const theme = PART_THEMES[partId];
  const isStill = durationInFrames === 1;

  const lines = text.split("\n");

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "flex-start",
        gap: 16,
        width: "100%",
        maxWidth: 1400,
      }}
    >
      {lines.map((line, i) => {
        const progress = isStill
          ? 1
          : spring({ frame: frame - i * 6, fps, config: { damping: 200 } });
        const x = interpolate(progress, [0, 1], [-60, 0]);

        return (
          <div
            key={i}
            style={{
              fontSize: 72,
              fontWeight: 700,
              lineHeight: 1.15,
              color: i === 0 ? COLORS.text : theme.accent,
              opacity: Math.max(0, progress),
              transform: `translateX(${x}px)`,
            }}
          >
            {line}
          </div>
        );
      })}
      {/* Accent underline */}
      <div
        style={{
          width: isStill ? 80 : interpolate(
            spring({ frame: frame - lines.length * 6, fps, config: { damping: 200 } }),
            [0, 1],
            [0, 80]
          ),
          height: 4,
          backgroundColor: theme.accent,
          borderRadius: 2,
          marginTop: 8,
        }}
      />
    </div>
  );
};

// ── Bullets Slide ────────────────────────────────────────────────

type BulletsProps = { items: string[]; partId: PartId };

export const BulletsSlide: React.FC<BulletsProps> = ({ items, partId }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const theme = PART_THEMES[partId];
  const isStill = durationInFrames === 1;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 28,
        width: "100%",
        maxWidth: 1400,
      }}
    >
      {items.map((item, i) => {
        const delay = 4 + i * 8;
        const progress = isStill
          ? 1
          : spring({ frame: frame - delay, fps, config: { damping: 200 } });
        const x = interpolate(progress, [0, 1], [40, 0]);

        return (
          <div
            key={i}
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: 20,
              opacity: Math.max(0, progress),
              transform: `translateX(${x}px)`,
            }}
          >
            {/* Bullet dot */}
            <div
              style={{
                width: 12,
                height: 12,
                borderRadius: 6,
                backgroundColor: theme.accent,
                marginTop: 14,
                flexShrink: 0,
              }}
            />
            <div
              style={{
                fontSize: 36,
                fontWeight: 400,
                lineHeight: 1.4,
                color: COLORS.text,
              }}
            >
              {item}
            </div>
          </div>
        );
      })}
    </div>
  );
};

// ── Text Slide ───────────────────────────────────────────────────

type TextProps = { body: string; partId: PartId };

export const TextSlide: React.FC<TextProps> = ({ body, partId }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const isStill = durationInFrames === 1;
  const progress = isStill ? 1 : spring({ frame, fps, config: { damping: 200 } });

  return (
    <div
      style={{
        maxWidth: 1200,
        opacity: progress,
      }}
    >
      <div
        style={{
          fontSize: 34,
          fontWeight: 400,
          lineHeight: 1.6,
          color: COLORS.text,
        }}
      >
        {body}
      </div>
    </div>
  );
};

// ── Tool Grid Slide ──────────────────────────────────────────────

type ToolGridProps = {
  tools: { name: string; desc: string; icon: string }[];
  partId: PartId;
};

// SVG icons for each tool (minimal, monochrome)
const TOOL_ICONS: Record<string, React.FC<{ color: string; size: number }>> = {
  terminal: ({ color, size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  ),
  file: ({ color, size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="16" y1="13" x2="8" y2="13" />
      <line x1="16" y1="17" x2="8" y2="17" />
    </svg>
  ),
  pencil: ({ color, size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
    </svg>
  ),
  diff: ({ color, size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="5" x2="12" y2="11" />
      <line x1="9" y1="8" x2="15" y2="8" />
      <line x1="9" y1="16" x2="15" y2="16" />
    </svg>
  ),
  globe: ({ color, size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <line x1="2" y1="12" x2="22" y2="12" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
  ),
  download: ({ color, size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  ),
  book: ({ color, size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
    </svg>
  ),
  help: ({ color, size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  ),
  trash: ({ color, size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    </svg>
  ),
  server: ({ color, size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="2" width="20" height="8" rx="2" ry="2" />
      <rect x="2" y="14" width="20" height="8" rx="2" ry="2" />
      <line x1="6" y1="6" x2="6.01" y2="6" />
      <line x1="6" y1="18" x2="6.01" y2="18" />
    </svg>
  ),
  shield: ({ color, size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
      <polyline points="9 12 11 14 15 10" />
    </svg>
  ),
};

// Narration-synced highlight cues for the tool grid.
// Each entry: [startFraction, endFraction, ...toolNames]
// Fractions are relative to the audio portion of the slide (after lead-in).
// Derived from the narration text timing for p1-tool-grid (22.4s audio).
//
// "bash runs shell commands — install packages, compile, test, manage git repos."  0.00–0.22
// "read, write, and edit handle file operations..."                                 0.22–0.42
// "expose gives the user a public URL for any running service."                     0.42–0.58
// "fetch bypasses sandbox egress restrictions via the server."                      0.58–0.72
// "onboard loads project context from the repo."                                    0.72–0.85
// "And destroy wipes the sandbox to start fresh."                                   0.85–1.00
const TOOL_CUES: [number, number, string[]][] = [
  [0.00, 0.22, ["bash"]],
  [0.22, 0.42, ["read", "write", "edit"]],
  [0.42, 0.58, ["expose"]],
  [0.58, 0.72, ["fetch"]],
  [0.72, 0.85, ["onboard"]],
  [0.85, 1.00, ["destroy"]],
];

export const ToolGridSlide: React.FC<ToolGridProps> = ({ tools, partId }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const theme = PART_THEMES[partId];
  const isStill = durationInFrames === 1;

  // Lead-in is 8 frames (TIMING.SLIDE_LEAD_IN). Audio starts after that.
  // The tail is 10 frames. Audio duration is total - lead - tail.
  const leadIn = 8;
  const tail = 10;
  const audioDuration = durationInFrames - leadIn - tail;
  const audioFrame = frame - leadIn; // frame relative to audio start
  const audioFrac = audioDuration > 0 ? audioFrame / audioDuration : 0;

  // Determine which tools are currently highlighted
  const activeTools = new Set<string>();
  if (!isStill && audioFrame >= 0) {
    for (const [start, end, names] of TOOL_CUES) {
      if (audioFrac >= start && audioFrac < end) {
        for (const n of names) activeTools.add(n);
      }
    }
  }

  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 20,
        justifyContent: "center",
        width: "100%",
        maxWidth: 1500,
      }}
    >
      {tools.map((tool, i) => {
        const delay = 2 + i * 3;
        const entranceProgress = isStill
          ? 1
          : spring({ frame: frame - delay, fps, config: { damping: 200 } });

        const isActive = activeTools.has(tool.name);
        const hasAnyCue = activeTools.size > 0;

        // Pulse: active cards scale up + brighten, inactive cards dim
        const pulseScale = isActive
          ? 1.06
          : hasAnyCue
            ? 0.97
            : 1;
        const pulseOpacity = isActive
          ? 1
          : hasAnyCue
            ? 0.45
            : 1;

        // Smooth the pulse with spring when transitioning
        const entranceScale = interpolate(entranceProgress, [0, 1], [0.85, 1]);
        const finalScale = entranceScale * pulseScale;

        const IconComponent = TOOL_ICONS[tool.icon];

        // Active border glow
        const borderColor = isActive
          ? `${theme.accent}cc`
          : `${COLORS.terminalBorder}`;
        const bgColor = isActive
          ? `${COLORS.bgCard}`
          : `${COLORS.bgCard}cc`;
        const shadowStyle = isActive
          ? `0 0 24px ${theme.accent}44, 0 0 48px ${theme.accent}22`
          : "none";

        return (
          <div
            key={tool.name}
            style={{
              width: 460,
              padding: "28px 32px",
              borderRadius: 16,
              backgroundColor: bgColor,
              border: `2px solid ${borderColor}`,
              boxShadow: shadowStyle,
              opacity: Math.max(0, entranceProgress) * pulseOpacity,
              transform: `scale(${finalScale})`,
              display: "flex",
              alignItems: "center",
              gap: 20,
              transition: "none", // no CSS transitions — all frame-driven
            }}
          >
            {IconComponent && (
              <div style={{ flexShrink: 0 }}>
                <IconComponent
                  color={isActive ? COLORS.highlight : theme.accent}
                  size={isActive ? 36 : 32}
                />
              </div>
            )}
            <div>
              <div
                style={{
                  fontSize: isActive ? 30 : 28,
                  fontWeight: 700,
                  fontFamily: FONTS.mono,
                  color: isActive ? COLORS.highlight : theme.accent,
                  marginBottom: 4,
                }}
              >
                {tool.name}
              </div>
              <div
                style={{
                  fontSize: 20,
                  color: isActive ? COLORS.text : COLORS.textMuted,
                }}
              >
                {tool.desc}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
};

// ── Code Block Slide ─────────────────────────────────────────────

type CodeBlockProps = { code: string; language: string; caption?: string; partId: PartId };

export const CodeBlockSlide: React.FC<CodeBlockProps> = ({
  code,
  caption,
  partId,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const theme = PART_THEMES[partId];
  const isStill = durationInFrames === 1;

  const progress = isStill ? 1 : spring({ frame, fps, config: { damping: 200 } });
  const lines = code.split("\n");

  return (
    <div
      style={{
        width: "100%",
        maxWidth: 1400,
        display: "flex",
        flexDirection: "column",
        gap: 20,
      }}
    >
      {caption && (
        <div
          style={{
            fontSize: 24,
            fontWeight: 600,
            color: COLORS.textMuted,
            opacity: progress,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
          }}
        >
          {caption}
        </div>
      )}
      <div
        style={{
          backgroundColor: COLORS.terminal,
          border: `1px solid ${COLORS.terminalBorder}`,
          borderRadius: 16,
          padding: "36px 40px",
          opacity: progress,
          transform: `translateY(${interpolate(progress, [0, 1], [20, 0])}px)`,
        }}
      >
        {/* Terminal dots */}
        <div style={{ display: "flex", gap: 8, marginBottom: 24 }}>
          <div style={{ width: 12, height: 12, borderRadius: 6, backgroundColor: "#ff5f56" }} />
          <div style={{ width: 12, height: 12, borderRadius: 6, backgroundColor: "#ffbd2e" }} />
          <div style={{ width: 12, height: 12, borderRadius: 6, backgroundColor: "#27c93f" }} />
        </div>
        {/* Code lines with staggered reveal */}
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {lines.map((line, i) => {
            const lineProgress = isStill
              ? 1
              : interpolate(frame, [6 + i * 2, 12 + i * 2], [0, 1], {
                  extrapolateLeft: "clamp",
                  extrapolateRight: "clamp",
                  easing: Easing.out(Easing.quad),
                });

            return (
              <div
                key={i}
                style={{
                  fontFamily: FONTS.mono,
                  fontSize: 24,
                  lineHeight: 1.6,
                  color: COLORS.code,
                  opacity: lineProgress,
                  whiteSpace: "pre",
                }}
              >
                {line}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};

// ── Two Column Slide ─────────────────────────────────────────────

type TwoColumnProps = {
  left: { heading: string; items: string[] };
  right: { heading: string; items: string[] };
  partId: PartId;
};

export const TwoColumnSlide: React.FC<TwoColumnProps> = ({
  left,
  right,
  partId,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const theme = PART_THEMES[partId];
  const isStill = durationInFrames === 1;

  const renderColumn = (
    col: { heading: string; items: string[] },
    baseDelay: number
  ) => {
    const headingProgress = isStill
      ? 1
      : spring({ frame: frame - baseDelay, fps, config: { damping: 200 } });

    return (
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 24 }}>
        <div
          style={{
            fontSize: 32,
            fontWeight: 700,
            color: theme.accent,
            opacity: Math.max(0, headingProgress),
            borderBottom: `2px solid ${theme.accentMuted}`,
            paddingBottom: 12,
          }}
        >
          {col.heading}
        </div>
        {col.items.map((item, i) => {
          const itemProgress = isStill
            ? 1
            : spring({
                frame: frame - baseDelay - 6 - i * 6,
                fps,
                config: { damping: 200 },
              });
          return (
            <div
              key={i}
              style={{
                fontSize: 28,
                lineHeight: 1.5,
                color: COLORS.text,
                opacity: Math.max(0, itemProgress),
                paddingLeft: 20,
                borderLeft: `3px solid ${theme.accentMuted}44`,
              }}
            >
              {item}
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div
      style={{
        display: "flex",
        gap: 80,
        width: "100%",
        maxWidth: 1400,
      }}
    >
      {renderColumn(left, 4)}
      {renderColumn(right, 10)}
    </div>
  );
};

// ── Callout Slide ────────────────────────────────────────────────

type CalloutProps = {
  icon: string;
  heading: string;
  body: string;
  partId: PartId;
};

export const CalloutSlide: React.FC<CalloutProps> = ({
  icon,
  heading,
  body,
  partId,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const theme = PART_THEMES[partId];
  const isStill = durationInFrames === 1;

  const progress = isStill ? 1 : spring({ frame, fps, config: { damping: 200 } });
  const scale = interpolate(progress, [0, 1], [0.92, 1]);
  const IconComponent = TOOL_ICONS[icon];

  return (
    <div
      style={{
        maxWidth: 1100,
        padding: "60px 72px",
        borderRadius: 24,
        backgroundColor: `${COLORS.bgCard}ee`,
        border: `2px solid ${theme.accentMuted}66`,
        opacity: progress,
        transform: `scale(${scale})`,
        display: "flex",
        flexDirection: "column",
        gap: 24,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 20 }}>
        {IconComponent && <IconComponent color={theme.accent} size={48} />}
        <div
          style={{
            fontSize: 48,
            fontWeight: 700,
            color: COLORS.text,
          }}
        >
          {heading}
        </div>
      </div>
      <div
        style={{
          fontSize: 30,
          lineHeight: 1.6,
          color: COLORS.textMuted,
        }}
      >
        {body}
      </div>
    </div>
  );
};

// ── Chat Slide ───────────────────────────────────────────────────

type ChatProps = {
  messages: { role: "user" | "agent" | "tool"; text: string }[];
  partId: PartId;
};

export const ChatSlide: React.FC<ChatProps> = ({ messages, partId }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const theme = PART_THEMES[partId];
  const isStill = durationInFrames === 1;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 28,
        width: "100%",
        maxWidth: 1200,
      }}
    >
      {messages.map((msg, i) => {
        const delay = i * 15;
        const progress = isStill
          ? 1
          : spring({ frame: frame - delay, fps, config: { damping: 200 } });
        const y = interpolate(progress, [0, 1], [30, 0]);

        const isUser = msg.role === "user";
        const bubbleColor = isUser ? `${COLORS.bgCard}` : `${theme.accent}18`;
        const borderColor = isUser
          ? COLORS.terminalBorder
          : `${theme.accent}44`;
        const align = isUser ? "flex-start" : "flex-end";
        const textColor = isUser ? COLORS.text : COLORS.text;
        const label = isUser ? "You" : "Agent";
        const labelColor = isUser ? COLORS.textMuted : theme.accent;

        return (
          <div
            key={i}
            style={{
              alignSelf: align,
              maxWidth: "85%",
              opacity: Math.max(0, progress),
              transform: `translateY(${y}px)`,
            }}
          >
            {/* Role label */}
            <div
              style={{
                fontSize: 16,
                fontWeight: 600,
                color: labelColor,
                marginBottom: 8,
                textTransform: "uppercase",
                letterSpacing: "0.08em",
                textAlign: isUser ? "left" : "right",
              }}
            >
              {label}
            </div>
            {/* Bubble */}
            <div
              style={{
                padding: "24px 32px",
                borderRadius: isUser
                  ? "20px 20px 20px 4px"
                  : "20px 20px 4px 20px",
                backgroundColor: bubbleColor,
                border: `1px solid ${borderColor}`,
              }}
            >
              <div
                style={{
                  fontSize: 30,
                  lineHeight: 1.5,
                  color: textColor,
                }}
              >
                {msg.text}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
};

// ── Tool Call Slide ──────────────────────────────────────────────

type ToolCallProps = {
  tool: string;
  args: string;
  result: string;
  icon: string;
  partId: PartId;
};

export const ToolCallSlide: React.FC<ToolCallProps> = ({
  tool,
  args,
  result,
  icon,
  partId,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const theme = PART_THEMES[partId];
  const isStill = durationInFrames === 1;

  const headerProgress = isStill
    ? 1
    : spring({ frame, fps, config: { damping: 200 } });
  const resultProgress = isStill
    ? 1
    : spring({ frame: frame - 12, fps, config: { damping: 200 } });

  const IconComponent = TOOL_ICONS[icon];

  return (
    <div
      style={{
        width: "100%",
        maxWidth: 1300,
        display: "flex",
        flexDirection: "column",
        gap: 0,
      }}
    >
      {/* Tool call header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 16,
          padding: "20px 32px",
          borderRadius: "16px 16px 0 0",
          backgroundColor: `${theme.accent}22`,
          border: `1px solid ${theme.accent}44`,
          borderBottom: "none",
          opacity: headerProgress,
          transform: `translateY(${interpolate(headerProgress, [0, 1], [15, 0])}px)`,
        }}
      >
        {IconComponent && <IconComponent color={theme.accent} size={28} />}
        <div
          style={{
            fontSize: 24,
            fontFamily: FONTS.mono,
            fontWeight: 600,
            color: theme.accent,
          }}
        >
          {tool}({args})
        </div>
      </div>

      {/* Result body */}
      <div
        style={{
          padding: "28px 36px",
          borderRadius: "0 0 16px 16px",
          backgroundColor: COLORS.terminal,
          border: `1px solid ${COLORS.terminalBorder}`,
          borderTop: `1px solid ${theme.accent}33`,
          opacity: Math.max(0, resultProgress),
          transform: `translateY(${interpolate(resultProgress, [0, 1], [10, 0])}px)`,
        }}
      >
        {result.split("\n").map((line, i) => {
          const lineProgress = isStill
            ? 1
            : interpolate(frame, [16 + i * 2, 22 + i * 2], [0, 1], {
                extrapolateLeft: "clamp",
                extrapolateRight: "clamp",
                easing: Easing.out(Easing.quad),
              });

          return (
            <div
              key={i}
              style={{
                fontFamily: FONTS.mono,
                fontSize: 22,
                lineHeight: 1.7,
                color: line.startsWith("✓") ? COLORS.green : COLORS.code,
                opacity: lineProgress,
                whiteSpace: "pre-wrap",
              }}
            >
              {line}
            </div>
          );
        })}
      </div>
    </div>
  );
};

// ── Scene Split Slide ────────────────────────────────────────────

type SceneProps = {
  leftLabel: string;
  leftLines: string[];
  rightLabel: string;
  rightLines: string[];
  partId: PartId;
};

export const SceneSlide: React.FC<SceneProps> = ({
  leftLabel,
  leftLines,
  rightLabel,
  rightLines,
  partId,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const theme = PART_THEMES[partId];
  const isStill = durationInFrames === 1;

  const renderPanel = (
    label: string,
    lines: string[],
    baseDelay: number,
    panelColor: string
  ) => {
    const labelProgress = isStill
      ? 1
      : spring({ frame: frame - baseDelay, fps, config: { damping: 200 } });

    return (
      <div
        style={{
          flex: 1,
          borderRadius: 16,
          backgroundColor: `${COLORS.bgCard}dd`,
          border: `1px solid ${COLORS.terminalBorder}`,
          overflow: "hidden",
          opacity: Math.max(0, labelProgress),
          transform: `translateY(${interpolate(labelProgress, [0, 1], [20, 0])}px)`,
        }}
      >
        {/* Panel header */}
        <div
          style={{
            padding: "16px 28px",
            backgroundColor: `${panelColor}22`,
            borderBottom: `1px solid ${panelColor}33`,
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div
            style={{
              width: 10,
              height: 10,
              borderRadius: 5,
              backgroundColor: panelColor,
            }}
          />
          <div
            style={{
              fontSize: 18,
              fontWeight: 700,
              color: panelColor,
              textTransform: "uppercase",
              letterSpacing: "0.06em",
            }}
          >
            {label}
          </div>
        </div>
        {/* Panel body */}
        <div style={{ padding: "20px 28px", display: "flex", flexDirection: "column", gap: 14 }}>
          {lines.map((line, i) => {
            const lineDelay = baseDelay + 6 + i * 6;
            const lineProgress = isStill
              ? 1
              : spring({
                  frame: frame - lineDelay,
                  fps,
                  config: { damping: 200 },
                });
            const isPath = line.startsWith("  ") || line.startsWith("/");

            return (
              <div
                key={i}
                style={{
                  fontSize: isPath ? 22 : 24,
                  lineHeight: 1.5,
                  fontFamily: isPath ? FONTS.mono : FONTS.body,
                  color: line.includes("←")
                    ? COLORS.highlight
                    : isPath
                      ? COLORS.code
                      : COLORS.text,
                  opacity: Math.max(0, lineProgress),
                  whiteSpace: "pre",
                }}
              >
                {line}
              </div>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div
      style={{
        display: "flex",
        gap: 32,
        width: "100%",
        maxWidth: 1500,
      }}
    >
      {renderPanel(leftLabel, leftLines, 4, theme.accent)}
      {renderPanel(rightLabel, rightLines, 10, COLORS.green)}
    </div>
  );
};

// ── Inspiration Slide ─────────────────────────────────────────────
// Side-by-side video clips of terminal coding agents (Pi, OpenCode)

type InspirationProps = {
  clips: { label: string; file: string; url: string }[];
  tagline: string;
  partId: PartId;
};

export const InspirationSlide: React.FC<InspirationProps> = ({
  clips,
  tagline,
  partId,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const theme = PART_THEMES[partId];
  const isStill = durationInFrames === 1;

  const taglineProgress = isStill
    ? 1
    : spring({ frame: frame - 15, fps, config: { damping: 200 } });

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 36,
        width: "100%",
        maxWidth: 1600,
      }}
    >
      {/* Clip panels */}
      <div style={{ display: "flex", gap: 32, width: "100%" }}>
        {clips.map((clip, i) => {
          const delay = 2 + i * 8;
          const progress = isStill
            ? 1
            : spring({ frame: frame - delay, fps, config: { damping: 200 } });
          const y = interpolate(progress, [0, 1], [30, 0]);

          return (
            <div
              key={clip.label}
              style={{
                flex: 1,
                borderRadius: 16,
                overflow: "hidden",
                border: `1px solid ${COLORS.terminalBorder}`,
                backgroundColor: COLORS.terminal,
                opacity: Math.max(0, progress),
                transform: `translateY(${y}px)`,
              }}
            >
              {/* Label bar */}
              <div
                style={{
                  padding: "10px 20px",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  borderBottom: `1px solid ${COLORS.terminalBorder}`,
                  backgroundColor: `${theme.accent}11`,
                }}
              >
                <div
                  style={{
                    fontSize: 22,
                    fontWeight: 700,
                    color: theme.accent,
                    fontFamily: FONTS.mono,
                  }}
                >
                  {clip.label}
                </div>
                <div
                  style={{
                    fontSize: 16,
                    color: COLORS.textMuted,
                    fontFamily: FONTS.mono,
                  }}
                >
                  {clip.url}
                </div>
              </div>
              {/* Video */}
              <div style={{ position: "relative", width: "100%" }}>
                <OffthreadVideo
                  src={staticFile(`images/${clip.file}`)}
                  playbackRate={0.5}
                  style={{ width: "100%", display: "block" }}
                  muted
                />
              </div>
            </div>
          );
        })}
      </div>
      {/* Tagline */}
      <div
        style={{
          fontSize: 30,
          fontWeight: 400,
          color: COLORS.textMuted,
          opacity: Math.max(0, taglineProgress),
          transform: `translateY(${interpolate(taglineProgress, [0, 1], [20, 0])}px)`,
          textAlign: "center",
          lineHeight: 1.5,
        }}
      >
        {tagline}
      </div>
    </div>
  );
};

// ── Conversation Slide ────────────────────────────────────────────
// A conversational wrapper: user bubble → compact tool card → agent bubble.
// Shifts visual emphasis from tool output to the chat experience.

type ConversationProps = {
  userMessage: string;
  agentReply: string;
  // Compact tool call (optional — some conversations are CodeBlock-based)
  toolName?: string;
  toolArgs?: string;
  toolResultPreview?: string;  // first few lines of result, truncated
  toolIcon?: string;
  partId: PartId;
};

export const ConversationSlide: React.FC<ConversationProps> = ({
  userMessage,
  agentReply,
  toolName,
  toolArgs,
  toolResultPreview,
  toolIcon,
  partId,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const theme = PART_THEMES[partId];
  const isStill = durationInFrames === 1;

  // Staggered entrance: user → tool → agent
  const userProgress = isStill
    ? 1
    : spring({ frame, fps, config: { damping: 200 } });
  const toolProgress = isStill
    ? 1
    : spring({ frame: frame - 15, fps, config: { damping: 200 } });
  const agentProgress = isStill
    ? 1
    : spring({ frame: frame - 30, fps, config: { damping: 200 } });

  const IconComponent = toolIcon ? TOOL_ICONS[toolIcon] : null;
  const hasTool = toolName != null;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: hasTool ? 16 : 28,
        width: "100%",
        maxWidth: 1400,
      }}
    >
      {/* User bubble */}
      <div
        style={{
          alignSelf: "flex-start",
          maxWidth: "80%",
          opacity: Math.max(0, userProgress),
          transform: `translateY(${interpolate(userProgress, [0, 1], [20, 0])}px)`,
        }}
      >
        <div
          style={{
            fontSize: 14,
            fontWeight: 600,
            color: COLORS.textMuted,
            marginBottom: 6,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
          }}
        >
          You
        </div>
        <div
          style={{
            padding: "16px 24px",
            borderRadius: "16px 16px 16px 4px",
            backgroundColor: COLORS.bgCard,
            border: `1px solid ${COLORS.terminalBorder}`,
          }}
        >
          <div
            style={{
              fontSize: 24,
              lineHeight: 1.4,
              color: COLORS.text,
            }}
          >
            {userMessage}
          </div>
        </div>
      </div>

      {/* Compact tool card (if present) */}
      {hasTool && (
        <div
          style={{
            alignSelf: "center",
            width: "90%",
            opacity: Math.max(0, toolProgress),
            transform: `translateY(${interpolate(toolProgress, [0, 1], [15, 0])}px)`,
          }}
        >
          {/* Tool header */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "10px 20px",
              borderRadius: toolResultPreview ? "10px 10px 0 0" : "10px",
              backgroundColor: `${theme.accent}18`,
              border: `1px solid ${theme.accent}33`,
              borderBottom: toolResultPreview ? "none" : undefined,
            }}
          >
            {IconComponent && <IconComponent color={theme.accent} size={18} />}
            <div
              style={{
                fontSize: 17,
                fontFamily: FONTS.mono,
                fontWeight: 600,
                color: theme.accent,
              }}
            >
              {toolName}({toolArgs || ""})
            </div>
          </div>
          {/* Compact result preview */}
          {toolResultPreview && (
            <div
              style={{
                padding: "12px 20px",
                borderRadius: "0 0 10px 10px",
                backgroundColor: `${COLORS.terminal}cc`,
                border: `1px solid ${COLORS.terminalBorder}`,
                borderTop: `1px solid ${theme.accent}22`,
              }}
            >
              {toolResultPreview.split("\n").slice(0, 4).map((line, i) => (
                <div
                  key={i}
                  style={{
                    fontFamily: FONTS.mono,
                    fontSize: 16,
                    lineHeight: 1.5,
                    color: line.startsWith("✓") ? COLORS.green : COLORS.code,
                    opacity: 0.8,
                    whiteSpace: "pre",
                    overflow: "hidden",
                  }}
                >
                  {line}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Agent bubble */}
      <div
        style={{
          alignSelf: "flex-end",
          maxWidth: "80%",
          opacity: Math.max(0, agentProgress),
          transform: `translateY(${interpolate(agentProgress, [0, 1], [20, 0])}px)`,
        }}
      >
        <div
          style={{
            fontSize: 14,
            fontWeight: 600,
            color: theme.accent,
            marginBottom: 6,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            textAlign: "right",
          }}
        >
          Agent
        </div>
        <div
          style={{
            padding: "16px 24px",
            borderRadius: "16px 16px 4px 16px",
            backgroundColor: `${theme.accent}18`,
            border: `1px solid ${theme.accent}33`,
          }}
        >
          <div
            style={{
              fontSize: 24,
              lineHeight: 1.4,
              color: COLORS.text,
            }}
          >
            {agentReply}
          </div>
        </div>
      </div>
    </div>
  );
};

// ── Demo Clip Slide ───────────────────────────────────────────────
// Shows a segment of the demo video starting at startSeconds, filling
// the full slide area. The video plays for however long the slide lasts.

type DemoClipProps = {
  file: string;
  startSeconds: number;
};

export const DemoClipSlide: React.FC<DemoClipProps> = ({ file, startSeconds }) => {
  const { fps } = useVideoConfig();
  const startFromFrame = Math.round(startSeconds * fps);

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        overflow: "hidden",
        borderRadius: 0,
      }}
    >
      <OffthreadVideo
        src={staticFile(`images/${file}`)}
        startFrom={startFromFrame}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
        }}
        muted
      />
    </div>
  );
};

// Re-export TOOL_ICONS for use in other components
export { TOOL_ICONS };
