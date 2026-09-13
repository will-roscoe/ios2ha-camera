# Connecting over MQTT

:::{note}
Scaffolded, not yet written. The outline below records the decisions this page has
to convey; the prose still has to be written.
:::

## You do not need a Home Assistant account

The supervisor authenticates to the **broker**, not to Home Assistant. Use a
broker-local login: the Mosquitto add-on's `logins` option, or any broker's own
password file.

This matters because requiring a Home Assistant user is unusual for a Home Assistant
app and hands the supervisor a login that reaches far beyond MQTT. A broker-local
account can do exactly one thing.

Points this page must make:

- How to add a login to the Mosquitto add-on, and that **add-on login passwords are
  stored in plain text** in the add-on options.
- Narrowing the account with an ACL (`customize.active` plus `/share/mosquitto`), as
  the hardened option.
- That the broker need not be Home Assistant's at all.

## Adding the camera entities

The two MJPEG endpoints, `/live` and `/live/stacked`, are not discovered over MQTT
and have to be added as camera entries by hand.

## Alerts worth having

Cross-reference {doc}`../supervisor/operations`, which already lists the five.
