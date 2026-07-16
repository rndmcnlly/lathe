// Design system for Lathe explainer video

import { loadFont } from "@remotion/google-fonts/Inter";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";

const { fontFamily: interFamily } = loadFont("normal", {
  weights: ["400", "600", "700", "800"],
  subsets: ["latin"],
});

const { fontFamily: monoFamily } = loadMono("normal", {
  weights: ["400", "600"],
  subsets: ["latin"],
});

export const FONTS = {
  body: interFamily,
  mono: monoFamily,
};

export const CANVAS = {
  width: 1920,
  height: 1080,
  fps: 30,
};

// Dark, industrial, precise — Lathe's visual identity
export const COLORS = {
  bg: "#0a0a0f",
  bgSubtle: "#12121a",
  bgCard: "#161622",
  text: "#e8e8ec",
  textMuted: "#8888a0",
  accent: "#5b8def",
  accentMuted: "#3a5c9e",
  highlight: "#f0c040",
  terminal: "#1a1a28",
  terminalBorder: "#2a2a3a",
  code: "#c8d0e0",
  green: "#4ecf8b",
  orange: "#cf8b4e",
  red: "#e05533",
  purple: "#9b6def",
};

export const PART_THEMES = {
  "spin-up": {
    accent: "#5b8def",
    accentMuted: "#3a5c9e",
    bgTint: "#0a0d14",
    label: "Spin Up",
  },
  "work": {
    accent: "#4ecf8b",
    accentMuted: "#2d7b52",
    bgTint: "#0a140d",
    label: "Get to Work",
  },
  "spin-down": {
    accent: "#cf8b4e",
    accentMuted: "#7b5a2d",
    bgTint: "#140f0a",
    label: "Spin Down",
  },
};

export type PartId = keyof typeof PART_THEMES;

// Timing constants (in frames at 30fps)
export const TIMING = {
  SLIDE_LEAD_IN: 8,
  SLIDE_TAIL: 10,
  SECTION_LEAD_IN: 16,
  SECTION_TAIL: 20,
};
