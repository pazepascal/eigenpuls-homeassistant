# Wire protocol

The JSON contract between an Eigenpuls client and this receiver. One file per
version; all of them are still accepted.

| Version | Status |
|---|---|
| [v4](payload-v4.md) | **current** — metric registry, sleep, body composition, blood pressure, workouts |
| [v3](payload-v3.md) | accepted, not deprecated — rolling aggregates |
| [v2](payload-v2.md) | accepted — snapshot and buckets separated |
| [v1](payload-v1.md) | accepted — superseded |

Two rules govern changes, and both exist because of the same failure mode: a
receiver that answers 200 while quietly dropping data the client believes it has
stored.

1. **Changing the meaning of an existing field requires a version bump.** Not a
   silent edit — an older receiver must refuse a newer payload outright rather
   than validate the envelope and discard what it does not recognise.
2. **The metric registry is closed.** An unknown metric identifier is rejected,
   never ignored. Adding a metric whose semantics fit an existing bucket family is
   a registry entry on each side and needs no new version; a metric that needs a
   genuinely new family does.

`custom_components/apple_health_sync/registry.py` is the registry, and
`tests/test_registry_freeze.py` stops an existing entry being renamed or
reinterpreted — Home Assistant long-term statistics cannot be rolled back, so a
changed unit silently reinterprets history that is already stored.
