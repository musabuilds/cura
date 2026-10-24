# Security

Report vulnerabilities privately through a GitHub **private security advisory** on this repository
(Security → Report a vulnerability). Please do not open public issues for them. We aim to acknowledge within 72 hours.

## Design notes

* Passwords: Argon2id. API keys, refresh tokens, auth codes: stored as SHA-256 hashes, shown once.
* Access tokens: short-lived (15 min) HS256 JWTs with `iss`/`aud`/`azp`. `CURA_JWT_SECRET` must be ≥ 32
  random bytes in production; the API refuses to start in production with the default.
* OAuth: authorization code + PKCE S256 only (no implicit, no `plain`), exact redirect-URI match,
  single-use 2-minute codes, rotating refresh tokens with family revocation on reuse.
* Consent page: `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `no-store`.
* Rate limiting is in-process; put a shared limiter (Redis) in front when running several workers.
* Reviews never expose reviewer identity; visit dates are month-granular.
