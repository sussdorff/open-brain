import { z } from "zod";
import type { DataLayer } from "../data-layer/index.js";

export const getContextSchema = {
  limit: z.number().optional().describe("Max recent sessions to return"),
  project: z.string().optional().describe("Filter by project name"),
};

export function createGetContextTool(dl: DataLayer) {
  return async (params: Record<string, unknown>) =>
    dl.getContext(
      params.limit as number | undefined,
      params.project as string | undefined
    );
}
