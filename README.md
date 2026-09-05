# Eigenpuls for Home Assistant

The Home Assistant side of **Eigenpuls**: a local-first bridge that brings selected
Apple Health data from an iPhone into your own Home Assistant.

The iPhone sends to a webhook on *your* instance. There is no cloud service in
between, no account, and no third party — this integration is the receiving half,
and the data path is phone → your Home Assistant, full stop.

*Not affiliated with, endorsed by, or sponsored by Apple. "Apple Health" and
"HealthKit" are trademarks of Apple Inc.*

## Naming

**Eigenpuls** is the product name. The technical Home Assistant domain stays
`apple_health_sync`, and so do the entity ids and the `apple_health_sync:*`
long-term statistics.

That is deliberate. The domain determines the statistic ids, and Home Assistant
statistics cannot be renamed without orphaning the history behind them — for an
installation that has been collecting for months, a cosmetic rename would throw
that away. Nothing about the identifier is user-visible, so it stays as it is.

## What it does

Receives a versioned JSON payload and turns it into:

- **Entities** on a device called *Apple Health* — the current value of each metric
- **Long-term statistics** under `apple_health_sync:*` — the durable history that
  charts and comparisons are built from

Currently 18 wire metrics and 7 sleep series: heart rate, steps, resting heart
rate, HRV, respiratory rate, blood oxygen, active energy, walking and running
distance, sleep with stages, naps, seven-night sleep trends, weight, body fat,
blood pressure with weighted averages, workout summaries, and VO₂ max.

Blood pressure carries a measurement count alongside the hourly means, because
Home Assistant's rollup is an unweighted mean of hourly means — without the count,
an hour holding three readings would count for no more than an hour holding one.

## Install

See [docs/INSTALL.md](docs/INSTALL.md).

## Design notes worth knowing before contributing

- **The metric registry is closed.** An unknown metric identifier is rejected, not
  ignored. A receiver that answers 200 while discarding data the client believes it
  has stored is the failure this design exists to prevent.
- **Absence is a state.** A missing value and a measured zero stay distinguishable
  all the way through. Do not coalesce them.
- **Statistics have no rollback.** Adding a series is safe. Renaming or
  reinterpreting one is not, and `tests/test_registry_freeze.py` will stop you.
- **Push-only.** There is no `DataUpdateCoordinator`, because there is nothing to
  poll.
- **No health values in logs.** Counts and reason codes only.

## Protocol

[`protocol/`](protocol/README.md) holds the wire contract, one file per version.
v1 through v4 are all still accepted; v4 is current.

## Development

```bash
pip install -r requirements-dev.txt
ruff check custom_components tests
pytest -q
```

## License

[Apache-2.0](LICENSE). Covers this repository only — the Eigenpuls iOS app is
developed separately and is not licensed here.
