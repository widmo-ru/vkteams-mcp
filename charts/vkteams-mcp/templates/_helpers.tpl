{{- define "vkteams-mcp.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "vkteams-mcp.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "vkteams-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "vkteams-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "vkteams-mcp.labels" -}}
{{ include "vkteams-mcp.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "vkteams-mcp.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}
{{- end -}}

{{/* "true" when a shared store is configured: a URL or Sentinels. */}}
{{- define "vkteams-mcp.sharedState" -}}
{{- if or .Values.state.url .Values.state.sentinel.hosts -}}true{{- end -}}
{{- end -}}

{{/* "true" when the number of pods can be more than one. */}}
{{- define "vkteams-mcp.manyPods" -}}
{{- if or (gt (int .Values.replicaCount) 1) (and .Values.autoscaling.enabled (or (gt (int .Values.autoscaling.minReplicas) 1) (gt (int .Values.autoscaling.maxReplicas) 1))) -}}true{{- end -}}
{{- end -}}

{{- define "vkteams-mcp.validate" -}}
{{- $v := .Values -}}
{{- if not (hasPrefix "https://" $v.publicUrl) -}}{{- fail "publicUrl is required and must start with https://" -}}{{- end -}}
{{- if not $v.key.existingSecret -}}
{{- fail "key.existingSecret is required: the Secret CI writes with the sealing key (scripts/new-sealing-key.sh)" -}}
{{- end -}}
{{- if and $v.state.url $v.state.sentinel.hosts -}}
{{- fail "state.url and state.sentinel.hosts are both set: use the URL for one Redis or Valkey, the Sentinels for a Sentinel setup, not both" -}}
{{- end -}}
{{- if and $v.state.url (not (or (hasPrefix "redis://" $v.state.url) (hasPrefix "rediss://" $v.state.url))) -}}
{{- fail "state.url must start with redis:// or rediss://" -}}
{{- end -}}
{{- if and $v.state.url (contains "@" $v.state.url) -}}
{{- fail "state.url carries a user or password: a password in a variable is visible to kubectl exec. Use state.username and state.existingSecret (key password)" -}}
{{- end -}}
{{- range $h := $v.state.sentinel.hosts -}}
{{- if not (regexMatch "^[^:@/ ]+(:[0-9]+)?$" $h) -}}{{- fail (printf "state.sentinel.hosts: %q is not host:port" $h) -}}{{- end -}}
{{- end -}}
{{- if and (include "vkteams-mcp.manyPods" .) (not (include "vkteams-mcp.sharedState" .)) -}}
{{- fail "more than one replica needs a shared state store: set state.url or state.sentinel.hosts. Without one, a sign-in code could be redeemed on every replica and every limit would be multiplied by the number of replicas" -}}
{{- end -}}
{{- if lt (int $v.replicaCount) 1 -}}{{- fail "replicaCount must be at least 1" -}}{{- end -}}
{{- if and $v.autoscaling.enabled (gt (int $v.autoscaling.minReplicas) (int $v.autoscaling.maxReplicas)) -}}
{{- fail "autoscaling.minReplicas is above autoscaling.maxReplicas" -}}
{{- end -}}
{{- if and $v.autoscaling.enabled (not $v.autoscaling.targetCPUUtilizationPercentage) (not $v.autoscaling.inFlightPerPod) -}}
{{- fail "autoscaling.enabled needs a target: targetCPUUtilizationPercentage or inFlightPerPod" -}}
{{- end -}}
{{- if not $v.ingress.paths -}}{{- fail "ingress.paths is empty: list the paths to publish; `/` is not allowed, it would publish the probes and /metrics" -}}{{- end -}}
{{- range $p := $v.ingress.paths -}}
{{- if or (eq $p "/") (not (hasPrefix "/" $p)) -}}{{- fail (printf "ingress.paths: %q is not an explicit path" $p) -}}{{- end -}}
{{- end -}}
{{- range $p := $v.trustedProxies -}}
{{- if not (regexMatch "^[0-9a-fA-F:.]+(/[0-9]+)?$" $p) -}}{{- fail (printf "trustedProxies: %q is not an address or a CIDR" $p) -}}{{- end -}}
{{- end -}}
{{- if or (eq (int $v.metrics.port) 8000) (lt (int $v.metrics.port) 1) -}}
{{- fail "metrics.port must be another port than 8000: metrics are never served on the public port" -}}
{{- end -}}
{{- range $k, $_ := $v.podLabels -}}
{{- if has $k (list "app.kubernetes.io/name" "app.kubernetes.io/instance") -}}{{- fail (printf "podLabels.%s is set by the chart: the Deployment selects its pods by it" $k) -}}{{- end -}}
{{- end -}}
{{- if hasKey $v.podAnnotations "vkteams-mcp/key-revision" -}}{{- fail "podAnnotations.vkteams-mcp/key-revision is set by the chart from key.revision" -}}{{- end -}}
{{- end -}}
