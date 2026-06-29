# Proposal: Automating Earthdata Token Rotation

**Status:** Proposal / not yet implemented
**Author:** drafted with Claude, 2026-06-28
**Scope:** `gps-spoofer` deployment (Ser8, Pi5b, repo, Zeus)

## Problem

The NASA Earthdata (CDDIS) bearer token in `gpsdata.py` (line 4, `TOKEN = "..."`)
expires roughly every 60 days. Each rotation currently requires a manual,
multi-step sequence across four machines:

1. Patch the token in the Ser8 runtime `gpsdata.py`
2. `scp` to the Pi5b runtime
3. `cp` into the repo working copy, commit, push
4. `git pull` on Zeus
5. Restart the Pi5b `gps_spoofer_web.service` so the running process drops the
   stale in-memory token

Done by hand this is ~10 minutes of careful work with several points where a
wrong base file or a missed `md5sum` check can silently break things (e.g. the
prior incident where work was lost by patching from a stale base file).

## Constraint (the one thing that can't be automated)

NASA Earthdata user tokens are generated only through the Earthdata web portal
("Generate Token"). There is **no public API to mint a token programmatically.**
Therefore fully hands-off rotation is not achievable without headless-browser
login automation, which is fragile (session/MFA behavior, portal changes) and
requires storing the NASA password. For a token that lasts ~60 days and takes
~20 seconds to generate by hand, that maintenance cost is not worth it.

**The realistic goal:** collapse everything *after* token generation into a
single command, and add an early-warning so the expiry never surprises us.

## Proposed tiers

### Tier 1 — `rotate_token.sh` (recommended, build this)

A single script on Ser8 that takes the new token as an argument and performs the
entire sync with verification baked in as guardrails:

```
./rotate_token.sh 'eyJ0eXAi…'
```

Behavior:

1. **Validate** the token: confirm it is a well-formed 3-part JWT, decode the
   payload, confirm `uid == bajacali` and `exp` is in the future. Abort if not.
2. **Patch** line 4 of the Ser8 runtime `gpsdata.py` (timestamped backup first).
3. **Compute** the new md5 and use it as the expected value for every downstream
   check — refuse to proceed if any location fails to match.
4. **Propagate:** `scp` to Pi5b runtime → verify md5; `cp` into repo working
   copy; `git commit` with the decoded expiry date in the message; `git push`.
5. **Zeus:** `ssh rzeus 'git pull'` → verify md5.
6. **Restart** `gps_spoofer_web.service` on Pi5b (see open question 2).
7. **Report** a four-location green/red summary.

This turns the 10-minute manual dance into ~15 seconds and removes the
stale-base-file failure mode entirely (the md5 gate is the safety net).

### Tier 2 — expiry early-warning (optional, cheap)

A weekly `cron` job on Ser8 that reads the `exp` claim out of the live
`gpsdata.py`, and emails/pings when it is within ~10 days of expiry. Closes the
"did I forget?" gap without touching the un-automatable login step. NASA already
emails ~7 days out; this is a redundant, self-owned heads-up.

### Tier 3 — automated token generation (not recommended)

A headless browser that logs into Earthdata and clicks "Generate Token." Rejected
for now: stores the NASA password, fights session/MFA behavior, and breaks
silently on any portal change. Revisit only if NASA ships a token-minting API.

## Recommendation

Build **Tier 1**, optionally add **Tier 2**. End-to-end rotation then becomes:

> receive expiry email (or Tier 2 ping) → log in, click Generate Token →
> `./rotate_token.sh 'new-token'` → done.

That is about as good as it gets while NASA owns the minting step.

## Open questions (block implementation)

1. **Passwordless SSH from Ser8?** The script needs non-interactive
   (key-based) SSH to both `michael@192.168.1.75` (Pi5b) and the `rzeus` alias,
   or it will hang on password prompts. Confirm keys are in place.
2. **Service restart policy:** restart `gps_spoofer_web.service` always, or only
   when it is currently `active`? (Conditional is safer for a unit that may be
   mid-run.)

## Reference: paths and invariants

- Token location: `gpsdata.py` line 4, `TOKEN = "<jwt>"`; consumed at line ~115
  as `Authorization: Bearer {TOKEN}`.
- Ser8 runtime: `~/gps_spoofer/gpsdata.py`
- Pi5b runtime: `~/gps_spoofer/gpsdata.py` (host `192.168.1.75`)
- Repo working copy (Ser8): `~/gps-spoofer-repo/`
- Zeus: `~/gps-spoofer-repo/` via `ssh rzeus`
- `TOKEN` is a module-level constant; Python caches the module after first
  import, so a long-running service holds the old token until restarted.
