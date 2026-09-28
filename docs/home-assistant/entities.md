# Entities

Every entity the ios2ha-camera supervisor publishes, grouped by domain: its id,
what it shows or controls, and its range, unit, device class and category. This
table is generated at build time from `ios2ha_camera.entities`, the same
declaration the supervisor renders into MQTT discovery payloads and into the
JSON API's `GET /api/v1/objects`, so it cannot drift from what is actually
published. This is the public interface: see {doc}`../guarantees`.

An entity id below is `<domain>.ios2ha_camera_<object_id>`, as Home Assistant
builds it from the MQTT discovery payload; the JSON API serves the same
`object_id` and `domain` as separate fields rather than joining them, because
`object_id` alone is not always unique — `screenshot` is both a camera and a
button.

The native component also adds two cameras that are not in this table because
they are video routes rather than entities the service declares:
`camera.ios2ha_camera_live`, the phone's stream re-served as it arrives, and
`camera.ios2ha_camera_stacked`, the same stream averaged over a few seconds for
a cleaner picture that smears movement. Either keeps the phone connected, and
awake, for as long as someone watches it.

```{entities-table}
```
