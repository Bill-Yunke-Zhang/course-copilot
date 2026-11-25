/**
 * The archetype renderer registry — the frontend mirror of the backend's
 * `app/agents/` plug-in property.
 *
 * The dashboard (and any other surface) renders agent output by looking up
 * the output's archetype here; it never needs to know which agent produced
 * it. That is what lets an agent the frontend has never seen — including
 * the staff mystery agents used at grading — display correctly, as long as
 * it uses a registered archetype.
 *
 * EXTENSION POINT (spec §9.4): if your student-designed agent defines a new
 * archetype, register its renderer here with `registerArchetype(...)` — do
 * not modify the five shipped renderers or turn this into a switch
 * statement. The shipped five must keep rendering exactly as before; staff
 * grading agents depend on them.
 */

import type { ComponentType } from "react";
import type { ItemPayload } from "../lib/api";
import { CardItemView } from "./CardItemView";
import { QaItemView } from "./QaItemView";
import { StatusItemView } from "./StatusItemView";
import { TextBlockView } from "./TextBlockView";
import { TimelineItemView } from "./TimelineItemView";

export interface ArchetypeRendererProps {
  payload: ItemPayload;
}

export type ArchetypeRenderer = ComponentType<ArchetypeRendererProps>;

const registry = new Map<string, ArchetypeRenderer>([
  ["timeline_item", TimelineItemView],
  ["status_item", StatusItemView],
  ["qa_item", QaItemView],
  ["card_item", CardItemView],
  ["text_block", TextBlockView],
]);

export function registerArchetype(name: string, renderer: ArchetypeRenderer): void {
  if (registry.has(name)) {
    throw new Error(`archetype "${name}" already has a renderer`);
  }
  registry.set(name, renderer);
}

export function getRenderer(name: string): ArchetypeRenderer | undefined {
  return registry.get(name);
}

/** Fallback for an archetype with no registered renderer. */
export function UnknownArchetype({ archetype }: { archetype: string }) {
  return (
    <div className="rounded border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/40 p-2 text-sm text-amber-800 dark:text-amber-300">
      No renderer registered for archetype "{archetype}" — see
      frontend/src/archetypes/registry.tsx
    </div>
  );
}
