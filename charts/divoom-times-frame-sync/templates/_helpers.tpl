{{- define "tfs.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tfs.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains (include "tfs.name" .) .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "tfs.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "tfs.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tfs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "tfs.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "tfs.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "tfs.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default (printf "v%s" .Chart.AppVersion)) -}}
{{- end -}}
{{- end -}}

{{- define "tfs.validate" -}}
{{- $_ := required "Set config.frame.host to the frame's reachable IPv4 address or hostname" .Values.config.frame.host -}}
{{- $_ := required "Set existingSecret to a Secret containing DIVOOM_TOKEN and source URL/password variables" .Values.existingSecret -}}
{{- $ids := dict -}}
{{- range .Values.sources -}}
{{- $id := lower .id -}}
{{- if hasKey $ids $id -}}{{- fail (printf "Duplicate source id: %s" .id) -}}{{- end -}}
{{- $_ := set $ids $id true -}}
{{- end -}}
{{- end -}}

{{- define "tfs.dataOwner" -}}
{{- $uid := .Values.podSecurityContext.runAsUser -}}
{{- $gid := .Values.podSecurityContext.runAsGroup -}}
{{- if hasKey .Values.securityContext "runAsUser" -}}
{{- $uid = .Values.securityContext.runAsUser -}}
{{- end -}}
{{- if hasKey .Values.securityContext "runAsGroup" -}}
{{- $gid = .Values.securityContext.runAsGroup -}}
{{- end -}}
{{- if or (eq $uid nil) (le ($uid | int64) 0) (eq $gid nil) -}}
{{- fail "volumePermissions requires an explicit non-root runAsUser and runAsGroup for the sync container" -}}
{{- end -}}
{{- printf "%d:%d" ($uid | int64) ($gid | int64) -}}
{{- end -}}
