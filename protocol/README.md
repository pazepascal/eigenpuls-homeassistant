# Wire protocol

The JSON contract between an Eigenpuls client and this receiver. One file per
version; all of them are still accepted.

| Version | Status |
|---|---|
| [v4](payload-v4.md) | **current** — metric registry, sleep, body composition, blood pressure, workouts |
| [v3](payload-v3.md) | accepted, not deprecated — rolling aggregates |
| [v2](payload-v2.md) | accepted — snapshot and buckets separated |
| [v1](payload-v1.md) | accepted — superseded |

Three rules govern changes, and all three exist because of the same failure
mode: a receiver that answers 200 while quietly dropping data the client believes
it has stored.

1. **Changing the meaning of an existing field requires a version bump.** Not a
   silent edit — an older receiver must refuse a newer payload outright rather
   than validate the envelope and discard what it does not recognise.
2. **The metric registry is closed.** An unknown metric identifier is rejected,
   never ignored. Adding a metric whose semantics fit an existing bucket family is
   a registry entry on each side and needs no new version; a metric that needs a
   genuinely new family does.
3. **Bucket objects are closed.** A bucket carries the keys its family defines
   and nothing else. A key nobody reads is a measurement the sender thinks was
   stored, and a typo in a field name is indistinguishable from one. Same
   reasoning as rule 2, one level down.

[Pairing](pairing-v1.md) is specified separately and versioned separately: it
carries one URL and one token, changes when *setup* changes, and has no business
forcing a wire version bump when it does.

`custom_components/apple_health_sync/registry.py` is the registry, and
`tests/test_registry_freeze.py` stops an existing entry being renamed or
reinterpreted — Home Assistant long-term statistics cannot be rolled back, so a
changed unit silently reinterprets history that is already stored.
