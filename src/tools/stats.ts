import type { DataLayer } from "../data-layer/index.js";

export const statsSchema = {};

export function createStatsTool(dl: DataLayer) {
  return async (_params: Record<string, unknown>) => dl.stats();
}
