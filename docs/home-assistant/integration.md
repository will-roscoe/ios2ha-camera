# The native integration

:::{note}
Not built. This page is a placeholder so the documentation structure does not have
to be rearranged when it arrives.
:::

A Home Assistant custom component, installed through HACS, that talks to the
supervisor directly instead of through a broker.

It will live in **its own repository**, not this one. HACS requires `hacs.json` and
`custom_components/<domain>/` at a repository's root, and selects a version from the
*most recent GitHub Release* in that repository, with no way to filter releases by
tag prefix. Sharing a repository would therefore let a supervisor release be offered
to integration users as an upgrade.
