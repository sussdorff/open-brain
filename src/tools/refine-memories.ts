import { z } from "zod";
import type { DataLayer, RefineParams } from "../data-layer/index.js";

export const refineMemoriesSchema = {
  scope: z
    .string()
    .optional()
    .describe(
      "Scope: 'recent', 'duplicates', 'low-priority', or 'project:<name>'"
    ),
  limit: z.number().optional().describe("Max memories to analyze (default 50)"),
  dryRun: z
    .boolean()
    .optional()
    .describe("If true, only suggest actions without executing"),
};

export function createRefineMemoriesTool(dl: DataLayer) {
  return async (params: Record<string, unknown>) =>
    dl.refineMemories(params as RefineParams);
}
