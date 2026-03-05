import { z } from "zod";
import { workerGet } from "../worker-client.js";

export const statsSchema = {};

export async function statsTool(_params: Record<string, unknown>) {
  return workerGet("/api/stats");
}
