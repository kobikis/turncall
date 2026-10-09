{{- define "turncall.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
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

{{- define "turncall.checkSecrets" -}}
{{- if not (or .Values.existingSecret .Values.secretEnv.DATABASE_URL) -}}
{{- fail "set secretEnv.DATABASE_URL, or existingSecret naming a Secret that holds it" -}}
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
