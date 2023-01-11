# Ansible Role: CZERTAINLY

Ansible role to install [CZERTAINLY](https://www.czertainly.com/). For complex usage example please refer
to [CZERTAINLY-Appliance](../../../CZERTAINLY-Appliance).

## Requirements

A Linux system with access to the Internet and configured Kubernetes cluster with localy installed and configured `kubectl` for controling the Kubernetes cluster.

## Example Playbook

```
- name: czertainly host config
  hosts: all
  connection: local
  
  roles:
    - role: czertainly
```

For more detailed example please look at [playbook](../CZERTAINLY-Appliance/blob/http_proxy/files/czertainly.yml) for installing CZERTAINLY in Appliance.
