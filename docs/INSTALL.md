# Installing the integration

## 1. Install

**Via HACS** — add this repository as a custom repository of type *Integration*,
then install **Eigenpuls**.

**Manually** — copy `custom_components/apple_health_sync/` into your Home
Assistant configuration directory so that
`/config/custom_components/apple_health_sync/manifest.json` exists.

Either way, restart Home Assistant afterwards. A new custom component is only
picked up on a full Core restart; a quick reload is not enough.

## 2. Add it

Settings ▸ Devices & Services ▸ **Add Integration** ▸ *Eigenpuls* ▸ Submit.

The result screen shows a **webhook URL** and a **token**. The token is shown once
and never again — put it into the app straight away. If it is lost, delete the
config entry and add the integration again; that issues a new webhook id and a new
token.

Neither value belongs in a note, a screenshot or a repository.

## 3. Reaching Home Assistant from the phone

The app sends to the URL you give it, and nothing else ever sees the data. That
URL has to be reachable from the phone:

- **Home Assistant Cloud (Nabu Casa)** — enable a cloudhook for this webhook under
  Settings ▸ Home Assistant Cloud ▸ Webhooks, and use that URL. It works from
  anywhere.
- **Your own HTTPS endpoint** — a reverse proxy with a valid certificate, or a
  private network such as Tailscale.
- **Local network only** — works while the phone is at home and not otherwise.

Self-signed certificates are not supported: iOS rejects them, and adding a custom
trust path would weaken the one guarantee this project makes about the transport.

## 4. What appears

A device named **Apple Health** with the sensors your client is configured to
send, plus long-term statistics under `apple_health_sync:*`.

If nothing arrives, check in this order: the integration is loaded, the app holds
the same URL and token, and Home Assistant is reachable from the phone's current
network.
