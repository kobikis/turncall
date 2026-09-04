# Open-core licensing: MIT engine, FSL builders

TurnCall is released as open core. The engine (this repo) is MIT. The two
builder repos — `turncall-builder-api` and `turncall-builder-web` — are
released under FSL-1.1-Apache-2.0. This records why the line was drawn there.

## The split

- **`turncall` (this repo): MIT.** The runtime that answers calls. Already
  public, 277 commits of history, and the thing a self-hoster actually runs.
- **`turncall-builder-*`: FSL-1.1-Apache-2.0.** The conversational agent
  builder. The commercial product.

## Why not MIT everywhere

The builder is the differentiated product; the engine is infrastructure. MIT on
the builder would let a competitor run it verbatim as a hosted service, which
is the specific outcome worth preventing. MIT on the engine costs nothing —
self-hosters running their own voice agents are the audience, not competitors,
and an unencumbered engine is what makes adoption plausible at all.

## Why FSL and not something harsher

Alternatives considered:

- **Elastic License 2.0** — simplest to read, three plain limitations. Rejected
  because it never converts to open source. We want outside contributors, and a
  perpetual restriction paired with a request for free labor is the combination
  that reliably breeds resentment.
- **PolyForm Shield** — permits everything except competing. Also never
  converts; same objection.
- **BSL 1.1** — the closest alternative. Rejected for having more parameters to
  configure and a conventional 4-year conversion where FSL's is 2.
- **AGPL** — would keep it open source proper, but the copyleft obligation on
  network use is a compliance question most companies answer by not adopting.
  It also doesn't actually stop a hosted competitor, only obliges them to share
  changes.

FSL-1.1-Apache-2.0 blocks the competing-service case and converts each release
to Apache 2.0 after two years. That conversion is what keeps contributing
rational for someone outside the company.

## Consequences

- **DCO everywhere; the builders add a relicensing grant.** On the MIT engine
  inbound terms equal outbound, so a sign-off is all that is needed. On the FSL
  builders, `CONTRIBUTING.md` states that contributing also grants the right to
  relicense — which is what lets a contribution be included in the Apache
  conversion, since that promise can only be made about code the licensor may
  relicense. The DCO sign-off is enforced in CI on all three.

  A signed CLA would be stronger: the DCO certifies authorship, not assent to
  an extra term, so this relies on the contributor having read the file. It was
  chosen anyway because the alternative was worse in practice — the de-facto
  CLA action is archived, the hosted service cannot be scripted, and a CLA that
  does not exist yet would have meant closing the builders to contributions
  entirely. This is the approach Caveman uses at scale for the same open-core
  shape. Revisit before selling or OEM-licensing the builders.
- **Dependencies must stay permissive.** A copyleft dependency in the engine
  would undercut the MIT grant. The Pipecat 1.8.1 upgrade removed the last one
  (`pyyaml-include`, GPL-3.0). New dependencies should be checked; anything
  GPL/AGPL/SSPL needs a deliberate decision, not a silent `pip install`.
- **The API between them is a public contract.** Three separate repos means a
  self-hoster can run mismatched versions, so the builder declares a supported
  engine version range and warns at startup on mismatch.
- **Irreversible per release.** Terms can change for future releases, but every
  published version stays licensed as published forever. That asymmetry is why
  this was decided before the builders went public rather than after.

## Status

Accepted. The engine is public under MIT today. The builders remain private
until their licence, CI, and documentation are in place.
