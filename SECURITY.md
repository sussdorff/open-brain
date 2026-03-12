# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest  | Yes       |

## Reporting a Vulnerability

If you discover a security vulnerability in open-brain, please report it responsibly:

1. **Do not** open a public GitHub issue for security vulnerabilities
2. Email **malte.sussdorff@gmail.com** with:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

You should receive a response within 48 hours. We will work with you to understand the issue and coordinate a fix before any public disclosure.

## Security Considerations

### Authentication

- open-brain uses OAuth 2.1 with PKCE for client authentication
- All MCP endpoints require a valid Bearer token or API key
- JWT secrets must be at least 32 characters
- Passwords must be at least 8 characters

### Data Storage

- All memory data is stored in Postgres with standard access controls
- Embeddings are stored alongside memory content
- No secrets are stored in the repository — use environment variables or a secrets manager

### Network

- The server should be deployed behind HTTPS in production
- `MCP_SERVER_URL` is validated to prevent DNS rebinding attacks
- CORS is not enabled by default

### Dependencies

- Dependencies are pinned via `uv.lock` for reproducible builds
- We monitor for known vulnerabilities in dependencies
