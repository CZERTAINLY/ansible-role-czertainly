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
| `ilm_private_components` | the components pulled from `ilm-private` |
| `ilm_db_wait_seconds` | `300` |
| `ilm_ingress_class` | `traefik` |
| `ilm_traefik_plugin_name` | `certheaderencode` |
| `ilm_traefik_plugin_module` | `github.com/OmniTrustILM/ansible-role-ilm/certheaderencode` |
| `ilm_cbom_release_name` | `cbom-repository` |
| `ilm_cbom_repository_version` | `1.1.0` |
| `ilm_cbom_data_dir` | `/var/lib/ilm/cbom-repo` |
| `ilm_cbom_data_owner` | `1000`, the uid minio runs as |
| `ilm_cbom_storage_size` | `10Gi` |
| `ilm_cbom_bucket` | `cbom-repo` |
| `ilm_cbom_console` | `false` |

## Components

Which components are installed comes from the `ilm` dict of
`/etc/ilm-ansible/vars/ilm.yml`, which the appliance TUI writes. Every key is
optional, a missing one means the component is not installed.

The components listed in `ilm_private_components` come from the `ilm-private`
repository of the registry, unlike every other one, so they only pull once
`docker.username` and `docker.password` are configured in
`/etc/ilm-ansible/vars/docker.yml`. They are shipped switched off for that
reason, and enabling one without credentials fails the play before anything is
installed - otherwise their pods would only reach `ImagePullBackOff`. The
appliance TUI marks the same components with an asterisk, reading this very
list, so a component becomes private in one place.

## Waiting for the database

Helm starts every pod at once, while pg-bouncer - which all of them reach the
database through - takes about as long as its image pull to come up. The
components that talk to the database while starting die in the meantime and
are restarted until it answers: `auth` restarted three times and
`cryptosense-discovery-provider` once on a measured install, with
`Failed to connect to <pg-bouncer>:5432, Connection refused` in their logs.

The role therefore renders a `wait-for-database` init container into the
values of every component that uses the database, in the style the chart uses
for its own `wait-for-auth-service` and `wait-for-messaging-service`: the
`curl` image of the chart running `nc -z` in a loop. Custom init containers
are appended to the ones the chart brings itself, so core keeps waiting for
the auth service and scheduler-service for the messaging service.

The image and the endpoint come from the defaults of the chart being
installed, not from anything written down here, so they cannot go stale.
`ilm_db_wait_seconds` bounds the wait; on timeout the container exits with a
message in `kubectl logs <pod> -c wait-for-database` and the kubelet retries
it.

Components that do not use the database - the api gateway, the frontend, the
OPA policies, both message brokers, the utils service, `x509ComplianceProvider`,
`otpkiConnector` and `timestampFormattingConnector` - are deliberately left
alone, and so is pg-bouncer itself, which would otherwise wait for the service
it is.

## CBOM repository

`cbomRepository` is not a component of the ilm chart, it has a chart and a
version of its own, so the role installs it as a second release into the same
namespace, see [tasks/cbom-repository.yml](tasks/cbom-repository.yml).

Its CBOMs live in an object store - the bundled minio - and not in the
database that everything else on the appliance is rebuilt from. Volumes of the
default storage class are provisioned under `/opt/local-path-provisioner` with
a name derived from the claim, so a wipe of RKE2 would orphan them and the
data would be gone from the point of view of the new cluster. The role
therefore creates a `PersistentVolume` bound to `ilm_cbom_data_dir` on the
host with `persistentVolumeReclaimPolicy: Retain`, and hands minio the claim
of it through `minio.persistence.existingClaim`. After
`rke2-uninstall.sh` and another run of the playbook the volume is recreated
over the same directory and the CBOMs are still there - as are the storage
credentials, generated once into `/etc/ilm-ansible/vars/cbom.yml`.

So the backup of the CBOMs is a copy of `ilm_cbom_data_dir`, the way the
backup of everything else is a dump of the database.

Reinstalling ILM deletes the namespace and with it the claim, while `Retain`
keeps the volume - as `Released`, still carrying the uid of the claim that is
gone. A new claim of the same name is refused with `volume already bound to a
different claim` and minio waits for a volume that never binds, so the role
drops that reference before it recreates the claim. Nothing has to be done by
hand after a reinstall.

Switching the component off removes the release but leaves the volume, the
claim and the directory alone, so switching it on again picks the CBOMs back
up. Removing the data is deliberate and manual:

```
kubectl delete pvc/cbom-repository-data pv/cbom-repository-data -n ilm
rm -rf /var/lib/ilm/cbom-repo
```

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
