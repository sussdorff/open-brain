import { z } from "zod";
import type { DataLayer, TimelineParams } from "../data-layer/index.js";

export const timelineSchema = {
  anchor: z
    .number()
    .optional()
    .describe("Observation ID to anchor the timeline around"),
  query: z
    .string()
    .optional()
    .describe("Search query to find anchor automatically"),
  depth_before: z
    .number()
    .optional()
    .describe("Number of records before anchor"),
  depth_after: z
    .number()
    .optional()
    .describe("Number of records after anchor"),
  project: z.string().optional().describe("Filter by project name"),
};

export function createTimelineTool(dl: DataLayer) {
  return async (params: Record<string, unknown>) =>
    dl.timeline(params as TimelineParams);
}
