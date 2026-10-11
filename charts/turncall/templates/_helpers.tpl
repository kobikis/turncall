{{- define "turncall.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "turncall.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Selector labels for one component: (dict "ctx" $ "component" "api") */}}
{{- define "turncall.selectorLabels" -}}
app.kubernetes.io/name: {{ .ctx.Chart.Name }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "turncall.image" -}}
{{- $image := merge (dict) (.override | default dict) .ctx.Values.image -}}
{{- printf "%s:%s" $image.repository (default .ctx.Chart.AppVersion $image.tag) -}}
{{- end -}}

{{- define "turncall.secretName" -}}
{{- default (include "turncall.fullname" .) .Values.existingSecret -}}
{{- end -}}

{{/* secretEnv, with the bundled Postgres/Redis URLs under any it does not set. */}}
{{- define "turncall.secretEnv" -}}
{{- $env := dict -}}
{{- $full := include "turncall.fullname" . -}}
{{- with .Values.postgresql }}{{ if .enabled -}}
{{- $_ := set $env "DATABASE_URL" (printf "postgresql+asyncpg://%s:%s@%s-postgresql:5432/%s" .user (urlquery .password) $full .database) -}}
{{- end }}{{ end -}}
{{- if .Values.redis.enabled -}}
{{- $_ := set $env "REDIS_URL" (printf "redis://%s-redis:6379/0" $full) -}}
{{- end -}}
{{- toYaml (merge (deepCopy .Values.secretEnv) $env) -}}
{{- end -}}

{{- define "turncall.checkSecrets" -}}
{{- if not (or .Values.existingSecret .Values.secretEnv.DATABASE_URL .Values.postgresql.enabled) -}}
{{- fail "set secretEnv.DATABASE_URL, existingSecret naming a Secret that holds it, or postgresql.enabled" -}}
{{- end -}}
{{- end -}}

{{/* Pod-level settings every TurnCall process shares. */}}
{{- define "turncall.podSpec" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
# Service links would inject TURNCALL_* variables beside TURNCALL_ENV.
enableServiceLinks: false
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  # A fresh volume (localStorage) is root-owned; this makes it the app's.
  fsGroup: 10001
{{- end -}}

{{/* envFrom + the env every TurnCall container gets. */}}
{{- define "turncall.env" -}}
envFrom:
  - configMapRef:
      name: {{ include "turncall.fullname" . }}
  - secretRef:
      name: {{ include "turncall.secretName" . }}
{{- end -}}

{{/* Restart pods when their configuration changes. */}}
{{- define "turncall.configChecksum" -}}
checksum/config: {{ include (print .Template.BasePath "/configmap.yaml") . | sha256sum }}
checksum/secret: {{ include (print .Template.BasePath "/secret.yaml") . | sha256sum }}
{{- end -}}

{{- define "turncall.scheduling" -}}
{{- with .nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
