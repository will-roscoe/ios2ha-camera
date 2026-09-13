# Installation

:::{note}
Scaffolded, not yet written. The content below is the outline these guides need to
cover; each heading is a page's worth of prose that still has to be written.
:::

## Before you start: the phone

The prerequisites that are easiest to get wrong, and that make the service look
broken when they are missing:

- Background App Refresh **on**. Without it iOS kills the camera app rather than
  suspending it, and the app has to be restarted by hand.
- Auto-Lock set to **Never**.
- A fixed IP address. The app binds to whatever address it had at launch, so a DHCP
  change means restarting the app.
- The app's own quality settings, which materially change both image quality and
  the power the phone draws.

## Docker Compose alongside a media stack

What runs today. Covers the usbmuxd socket mount and why it needs care.

## Standalone Docker

The same image with no other services around it.

## Bare Python

For development, or a host where Docker is not wanted.

## Connecting it to Home Assistant

See {doc}`../home-assistant/index`. The short version: MQTT plus two camera entries,
or the native integration once it exists.
