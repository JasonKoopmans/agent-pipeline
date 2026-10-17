{{- define "agent-pipeline.name" -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{- define "agent-pipeline.labels" -}}
app.kubernetes.io/name: agent-pipeline
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/* Merged role values: roleDefaults overlaid with the role's own. Args: dict root, role */}}
{{- define "agent-pipeline.role" -}}
{{- mergeOverwrite (deepCopy .root.Values.roleDefaults) (deepCopy .role) | toJson -}}
{{- end -}}

{{/* PAT fallback secret for a role kind: writer kinds push code, everyone else only comments. */}}
{{- define "agent-pipeline.githubSecret" -}}
{{- if has .kind (list "implementer" "fixer" "merger") -}}
{{- .root.Values.secrets.githubWriter | toJson -}}
{{- else -}}
{{- .root.Values.secrets.githubCommenter | toJson -}}
{{- end -}}
{{- end -}}

{{/* Image for a variant. Args: dict root, variant */}}
{{- define "agent-pipeline.image" -}}
{{- printf "%s:%s-%s" .root.Values.image.repository .root.Values.image.version .variant -}}
{{- end -}}

{{/* Variant for a role's agent/verify containers. */}}
{{- define "agent-pipeline.roleVariant" -}}
{{- if .role.image -}}{{ .role.image }}
{{- else if has .role.kind (list "planner" "designer" "merger") -}}core
{{- else -}}{{ .root.Values.defaultStack }}
{{- end -}}
{{- end -}}

{{/* GitHub credential env for prepare/publish: the role's App (key file mounted at
/run/github-app) or the PAT fallback. Args: dict role, pat */}}
{{- define "agent-pipeline.githubEnv" -}}
{{- if .role.appSecret -}}
- {name: GITHUB_APP_ID, valueFrom: {secretKeyRef: {name: {{ .role.appSecret }}, key: app-id}}}
- {name: GITHUB_APP_PRIVATE_KEY_FILE, value: /run/github-app/private-key}
{{- else -}}
- {name: GH_TOKEN, valueFrom: {secretKeyRef: {name: {{ .pat.name }}, key: {{ .pat.key }}}}}
{{- end -}}
{{- end -}}
