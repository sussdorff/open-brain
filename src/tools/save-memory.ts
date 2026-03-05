import { z } from "zod";
import type { DataLayer, SaveMemoryParams } from "../data-layer/index.js";

export const saveMemorySchema = {
  text: z.string().describe("The observation text to save"),
  type: z
    .string()
    .optional()
    .describe("Observation type (discovery, decision, action, etc.)"),
  project: z
    .string()
    .optional()
    .describe("Project name to associate with"),
  title: z
    .string()
    .optional()
    .describe("Optional title for the observation"),
  subtitle: z
    .string()
    .optional()
    .describe("Optional subtitle or one-line summary"),
  narrative: z
    .string()
    .optional()
    .describe("Optional longer narrative context"),
};

export function createSaveMemoryTool(dl: DataLayer) {
  return async (params: Record<string, unknown>) =>
    dl.saveMemory(params as unknown as SaveMemoryParams);
}
