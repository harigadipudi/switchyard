output "gateway_url" {
  description = "Single Enterprise Gateway URL shared by Claude Code, Google Antigravity, and Codex CLI/Desktop."
  value       = "https://${local.gateway_hostname}"
}

output "apigee_org" {
  description = "Apigee X Organization name."
  value       = local.apigee_org_name
}

output "apigee_env" {
  description = "Apigee X Environment name."
  value       = var.apigee_env
}

output "runtime_service_account_email" {
  description = "Least-privilege Google Service Account used by Apigee TargetEndpoints to authenticate to Vertex AI and Model Armor."
  value       = google_service_account.apigee_vertex_runtime.email
}

output "api_products" {
  description = "Provisioned Apigee API Products enforcing <LLMTokenQuota> and model RBAC."
  value = {
    standard = google_apigee_api_product.coding_agent_standard.name
    frontier = google_apigee_api_product.coding_agent_frontier.name
  }
}

output "deployed_proxies" {
  description = "Deployed Apigee X API proxy revisions in the target environment."
  value = {
    for k, v in google_apigee_environment_api_revision_deployment.proxy_deployments :
    k => {
      environment = v.environment
      revision    = v.revision
    }
  }
}

output "fetch_developer_api_key_command" {
  description = "Command to securely retrieve a developer's Apigee API Key without storing plaintext secrets in Terraform state logs."
  value       = "curl -sS -H \"Authorization: Bearer $(gcloud auth print-access-token)\" \"https://apigee.googleapis.com/v1/organizations/${local.apigee_org_name}/developers/dev-standard@example.com/apps/alex-rivera-coding-agent-app\" | jq -r '.credentials[0].consumerKey'"
}

output "finops_cloud_monitoring_dashboard_id" {
  description = "Resource ID of the native GCP Cloud Monitoring Coding Agent Enterprise FinOps Dashboard."
  value       = google_monitoring_dashboard.coding_agent_finops_dashboard.id
}

output "finops_alert_policy_ids" {
  description = "Cloud Monitoring FinOps spend, overkill waste, and quota alert policy IDs."
  value = {
    daily_net_cash_spend = var.enable_finops_alerts ? google_monitoring_alert_policy.finops_daily_net_cash_spend[0].id : null
    avoidable_overkill   = var.enable_finops_alerts ? google_monitoring_alert_policy.finops_avoidable_overkill[0].id : null
    gateway_quota_429    = var.enable_finops_alerts ? google_monitoring_alert_policy.finops_gateway_429_surge[0].id : null
  }
}

output "finops_bigquery_dataset" {
  description = "BigQuery dataset ID for SQL FinOps chargeback when enable_finops_bigquery_export is enabled."
  value       = var.enable_finops_bigquery_export ? google_bigquery_dataset.finops_analytics[0].dataset_id : null
}
