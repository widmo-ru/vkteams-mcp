# vkteams-mcp chart

The VK Teams MCP server: many replicas, a shared state store, JSON logs, Prometheus metrics. The story behind each
setting (the key, the store, probes, metrics, ingress, security review) is in
[deploy/README.en.md](../../deploy/README.en.md) ([по-русски](../../deploy/README.md)).

```sh
helm upgrade --install vkteams-mcp oci://ghcr.io/widmo-ru/charts/vkteams-mcp --version 0.4.0 -n vkteams -f values.yaml
```

Required: `publicUrl` (https), `key.existingSecret`, and, for the default two replicas, a store: `state.url` or
`state.sentinel.hosts`. The chart refuses a render without them, and refuses `ingress.paths: ["/"]`, a URL and Sentinels
together, a password inside `state.url`, and `metrics.port: 8000`.

| Key | Default | Meaning |
|---|---|---|
| `image.repository`, `tag`, `digest`, `pullPolicy`, `imagePullSecrets` | `ghcr.io/widmo-ru/vkteams-mcp`, appVersion | the image; a digest wins over a tag |
| `publicUrl` | required | the https address clients reach the server on |
| `vkteams.installations` | `[]` | pin the connector to these VK Teams hosts |
| `vkteams.devId`, `timezone`, `allowPrivateNetworks` | `""`, `Europe/Moscow`, `false` | see the README |
| `vkteams.caBundle.configMap`, `key` | `""`, `ca.crt` | a CA file for VK Teams (`VKTEAMS_MCP_CA_BUNDLE`) |
| `areas` | `[]` | areas to publish; empty is the default set |
| `logLevel` | `INFO` | `LOG_LEVEL` |
| `trustedProxies` | `[]` | CIDRs that may set `X-Forwarded-For`; empty turns the per-network limit off |
| `key.existingSecret`, `secretKey`, `revision` | required, `key`, `""` | the sealing key Secret (stable), the field, and the id that restarts pods on rotation |
| `state.url` / `state.sentinel.hosts`, `.master` | `""` / `[]`, `mymaster` | Redis or Valkey, standalone or Sentinel |
| `state.db`, `tls`, `username`, `prefix` | `0`, `false`, `""`, `""` | |
| `state.existingSecret` | `""` | a Secret with `password` and `sentinelPassword` (both optional) |
| `replicaCount` | `2` | |
| `autoscaling.*` | off | HPA; `inFlightPerPod` needs a metrics adapter |
| `podDisruptionBudget.*` | `maxUnavailable: 1` | |
| `metrics.port`, `metrics.serviceMonitor.*` | `9090`, off | the metrics port, and an optional ServiceMonitor (with a `-metrics` Service) |
| `service.*`, `ingress.*` | ClusterIP 80, off | `ingress.paths` lists explicit paths |
| `resources` | 50m / 128Mi, memory limit 512Mi | no CPU limit |
| `tmp.medium`, `tmp.sizeLimit` | `Memory`, `16Mi` | `/tmp` of the server |
| `podAnnotations`, `podLabels`, `nodeSelector`, `tolerations`, `affinity`, `topologySpreadConstraints` | | empty affinity and spread: soft, by node |

No NetworkPolicy is templated. The Secrets are mounted into the init container `stage-keys` only.
