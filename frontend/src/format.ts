import type { OverallStatus } from "./types";

export const FLAG_LABEL: Record<string, string> = { H: "High", L: "Low", N: "Normal" };
export const OP_WORDS: Record<string, string> = { ">": ">", ">=": "≥", "<": "<", "<=": "≤" };
export const TYPE_LABEL: Record<string, string> = { DRUG_DRUG: "Drug–drug", DRUG_LAB: "Drug–lab" };

export const statusLabel = (s: OverallStatus) => (s === "NEEDS_DATA" ? "NEEDS DATA" : s);

export const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

// The API reports only the model name (never a literal "provider" field), so the provider is derived from it --
// never hard-coded to one vendor. Known models get a polished label; an unrecognized model still shows correctly
// (just without a pretty vendor prefix) instead of silently lying about which provider answered.
const MODEL_LABELS: Record<string, string> = {
  "gpt-5.6-luna": "OpenAI · GPT-5.6 Luna",
  "claude-opus-5": "Anthropic · Claude Opus 5",
  "claude-sonnet-5": "Anthropic · Claude Sonnet 5",
  "us.amazon.nova-2-lite-v1:0": "Amazon Bedrock · Nova 2 Lite",
  "gemini-3.8-flash": "Google · Gemini 3.8 Flash",
};

export function providerLabel(model?: string | null): string {
  if (!model) return "model";
  if (MODEL_LABELS[model]) return MODEL_LABELS[model];
  if (model.startsWith("gpt-")) return `OpenAI · ${model}`;
  if (model.startsWith("claude-")) return `Anthropic · ${model}`;
  if (model.includes("nova")) return `Amazon Bedrock · ${model}`;
  if (model.includes("gemini")) return `Google · ${model}`;
  return model;
}

export function formatRange(r?: { low?: number | null; high?: number | null } | null): string {
  if (!r || (r.low == null && r.high == null)) return "—";
  if (r.low != null && r.high != null) return `${r.low}–${r.high}`;
  return r.low != null ? `≥ ${r.low}` : `≤ ${r.high}`;
}
