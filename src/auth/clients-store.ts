import { readFile, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import type { OAuthRegisteredClientsStore } from "@modelcontextprotocol/sdk/server/auth/clients.js";
import type { OAuthClientInformationFull } from "@modelcontextprotocol/sdk/shared/auth.js";
import { config } from "../config.js";

const clients = new Map<string, OAuthClientInformationFull>();

async function loadClients(): Promise<void> {
  try {
    const data = await readFile(config.CLIENTS_FILE, "utf-8");
    const entries = JSON.parse(data) as OAuthClientInformationFull[];
    for (const client of entries) {
      clients.set(client.client_id, client);
    }
    console.log(`Loaded ${clients.size} registered client(s)`);
  } catch {
    // No file yet, that's fine
  }
}

async function persistClients(): Promise<void> {
  const data = JSON.stringify([...clients.values()], null, 2);
  await writeFile(config.CLIENTS_FILE, data, "utf-8");
}

export const clientsStore: OAuthRegisteredClientsStore = {
  getClient(clientId: string) {
    return clients.get(clientId);
  },

  async registerClient(
    clientData: Omit<
      OAuthClientInformationFull,
      "client_id" | "client_id_issued_at"
    >
  ) {
    const clientId = randomUUID();
    const client: OAuthClientInformationFull = {
      ...clientData,
      client_id: clientId,
      client_id_issued_at: Math.floor(Date.now() / 1000),
    };
    clients.set(clientId, client);
    await persistClients();
    console.log(
      `Registered new client: ${client.client_name ?? clientId}`
    );
    return client;
  },
};

// Load on import
loadClients();
