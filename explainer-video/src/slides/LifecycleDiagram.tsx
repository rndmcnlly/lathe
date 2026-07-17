import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS, FONTS, PART_THEMES } from "../design";

const rows = [
  { label: "Filesystem", detail: "files, packages, Git history", stop: "remains", color: COLORS.green },
  { label: "Processes", detail: "servers, jobs, interpreter state", stop: "ends", color: PART_THEMES["spin-down"].accent },
];

export const LifecycleDiagram: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const isStill = durationInFrames === 1;
  const p = (delay: number) => isStill ? 1 : spring({ frame: frame - delay, fps, config: { damping: 200 } });
  const arrow = isStill ? 1 : interpolate(frame, [18, 40], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });

  return (
    <svg viewBox="0 0 1320 720" style={{ width: "100%", maxWidth: 1500 }}>
      <defs>
        <marker id="life-arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
          <polygon points="0 0, 10 4, 0 8" fill={COLORS.textMuted} />
        </marker>
      </defs>

      <text x="660" y="72" textAnchor="middle" fill={COLORS.text} fontFamily={FONTS.body} fontSize="40" fontWeight="700">What sleep preserves</text>

      {[
        { x: 90, title: "RUNNING", detail: "tools and services active", color: PART_THEMES.work.accent },
        { x: 505, title: "STOPPED / ARCHIVED", detail: "compute is off", color: PART_THEMES["spin-down"].accent },
        { x: 920, title: "LATER TOOL CALL", detail: "sandbox restarts", color: COLORS.green },
      ].map((state, i) => (
        <g key={state.title} opacity={Math.max(0, p(i * 8))}>
          <rect x={state.x} y="120" width="310" height="105" rx="18" fill={COLORS.bgCard} stroke={`${state.color}99`} strokeWidth="2" />
          <text x={state.x + 155} y="163" textAnchor="middle" fill={state.color} fontFamily={FONTS.body} fontSize="21" fontWeight="700">{state.title}</text>
          <text x={state.x + 155} y="194" textAnchor="middle" fill={COLORS.textMuted} fontFamily={FONTS.body} fontSize="17">{state.detail}</text>
        </g>
      ))}

      <line x1="400" y1="172" x2="505" y2="172" stroke={COLORS.textMuted} strokeWidth="3" markerEnd="url(#life-arrow)" opacity={arrow} />
      <line x1="815" y1="172" x2="920" y2="172" stroke={COLORS.textMuted} strokeWidth="3" markerEnd="url(#life-arrow)" opacity={arrow} />

      {rows.map((row, i) => (
        <g key={row.label} opacity={Math.max(0, p(30 + i * 8))}>
          <rect x="150" y={290 + i * 115} width="1020" height="88" rx="16" fill={`${COLORS.bgCard}dd`} stroke={COLORS.terminalBorder} />
          <text x="190" y={326 + i * 115} fill={row.color} fontFamily={FONTS.body} fontSize="24" fontWeight="700">{row.label}</text>
          <text x="190" y={354 + i * 115} fill={COLORS.textMuted} fontFamily={FONTS.body} fontSize="18">{row.detail}</text>
          <text x="660" y={343 + i * 115} textAnchor="middle" fill={row.color} fontFamily={FONTS.body} fontSize="27" fontWeight="700">{row.stop}</text>
          <text x="1110" y={343 + i * 115} textAnchor="end" fill={i === 0 ? COLORS.green : COLORS.highlight} fontFamily={FONTS.body} fontSize="20">{i === 0 ? "available after restart" : "must be restarted"}</text>
        </g>
      ))}

      <g opacity={Math.max(0, p(48))}>
        <line x1="245" y1="565" x2="1075" y2="565" stroke={`${COLORS.textMuted}66`} strokeWidth="2" strokeDasharray="8 7" />
        <text x="245" y="604" fill={COLORS.text} fontFamily={FONTS.body} fontSize="21" fontWeight="700">DELETION / DESTROY</text>
        <text x="490" y="604" fill={COLORS.textMuted} fontFamily={FONTS.body} fontSize="19">removes sandbox filesystem</text>
        <text x="1075" y="604" textAnchor="end" fill={COLORS.highlight} fontFamily={FONTS.body} fontSize="19">optional volume: separate boundary</text>
      </g>
    </svg>
  );
};
