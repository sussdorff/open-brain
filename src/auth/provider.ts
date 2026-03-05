import { randomBytes } from "node:crypto";
import type { Response } from "express";
import type {
  OAuthServerProvider,
  AuthorizationParams,
} from "@modelcontextprotocol/sdk/server/auth/provider.js";
import type {
  OAuthClientInformationFull,
  OAuthTokens,
  OAuthTokenRevocationRequest,
} from "@modelcontextprotocol/sdk/shared/auth.js";
import type { AuthInfo } from "@modelcontextprotocol/sdk/server/auth/types.js";
import { clientsStore } from "./clients-store.js";
import { issueAccessToken, issueRefreshToken, verifyToken } from "./tokens.js";
import { config } from "../config.js";

// In-memory store for authorization codes
const authCodes = new Map<
  string,
  {
    clientId: string;
    codeChallenge: string;
    redirectUri: string;
    scopes: string[];
    expiresAt: number;
  }
>();

// Track revoked tokens
const revokedTokens = new Set<string>();

export const oauthProvider: OAuthServerProvider = {
  get clientsStore() {
    return clientsStore;
  },

  async authorize(
    client: OAuthClientInformationFull,
    params: AuthorizationParams,
    res: Response
  ): Promise<void> {
    const formHtml = `<!DOCTYPE html>
<html><head><title>open-brain Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: system-ui; max-width: 400px; margin: 100px auto; padding: 20px; }
  input { display: block; width: 100%; padding: 8px; margin: 8px 0; box-sizing: border-box; }
  button { padding: 10px 20px; background: #2563eb; color: white; border: none; border-radius: 4px; cursor: pointer; width: 100%; }
  button:hover { background: #1d4ed8; }
  h2 { margin-bottom: 4px; }
  p { color: #666; margin-top: 0; }
</style></head>
<body>
  <h2>open-brain</h2>
  <p>Authorize <strong>${client.client_name ?? "MCP Client"}</strong></p>
  <form method="POST" action="/authorize/submit">
    <input type="hidden" name="client_id" value="${client.client_id}">
    <input type="hidden" name="redirect_uri" value="${params.redirectUri}">
    <input type="hidden" name="code_challenge" value="${params.codeChallenge}">
    <input type="hidden" name="state" value="${params.state ?? ""}">
    <input type="hidden" name="scopes" value="${(params.scopes ?? []).join(" ")}">
    <input type="text" name="username" placeholder="Username" required autofocus>
    <input type="password" name="password" placeholder="Password" required>
    <button type="submit">Sign In</button>
  </form>
</body></html>`;
    res.setHeader("Content-Type", "text/html");
    res.end(formHtml);
  },

  async challengeForAuthorizationCode(
    _client: OAuthClientInformationFull,
    authorizationCode: string
  ): Promise<string> {
    const entry = authCodes.get(authorizationCode);
    if (!entry || entry.expiresAt < Date.now()) {
      throw new Error("Invalid or expired authorization code");
    }
    return entry.codeChallenge;
  },

  async exchangeAuthorizationCode(
    client: OAuthClientInformationFull,
    authorizationCode: string
  ): Promise<OAuthTokens> {
    const entry = authCodes.get(authorizationCode);
    if (!entry || entry.expiresAt < Date.now()) {
      throw new Error("Invalid or expired authorization code");
    }
    if (entry.clientId !== client.client_id) {
      throw new Error("Client ID mismatch");
    }

    // Consume the code
    authCodes.delete(authorizationCode);

    const claims = {
      sub: config.AUTH_USER,
      clientId: client.client_id,
      scopes: entry.scopes,
    };

    const accessToken = await issueAccessToken(claims);
    const refreshToken = await issueRefreshToken(claims);

    return {
      access_token: accessToken,
      token_type: "Bearer",
      expires_in: 3600,
      refresh_token: refreshToken,
    };
  },

  async exchangeRefreshToken(
    client: OAuthClientInformationFull,
    refreshToken: string,
    scopes?: string[]
  ): Promise<OAuthTokens> {
    const payload = await verifyToken(refreshToken);
    if (payload.type !== "refresh") {
      throw new Error("Not a refresh token");
    }
    if (revokedTokens.has(refreshToken)) {
      throw new Error("Token has been revoked");
    }

    const claims = {
      sub: config.AUTH_USER,
      clientId: client.client_id,
      scopes: scopes ?? payload.scopes,
    };

    const newAccessToken = await issueAccessToken(claims);
    const newRefreshToken = await issueRefreshToken(claims);

    return {
      access_token: newAccessToken,
      token_type: "Bearer",
      expires_in: 3600,
      refresh_token: newRefreshToken,
    };
  },

  async verifyAccessToken(token: string): Promise<AuthInfo> {
    if (revokedTokens.has(token)) {
      throw new Error("Token has been revoked");
    }
    const payload = await verifyToken(token);
    if (payload.type === "refresh") {
      throw new Error("Cannot use refresh token as access token");
    }
    return {
      token,
      clientId: payload.clientId,
      scopes: payload.scopes,
      expiresAt: payload.exp,
    };
  },

  async revokeToken(
    _client: OAuthClientInformationFull,
    request: OAuthTokenRevocationRequest
  ): Promise<void> {
    revokedTokens.add(request.token);
  },
};

/**
 * Handle the login form submission (POST /authorize/submit).
 */
export function handleAuthorizeSubmit(
  body: {
    username: string;
    password: string;
    client_id: string;
    redirect_uri: string;
    code_challenge: string;
    state: string;
    scopes: string;
  },
  res: Response
): void {
  if (
    body.username !== config.AUTH_USER ||
    body.password !== config.AUTH_PASSWORD
  ) {
    const errorUrl = new URL(body.redirect_uri);
    errorUrl.searchParams.set("error", "access_denied");
    errorUrl.searchParams.set("error_description", "Invalid credentials");
    if (body.state) errorUrl.searchParams.set("state", body.state);
    res.redirect(errorUrl.toString());
    return;
  }

  // Generate authorization code
  const code = randomBytes(32).toString("hex");
  authCodes.set(code, {
    clientId: body.client_id,
    codeChallenge: body.code_challenge,
    redirectUri: body.redirect_uri,
    scopes: body.scopes ? body.scopes.split(" ").filter(Boolean) : [],
    expiresAt: Date.now() + 5 * 60 * 1000, // 5 minutes
  });

  // Redirect back with code
  const redirectUrl = new URL(body.redirect_uri);
  redirectUrl.searchParams.set("code", code);
  if (body.state) redirectUrl.searchParams.set("state", body.state);
  res.redirect(redirectUrl.toString());
}
