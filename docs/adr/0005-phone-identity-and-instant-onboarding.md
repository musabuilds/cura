# ADR 0005 — A verified phone is the login and the evidence; onboarding is never blocked

**Status:** accepted · 2026-10-24 · amends [ADR 0004](0004-identity-onboarding-and-duplicates.md)

## Context

Cura's own app (`cura-mobile`) is how doctors and facility staff keep their records current. Email and
password, plus a moderator for every claim, is too slow for a doctor standing in a clinic corridor, and
a doctor who cannot find themselves (dirty data, a transliteration Cura has never seen) must not be
stuck. At the same time the registry must not fill with duplicates or with fake facilities listing
real doctors.

The registry already holds phone numbers for many facilities (contact) and doctors (role phones),
collected from sources before any account existed.

## Decision

1. **Sign-in is a one-time code to a mobile number.** The verified number (E.164) is the account's
   identity. Codes are random, HMAC-hashed with a server key, valid 5 minutes, 5 attempts, one live code
   per number; rate limited per number and per IP; the response never says whether an account exists.
2. **The number is evidence.** If the caller's verified number is on exactly one doctor's record (or
   one facility's), claiming that record is instant (`method: phone_match`, level `verified`). A number
   on several records proves nothing (a clinic's reception line is on every doctor's role there).
3. **Facilities prove control of something the registry recorded first**: a code to the listed phone
   (SMS for a mobile, voice for a landline), a code to a mailbox on the record's own website domain,
   a token on that website (well-known file or DNS TXT, fetched under strict SSRF rules), or MoH licence +
   commercial registration checked by a moderator. Targets always come from the record. Free-mail and
   shared hosting platforms (facebook.com, business.site…) are refused: anyone can publish there.
4. **Create is never blocked.** With a verified phone, `POST /v1/practitioners` / `POST /v1/facilities`
   still answer 409 with candidates first; with `confirm_new: true` the record is created at once, owned
   by its creator at level `unverified`, and a **merge case** with the candidates goes to moderators.
   (ADR 0004 sent these to a moderator before creation; email/password accounts keep that path.)
5. **Unverified is contained.** An owner who has proven nothing manages their own record, but cannot put
   a role on someone else's public roster on their own say-so (it waits for the other side), and an
   unverified facility cannot send invitations (which hand over a doctor's profile).
6. **Verification is computed on read** from approved claims (`reviewed > verified > unverified`, or
   `pending`), shown on every facility and practitioner read; it is never stored on the record.

## Consequences

* The common case (a doctor whose mobile is on their own clinic) is one code and one tap.
* SMS cost and abuse are bounded by per-number, per-IP and per-facility limits; a facility's listed
  phone can be targeted by at most 12 verification attempts an hour.
* Demo numbers with a fixed code exist for development and pitches and are switched off in production
  regardless of configuration.
* Private contact details a doctor adds are stored outside the record (owner-only), so the open dataset,
  history and change feed only ever contain what the doctor chose to publish.
