import React from "react";
import { Composition, AbsoluteFill } from "remotion";
import { CANVAS } from "./design";
import { script, toComposition, getTotalFrames } from "./data/script";
import { ProgressBar } from "./ProgressBar";
import manifestData from "./data/manifest.json";

const manifest = manifestData as Record<string, { file: string; durationMs: number }>;

const LatheExplainer: React.FC = () => {
  const { element } = toComposition(script, manifest, CANVAS.fps);

  return (
    <AbsoluteFill>
      {element}
      <ProgressBar />
    </AbsoluteFill>
  );
};

export const RemotionRoot: React.FC = () => {
  const totalFrames = getTotalFrames(script, manifest, CANVAS.fps);

  return (
    <Composition
      id="LatheExplainer"
      component={LatheExplainer}
      durationInFrames={totalFrames}
      fps={CANVAS.fps}
      width={CANVAS.width}
      height={CANVAS.height}
    />
  );
};
