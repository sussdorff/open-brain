import { z } from "zod";
import type { DataLayer } from "../data-layer/index.js";

export const getObservationsSchema = {
  ids: z
    .array(z.number())
    .describe("Array of observation IDs to fetch (required)"),
};

export function createGetObservationsTool(dl: DataLayer) {
  return async (params: Record<string, unknown>) => {
    const ids = params.ids as number[];
    return dl.getObservations(ids);
  };
}
