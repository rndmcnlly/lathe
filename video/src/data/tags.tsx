/**
 * Semantic JSX tags for authoring the video script.
 *
 * These are NOT rendering components — they exist for type-checked authoring.
 * Two walkers consume the element tree:
 *
 *   extractNarrations(tree)  → {id, narration}[]     (for TTS)
 *   toComposition(tree)      → Remotion <Sequence> tree  (for rendering)
 */

import React from "react";
import { Sequence, Audio, staticFile } from "remotion";
import type { PartId } from "../design";
import { TIMING, PART_THEMES } from "../design";
import { SlideContainer } from "../slides/SlideContainer";
import {
  TitleSlide,
  HeadlineSlide,
  BulletsSlide,
  TextSlide,
  CodeBlockSlide,
  CalloutSlide,
  ChatSlide,
  ToolCallSlide,
  SceneSlide,
  InspirationSlide,
} from "../slides/SlideComponents";
import { ArchDiagram } from "../slides/ArchDiagram";
import { LifecycleDiagram } from "../slides/LifecycleDiagram";

// ── JSX tag components (dead data — never rendered) ───────────────

export const Video: React.FC<{ children: React.ReactNode }> = () => null;

export const Part: React.FC<{
  id: PartId;
  label: string;
  children: React.ReactNode;
}> = () => null;

export const Slide: React.FC<{
  id: string;
  section?: boolean;
  children: React.ReactNode;
}> = () => null;

// Visual tags
export const Title: React.FC<{ main: string; sub?: string }> = () => null;
export const Headline: React.FC<{ text: string }> = () => null;
export const Text: React.FC<{ body: string }> = () => null;
export const Bullets: React.FC<{ items: string[] }> = () => null;
export const CodeBlock: React.FC<{
  code: string;
  language: string;
  caption?: string;
}> = () => null;
export const Callout: React.FC<{
  icon: string;
  heading: string;
  body: string;
}> = () => null;
export const ArchDiagramTag: React.FC = () => null;
export const LifecycleDiagramTag: React.FC = () => null;
export const Chat: React.FC<{ children: React.ReactNode }> = () => null;
export const User: React.FC<{ children: string }> = () => null;
export const Agent: React.FC<{ children: string }> = () => null;
export const Tool: React.FC<{ children: string }> = () => null;
export const ToolCall: React.FC<{
  tool: string;
  args: string;
  result: string;
  icon: string;
}> = () => null;
export const Scene: React.FC<{
  leftLabel: string;
  leftLines: string[];
  rightLabel: string;
  rightLines: string[];
}> = () => null;
export const Inspiration: React.FC<{
  tagline: string;
  children: React.ReactNode;
}> = () => null;
export const Clip: React.FC<{
  label: string;
  file: string;
  url: string;
}> = () => null;

// ── Shared tree-walking helpers ───────────────────────────────────

function collectText(children: React.ReactNode): string {
  const parts: string[] = [];
  React.Children.forEach(children, (child) => {
    if (typeof child === "string") parts.push(child);
    else if (typeof child === "number") parts.push(String(child));
  });
  return parts.join(" ").replace(/\s+/g, " ").trim();
}

type SlideInfo = {
  id: string;
  narration: string;
  section: boolean;
  partId: PartId;
  visualElement: React.ReactElement<any>;
};

function walkSlides(tree: React.ReactElement): SlideInfo[] {
  const el = tree as React.ReactElement<any>;
  if (el.type !== Video) throw new Error("Root element must be <Video>");

  const slides: SlideInfo[] = [];

  React.Children.forEach(el.props.children, (partChild) => {
    if (!React.isValidElement(partChild)) return;
    const partEl = partChild as React.ReactElement<any>;
    if (partEl.type !== Part) throw new Error("Video children must be <Part>");
    const partId: PartId = partEl.props.id;

    React.Children.forEach(partEl.props.children, (slideChild) => {
      if (!React.isValidElement(slideChild)) return;
      const slideEl = slideChild as React.ReactElement<any>;
      if (slideEl.type !== Slide) throw new Error("Part children must be <Slide>");

      // Find the visual element (the one non-text child)
      let visualElement: React.ReactElement<any> | null = null;
      React.Children.forEach(slideEl.props.children, (child) => {
        if (React.isValidElement(child)) visualElement = child as React.ReactElement<any>;
      });
      if (!visualElement) throw new Error(`Slide "${slideEl.props.id}" has no visual tag`);

      slides.push({
        id: slideEl.props.id,
        narration: collectText(slideEl.props.children),
        section: slideEl.props.section ?? false,
        partId,
        visualElement,
      });
    });
  });

  return slides;
}

// ── Walker 1: extractNarrations (for TTS / build.ts) ─────────────

export type NarrationEntry = { id: string; narration: string };

export function extractNarrations(tree: React.ReactElement): NarrationEntry[] {
  return walkSlides(tree).map(({ id, narration }) => ({ id, narration }));
}

// ── Walker 2: toComposition (for Remotion rendering) ─────────────

function renderVisual(el: React.ReactElement<any>, partId: PartId): React.ReactNode {
  const { type, props } = el;

  if (type === Title) {
    return <TitleSlide title={props.main} subtitle={props.sub} partId={partId} />;
  } else if (type === Headline) {
    return <HeadlineSlide text={props.text} partId={partId} />;
  } else if (type === Text) {
    return <TextSlide body={props.body} partId={partId} />;
  } else if (type === Bullets) {
    return <BulletsSlide items={props.items} partId={partId} />;
  } else if (type === CodeBlock) {
    return <CodeBlockSlide code={props.code} language={props.language} caption={props.caption} partId={partId} />;
  } else if (type === Callout) {
    return <CalloutSlide icon={props.icon} heading={props.heading} body={props.body} partId={partId} />;
  } else if (type === ArchDiagramTag) {
    return <ArchDiagram />;
  } else if (type === LifecycleDiagramTag) {
    return <LifecycleDiagram />;
  } else if (type === Chat) {
    const messages: { role: "user" | "agent" | "tool"; text: string }[] = [];
    React.Children.forEach(props.children, (msg) => {
      if (!React.isValidElement(msg)) return;
      const msgEl = msg as React.ReactElement<any>;
      if (msgEl.type === User) messages.push({ role: "user", text: String(msgEl.props.children) });
      else if (msgEl.type === Agent) messages.push({ role: "agent", text: String(msgEl.props.children) });
      else if (msgEl.type === Tool) messages.push({ role: "tool", text: String(msgEl.props.children) });
    });
    return <ChatSlide messages={messages} partId={partId} />;
  } else if (type === ToolCall) {
    return <ToolCallSlide tool={props.tool} args={props.args} result={props.result} icon={props.icon} partId={partId} />;
  } else if (type === Scene) {
    return <SceneSlide leftLabel={props.leftLabel} leftLines={props.leftLines} rightLabel={props.rightLabel} rightLines={props.rightLines} partId={partId} />;
  } else if (type === Inspiration) {
    const clips: { label: string; file: string; url: string }[] = [];
    React.Children.forEach(props.children, (clipChild) => {
      if (!React.isValidElement(clipChild)) return;
      const clipEl = clipChild as React.ReactElement<any>;
      if (clipEl.type === Clip) clips.push({ label: clipEl.props.label, file: clipEl.props.file, url: clipEl.props.url });
    });
    return <InspirationSlide clips={clips} tagline={props.tagline} partId={partId} />;
  }

  throw new Error(`Unknown visual tag: ${type}`);
}

type Manifest = Record<string, { file: string; durationMs: number }>;

export function toComposition(
  tree: React.ReactElement,
  manifest: Manifest,
  fps: number,
): { element: React.ReactNode; totalFrames: number } {
  const slides = walkSlides(tree);
  let currentFrame = 0;
  const sequences: React.ReactNode[] = [];

  slides.forEach((slide, i) => {
    const isSection = slide.section && i > 0;
    const leadIn = isSection ? TIMING.SECTION_LEAD_IN : TIMING.SLIDE_LEAD_IN;
    const tail = isSection ? TIMING.SECTION_TAIL : TIMING.SLIDE_TAIL;

    // Audio duration in frames
    let audioFrames: number;
    if (manifest[slide.id]) {
      audioFrames = Math.ceil((manifest[slide.id].durationMs / 1000) * fps);
    } else {
      // Fallback: estimate from word count
      const words = slide.narration.split(/\s+/).length;
      audioFrames = Math.ceil(((words / 150) * 60) * fps);
    }

    const totalDuration = leadIn + audioFrames + tail;
    const slideStart = currentFrame;
    currentFrame += totalDuration;

    const hasAudio = !!manifest[slide.id];

    sequences.push(
      <Sequence key={slide.id} from={slideStart} durationInFrames={totalDuration} premountFor={fps}>
        <SlideContainer partId={slide.partId}>
          {renderVisual(slide.visualElement, slide.partId)}
        </SlideContainer>
        {hasAudio && (
          <Sequence from={leadIn} durationInFrames={audioFrames}>
            <Audio src={staticFile(`audio/${manifest[slide.id].file}`)} />
          </Sequence>
        )}
      </Sequence>,
    );
  });

  return { element: <>{sequences}</>, totalFrames: currentFrame };
}

// ── Walker 3: extractTimeline (for ProgressBar) ──────────────────

export type TimelineSlide = {
  id: string;
  partIndex: number;
  slideIndex: number;
  globalStart: number;
  duration: number;
  section: boolean;
};

export type TimelinePart = {
  partId: PartId;
  label: string;
  globalStart: number;
  duration: number;
  slides: TimelineSlide[];
};

export function extractTimeline(
  tree: React.ReactElement,
  manifest: Manifest,
  fps: number,
): { parts: TimelinePart[]; totalFrames: number } {
  const slides = walkSlides(tree);
  const parts: TimelinePart[] = [];
  let globalFrame = 0;

  // Group slides by part
  let currentPartId: PartId | null = null;
  let currentPart: TimelinePart | null = null;
  let partIndex = -1;

  for (let i = 0; i < slides.length; i++) {
    const slide = slides[i];

    if (slide.partId !== currentPartId) {
      if (currentPart) parts.push(currentPart);
      partIndex++;
      currentPartId = slide.partId;
      currentPart = {
        partId: slide.partId,
        label: PART_THEMES[slide.partId].label,
        globalStart: globalFrame,
        duration: 0,
        slides: [],
      };
    }

    const slideIndex = currentPart!.slides.length;
    const isSection = slide.section && slideIndex > 0;
    const leadIn = isSection ? TIMING.SECTION_LEAD_IN : TIMING.SLIDE_LEAD_IN;
    const tail = isSection ? TIMING.SECTION_TAIL : TIMING.SLIDE_TAIL;

    let audioFrames: number;
    if (manifest[slide.id]) {
      audioFrames = Math.ceil((manifest[slide.id].durationMs / 1000) * fps);
    } else {
      const words = slide.narration.split(/\s+/).length;
      audioFrames = Math.ceil(((words / 150) * 60) * fps);
    }

    const totalDuration = leadIn + audioFrames + tail;

    currentPart!.slides.push({
      id: slide.id,
      partIndex,
      slideIndex,
      globalStart: globalFrame,
      duration: totalDuration,
      section: slide.section,
    });

    globalFrame += totalDuration;
  }

  if (currentPart) {
    currentPart.duration = globalFrame - currentPart.globalStart;
    parts.push(currentPart);
  }

  // Backfill part durations
  for (const part of parts) {
    const lastSlide = part.slides[part.slides.length - 1];
    part.duration = lastSlide.globalStart + lastSlide.duration - part.globalStart;
  }

  return { parts, totalFrames: globalFrame };
}

// Compute total frames without building React elements (for Composition registration)
export function getTotalFrames(tree: React.ReactElement, manifest: Manifest, fps: number): number {
  const slides = walkSlides(tree);
  let total = 0;
  slides.forEach((slide, i) => {
    const isSection = slide.section && i > 0;
    const leadIn = isSection ? TIMING.SECTION_LEAD_IN : TIMING.SLIDE_LEAD_IN;
    const tail = isSection ? TIMING.SECTION_TAIL : TIMING.SLIDE_TAIL;
    let audioFrames: number;
    if (manifest[slide.id]) {
      audioFrames = Math.ceil((manifest[slide.id].durationMs / 1000) * fps);
    } else {
      const words = slide.narration.split(/\s+/).length;
      audioFrames = Math.ceil(((words / 150) * 60) * fps);
    }
    total += leadIn + audioFrames + tail;
  });
  return total;
}
