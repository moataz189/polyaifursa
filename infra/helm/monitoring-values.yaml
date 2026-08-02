fullnameOverride: monitoring

prometheus:
  prometheusSpec:
    serviceMonitorSelector: {}
    serviceMonitorNamespaceSelector: {}
    serviceMonitorSelectorNilUsesHelmValues: false

grafana:
  fullnameOverride: grafana
  sidecar:
    dashboards:
      enabled: true
      label: grafana_dashboard
      labelValue: "1"
