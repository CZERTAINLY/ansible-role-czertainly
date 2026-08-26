# Ansible Role: ILM

Ansible role to install [ILM](https://www.czertainly.com/). For complex usage example please refer
to [ILM-Appliance](../../../CZERTAINLY-Appliance).

## Requirements

A Linux system with access to the Internet and configured Kubernetes cluster with localy installed and configured `kubectl` for controling the Kubernetes cluster.

## Role Variables

| variable | default |
|---|---|
| `app_namespace` | `ilm` |
| `app_release_name` | `ilm` |
| `appliance_user` / `appliance_group` | `ilm` |
| `ilm_ingress_class` | `traefik` |
| `ilm_traefik_plugin_name` | `certheaderencode` |
| `ilm_traefik_plugin_module` | `github.com/semik/ansible-role-ilm/certheaderencode` |

## Client certificate login

The chart is written for ingress-nginx: it annotates its Ingress with
`nginx.ingress.kubernetes.io/auth-tls-*` and core expects the client
certificate in the header the way ingress-nginx writes it with
`$ssl_client_escaped_cert`, which is a percent-encoded PEM. RKE2 deploys
Traefik instead since v1.36, so with `ilm_ingress_class: traefik` the role
configures the same thing the Traefik way, in `tasks/traefik.yml`:

- a `TLSOption` that asks for a client certificate and verifies it against
  the `trusted-certificates` secret of the chart, `VerifyClientCertIfGiven`
  so that password login stays reachable,
- a `passTLSClientCert` middleware that writes the certificate into
  `X-Forwarded-Tls-Client-Cert`,
- a middleware of the `certheaderencode` plugin from
  [files/certheaderencode](files/certheaderencode), which percent-encodes that
  header.

The plugin is needed because core URL-decodes the header before it decodes the
base64, while Traefik sends plain base64. Every `+` of the base64 arrives as a
space and authentication fails with `Invalid certificate header: Illegal
base64 character 20`. Traefik has no option to encode the value, so the plugin
is loaded as a local plugin through a `HelmChartConfig` for `rke2-traefik`,
with its source inline - no image and no download at cluster start.

Plugins of the traefik catalogue are fetched from GitHub every time traefik
starts, and a failed fetch only disables the plugin, which would break
certificate login without saying so. A plugin of the catalogue would have to
be vendored here to avoid that, and then it is third party Go source in the
package for what is one call to `url.QueryEscape`.

A generic header rewriter, e.g.
[bitrvmpd/traefik-plugin-rewrite-headers](https://github.com/bitrvmpd/traefik-plugin-rewrite-headers),
can do the same with a rewrite of `\+` to `%2B` - that is enough because `/`
and `=` survive URL-decoding and base64 holds no `%`. Two reasons against it:
it wraps the ResponseWriter of every request through the router, also when
only request rewrites are configured, and that wrapper implements neither
`http.Flusher` nor `http.Hijacker`, so streaming and websockets on the same
router lose them. And the rewrite only escapes what breaks today - a traefik
release that keeps the PEM markers in the header would bring the spaces back,
while encoding the value is the inverse of what core does with it whatever it
contains.

The role also carries the trusted certificates of the chart over to
`global.trusted.certificates`, which is what mounts them into the pods of the
subcharts. Without it the auth service validates the client certificate
against the stock CA bundle of its image and rejects it with `unable to get
local issuer certificate`. Set `trustedCA_file` to use a CA of your own
instead.

Setting `ilm_ingress_class: nginx` skips all of it, the annotations of the
chart are the ingress-nginx ones already.

## Example Playbook

```
- name: ilm host config
  hosts: all
  connection: local

  roles:
    - role: ilm
```

For more detailed example please look at [playbook](../../../CZERTAINLY-Appliance/blob/http_proxy/files/czertainly.yml) for installing ILM in Appliance.
