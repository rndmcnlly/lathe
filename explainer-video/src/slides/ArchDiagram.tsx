import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS, FONTS } from "../design";

const boxes = [
  { x: 80, width: 250, title: "Browser", detail: "Open WebUI chat", color: COLORS.accent },
  { x: 420, width: 360, title: "Open WebUI server", detail: "admin-installed Lathe", color: COLORS.highlight },
  { x: 870, width: 370, title: "Daytona sandbox", detail: "commands + project files", color: COLORS.green },
];

export const ArchDiagram: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const isStill = durationInFrames === 1;
  const progress = boxes.map((_, i) => isStill ? 1 : spring({ frame: frame - i * 9, fps, config: { damping: 200 } }));
  const flow = isStill ? 1 : interpolate(frame, [24, 45], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });

  return (
    <svg viewBox="0 0 1320 720" style={{ width: "100%", maxWidth: 1500 }}>
      <defs>
        <marker id="arch-arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
          <polygon points="0 0, 10 4, 0 8" fill={COLORS.textMuted} />
        </marker>
      </defs>

      <text x="660" y="88" textAnchor="middle" fill={COLORS.text} fontFamily={FONTS.body} fontSize="42" fontWeight="700">
        Where a tool call goes
      </text>

      <rect x="390" y="155" width="880" height="360" rx="28" fill="none" stroke={`${COLORS.highlight}66`} strokeWidth="2" strokeDasharray="10 8" />
      <text x="420" y="190" fill={COLORS.highlight} fontFamily={FONTS.body} fontSize="18" fontWeight="700">
        ADMINISTRATOR-CONFIGURED SERVICE PATH
      </text>

      <line x1="330" y1="340" x2="420" y2="340" stroke={COLORS.textMuted} strokeWidth="3" markerEnd="url(#arch-arrow)" opacity={flow} />
      <line x1="780" y1="340" x2="870" y2="340" stroke={COLORS.textMuted} strokeWidth="3" markerEnd="url(#arch-arrow)" opacity={flow} />
      <text x="375" y="318" textAnchor="middle" fill={COLORS.textMuted} fontFamily={FONTS.mono} fontSize="16">tool call</text>
      <text x="825" y="318" textAnchor="middle" fill={COLORS.textMuted} fontFamily={FONTS.mono} fontSize="16">Daytona API</text>

      {boxes.map((box, i) => {
        const p = progress[i];
        return (
          <g key={box.title} opacity={Math.max(0, p)} transform={`translate(${box.x + box.width / 2}, 340) scale(${interpolate(p, [0, 1], [0.9, 1])}) translate(${-box.x - box.width / 2}, -340)`}>
            <rect x={box.x} y="245" width={box.width} height="190" rx="20" fill={COLORS.bgCard} stroke={`${box.color}aa`} strokeWidth="2" />
            <text x={box.x + box.width / 2} y="315" textAnchor="middle" fill={box.color} fontFamily={FONTS.body} fontSize="28" fontWeight="700">{box.title}</text>
            <text x={box.x + box.width / 2} y="352" textAnchor="middle" fill={COLORS.textMuted} fontFamily={FONTS.body} fontSize="19">{box.detail}</text>
            {i === 1 && <text x={box.x + box.width / 2} y="393" textAnchor="middle" fill={COLORS.code} fontFamily={FONTS.mono} fontSize="16">holds Daytona credential</text>}
            {i === 2 && <text x={box.x + box.width / 2} y="393" textAnchor="middle" fill={COLORS.code} fontFamily={FONTS.mono} fontSize="16">label: deployment=email</text>}
          </g>
        );
      })}

      <text x="660" y="590" textAnchor="middle" fill={COLORS.text} fontFamily={FONTS.body} fontSize="25">
        Requests flow through Open WebUI. Work executes in the sandbox.
      </text>
      <text x="660" y="628" textAnchor="middle" fill={COLORS.textMuted} fontFamily={FONTS.body} fontSize="20">
        The chat receives tool results, never the administrator's Daytona credential.
      </text>
    </svg>
  );
};
