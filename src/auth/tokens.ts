import { SignJWT, jwtVerify, type JWTPayload } from "jose";
import { config } from "../config.js";

const secret = new TextEncoder().encode(config.JWT_SECRET);

interface TokenClaims {
  sub: string;
  clientId: string;
  scopes: string[];
}

export async function issueAccessToken(claims: TokenClaims): Promise<string> {
  return new SignJWT({ clientId: claims.clientId, scopes: claims.scopes })
    .setProtectedHeader({ alg: "HS256" })
    .setSubject(claims.sub)
    .setIssuedAt()
    .setExpirationTime("1h")
    .setIssuer(config.MCP_SERVER_URL)
    .sign(secret);
}

export async function issueRefreshToken(claims: TokenClaims): Promise<string> {
  return new SignJWT({
    clientId: claims.clientId,
    scopes: claims.scopes,
    type: "refresh",
  })
    .setProtectedHeader({ alg: "HS256" })
    .setSubject(claims.sub)
    .setIssuedAt()
    .setExpirationTime("30d")
    .setIssuer(config.MCP_SERVER_URL)
    .sign(secret);
}

export interface VerifiedToken extends JWTPayload {
  clientId: string;
  scopes: string[];
  type?: string;
}

export async function verifyToken(token: string): Promise<VerifiedToken> {
  const { payload } = await jwtVerify(token, secret, {
    issuer: config.MCP_SERVER_URL,
  });
  return payload as VerifiedToken;
}
