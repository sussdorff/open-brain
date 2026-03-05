import { z } from "zod";
import { workerPost } from "../worker-client.js";

export const getObservationsSchema = {
  ids: z.array(z.number()).describe("Array of observation IDs to fetch (required)"),
  orderBy: z.string().optional().describe("Sort order"),
  limit: z.number().optional().describe("Max results"),
  project: z.string().optional().describe("Filter by project"),
};

export async function getObservationsTool(params: Record<string, unknown>) {
  return workerPost("/api/observations/batch", params);
}
