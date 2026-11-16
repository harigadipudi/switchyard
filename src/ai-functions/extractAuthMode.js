/**
 * extractAuthMode.js
 * Normalizes Dual-Mode Authentication (Gateway API Key via x-api-key OR Authorization: Bearer <key>,
 * or Google OIDC JWT via Authorization: Bearer <eyJ...>) into unified Apigee flow variables.
 *
 * Sets both dot-scoped variables (`auth.*`) used by features/ai-auth-dual.yaml and
 * flat variables (`auth_mode`, `developer_email`, `product_tier`, `allowed_model_tiers`,
 * `developer_daily_token_quota`) used by downstream JavaScript callouts.
 */
// Tier defaults used when the API product / developer record does not carry an explicit
// daily_token_quota. Keep in sync with infra/variables.tf (standard_tier_daily_token_quota,
// frontier_tier_daily_token_quota), deploy.sh product attributes, and
// features/ai-token-quota.yaml DefaultTokenQuota.
var STANDARD_DEFAULT_QUOTA = "5000000";
var FRONTIER_DEFAULT_QUOTA = "25000000";

function tierDefaultQuota(productTier) {
  return productTier === "coding-agent-frontier"
    ? FRONTIER_DEFAULT_QUOTA
    : STANDARD_DEFAULT_QUOTA;
}

/**
 * Reads a custom attribute from the developer profile returned by the
 * AE-LookupDeveloperByEmail AccessEntity policy. In Apigee X, AccessEntity stores the
 * whole profile (XML, or JSON when OutputFormat=JSON) in a single flow variable, so the
 * attribute is parsed here with a regex (Rhino-safe, no XML/DOM APIs).
 */
function readDeveloperAttribute(profile, name) {
  if (!profile || typeof profile !== "string" || !name) return "";
  var esc = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  var xmlRe = new RegExp(
    "<Attribute>\\s*<Name>\\s*" + esc + "\\s*</Name>\\s*<Value>([^<]*)</Value>\\s*</Attribute>"
  );
  var m = profile.match(xmlRe);
  if (m) return m[1].trim();
  var jsonRe = new RegExp(
    "\"name\"\\s*:\\s*\"" + esc + "\"\\s*,\\s*\"value\"\\s*:\\s*\"([^\"]*)\""
  );
  m = profile.match(jsonRe);
  return m ? m[1].trim() : "";
}

function isJwtFormat(token) {
  if (!token || typeof token !== "string") return false;
  var parts = token.split(".");
  return parts.length === 3 && token.indexOf("eyJ") === 0;
}

function sanitizeAttributionId(val, maxLen, fallback) {
  if (!val || typeof val !== "string") return fallback || "";
  var cleaned = val.replace(/[\r\n\t\0]/g, "").trim();
  if (!cleaned) return fallback || "";
  var safe = cleaned.replace(/[^A-Za-z0-9@._:\-\/]/g, "_");
  if (maxLen && safe.length > maxLen) {
    safe = safe.substring(0, maxLen);
  }
  return safe || fallback || "";
}

function isServiceKeyApp(appName, rawKeyType) {
  var kt = String(rawKeyType || "").toLowerCase().trim();
  if (
    kt === "service_key" ||
    kt === "service_account" ||
    kt === "service" ||
    kt === "ci"
  ) {
    return true;
  }
  var an = String(appName || "").toLowerCase().trim();
  if (!an) return false;
  return (
    /(^|[-_])(service|svc|ci|bot)([-_]app)?$/.test(an) ||
    /-ci-svc(-app)?$/.test(an) ||
    /^svc[-_]/.test(an)
  );
}

function mapAllowedModelsToTiers(allowedModelsOrTiers, productTier) {
  if (!allowedModelsOrTiers) {
    return productTier === "coding-agent-frontier"
      ? "tier1-fast,tier2-daily,tier3-frontier,tier4-open"
      : "tier1-fast,tier2-daily,tier4-open";
  }
  if (allowedModelsOrTiers.indexOf("tier") !== -1) {
    return allowedModelsOrTiers;
  }
  var tiers = ["tier1-fast", "tier2-daily", "tier4-open"];
  if (
    productTier === "coding-agent-frontier" ||
    allowedModelsOrTiers.indexOf("opus") !== -1
  ) {
    tiers.push("tier3-frontier");
  }
  return tiers.join(",");
}

function runExtractAuthMode(context) {
  var apiKeyHeader = (
    context.getVariable("request.header.x-api-key") ||
    context.getVariable("request.header.X-Api-Key") ||
    context.getVariable("request.header.x-goog-api-key") ||
    context.getVariable("request.header.X-Goog-Api-Key") ||
    ""
  ).trim();
  var authHeader = (
    context.getVariable("request.header.authorization") ||
    context.getVariable("request.header.Authorization") ||
    ""
  ).trim();

  var authMode = "none";
  var yamlAuthMode = "none";
  var extractedKey = "";
  var extractedJwt = "";
  var developerEmail = "";
  var quotaOwnerId = "";
  var gatewayKeyName = "unauthenticated";
  var credentialType = "none";
  var userIdSource = "none";
  var productTier = "coding-agent-standard";
  var allowedModelTiers = "tier1-fast,tier2-daily,tier4-open";
  var dailyQuota = STANDARD_DEFAULT_QUOTA;
  var verified = "false";

  if (apiKeyHeader.length > 0 && !isJwtFormat(apiKeyHeader)) {
    authMode = "api_key";
    yamlAuthMode = "apikey";
    extractedKey = apiKeyHeader;
  } else if (apiKeyHeader.length > 0 && isJwtFormat(apiKeyHeader)) {
    // Claude Code's apiKeyHelper sends Google OIDC JWTs in x-api-key as well as Authorization
    authMode = "oidc_jwt";
    yamlAuthMode = "oidc";
    extractedJwt = apiKeyHeader;
  } else if (authHeader.toLowerCase().indexOf("bearer ") === 0) {
    var bearerToken = authHeader.substring(7).trim();
    if (isJwtFormat(bearerToken)) {
      authMode = "oidc_jwt";
      yamlAuthMode = "oidc";
      extractedJwt = bearerToken;
    } else if (bearerToken.length > 0) {
      // Official Antigravity Enterprise Gateway contract sends Authorization: Bearer <GATEWAY_API_KEY>
      authMode = "api_key";
      yamlAuthMode = "apikey";
      extractedKey = bearerToken;
    }
  }

  if (authMode === "api_key") {
    var vaEmail =
      context.getVariable("verifyapikey.VA-VerifyApiKey.developer.email") ||
      context.getVariable("verifyapikey.VA-VerifyDeveloperKey.developer.email") ||
      context.getVariable("developer.email") ||
      "";
    var vaAppName =
      context.getVariable("verifyapikey.VA-VerifyApiKey.app.name") ||
      context.getVariable("verifyapikey.VA-VerifyDeveloperKey.app.name") ||
      context.getVariable("developer.app.name") ||
      "";
    var vaKeyType =
      context.getVariable("verifyapikey.VA-VerifyApiKey.credential_type") ||
      context.getVariable("verifyapikey.VA-VerifyApiKey.key_type") ||
      context.getVariable("app.credential_type") ||
      context.getVariable("app.key_type") ||
      context.getVariable("developer.credential_type") ||
      context.getVariable("verifyapikey.VA-VerifyApiKey.app.attribute.credential_type") ||
      context.getVariable("verifyapikey.VA-VerifyApiKey.app.attribute.key_type") ||
      context.getVariable("verifyapikey.VA-VerifyDeveloperKey.credential_type") ||
      context.getVariable("verifyapikey.VA-VerifyDeveloperKey.app.attribute.credential_type") ||
      context.getVariable("verifyapikey.VA-VerifyApiKey.developer.attribute.credential_type") ||
      context.getVariable("verifyapikey.VA-VerifyApiKey.developer.attribute.key_type") ||
      "";
    var vaProduct =
      context.getVariable("verifyapikey.VA-VerifyApiKey.apiproduct.name") ||
      context.getVariable("verifyapikey.VA-VerifyDeveloperKey.apiproduct.name") ||
      "";
    var vaAllowed =
      context.getVariable("verifyapikey.VA-VerifyApiKey.apiproduct.allowed_model_tiers") ||
      context.getVariable("verifyapikey.VA-VerifyDeveloperKey.apiproduct.allowed_model_tiers") ||
      context.getVariable("verifyapikey.VA-VerifyApiKey.apiproduct.allowedModels") ||
      context.getVariable("verifyapikey.VA-VerifyDeveloperKey.apiproduct.allowedModels") ||
      "";
    var vaQuota =
      context.getVariable("verifyapikey.VA-VerifyApiKey.apiproduct.daily_token_quota") ||
      context.getVariable("verifyapikey.VA-VerifyDeveloperKey.apiproduct.daily_token_quota") ||
      context.getVariable("verifyapikey.VA-VerifyApiKey.apiproduct.llmQuota") ||
      context.getVariable("verifyapikey.VA-VerifyDeveloperKey.apiproduct.llmQuota") ||
      "";

    if (vaEmail || vaProduct) {
      verified = "true";
    }
    var defaultAppFallback = vaEmail
      ? sanitizeAttributionId(vaEmail.split("@")[0] + "-coding-agent-app", 80, "default-gateway-key")
      : "default-gateway-key";
    // Security invariant: never include raw consumer key material in gatewayKeyName.
    gatewayKeyName = sanitizeAttributionId(vaAppName, 80, defaultAppFallback);
    credentialType = isServiceKeyApp(gatewayKeyName, vaKeyType)
      ? "service_key"
      : "user_key";

    var ownerIdentity = vaEmail || gatewayKeyName || "unknown@example.com";
    quotaOwnerId = ownerIdentity;

    // Service Account Keys (CI/shared bots) may delegate end-user FinOps attribution via
    // x-user-id / langfuse-user-id, while quotaOwnerId stays pinned to the service key owner
    // so rotating headers can never bypass <LLMTokenQuota>. Human user_key credentials
    // strictly ignore client user-id headers to prevent developer spoofing.
    var delegatedUser = sanitizeAttributionId(
      context.getVariable("request.header.x-user-id") ||
        context.getVariable("request.header.X-User-Id") ||
        context.getVariable("request.header.langfuse-user-id") ||
        context.getVariable("request.header.Langfuse-User-Id") ||
        "",
      128,
      ""
    );
    if (credentialType === "service_key") {
      if (delegatedUser.length > 0) {
        developerEmail = delegatedUser;
        userIdSource = "client_header";
      } else {
        developerEmail = vaEmail || gatewayKeyName;
        userIdSource = vaEmail ? "service_key_owner" : "service_key_name";
      }
    } else {
      developerEmail = vaEmail || "unknown@example.com";
      userIdSource = "api_key_owner";
    }

    productTier = vaProduct || "coding-agent-standard";
    allowedModelTiers = mapAllowedModelsToTiers(vaAllowed, productTier);
    dailyQuota = vaQuota || tierDefaultQuota(productTier);
  } else if (authMode === "oidc_jwt") {
    var jwtEmail =
      context.getVariable("jwt.VJ-VerifyGoogleOIDC.decoded.claim.email") ||
      context.getVariable("auth.oidc_email") ||
      "";
    var emailVerified =
      context.getVariable("jwt.VJ-VerifyGoogleOIDC.decoded.claim.email_verified") ||
      "";
    var registeredDevEmail =
      context.getVariable("AccessEntity.AE-LookupDeveloperByEmail.Developer.Email") ||
      context.getVariable("AccessEntity.AE-LookupDeveloperByEmail") ||
      context.getVariable("developer.email") ||
      context.getVariable("developer.product_tier") ||
      context.getVariable("auth.oidc_email") ||
      "";
    if (
      jwtEmail &&
      String(emailVerified).toLowerCase() === "true" &&
      registeredDevEmail
    ) {
      verified = "true";
    }
    developerEmail = jwtEmail || "unknown@example.com";
    quotaOwnerId = developerEmail;
    gatewayKeyName = "google-oidc-sso";
    credentialType = "oidc_user";
    userIdSource = "oidc_jwt";

    // AccessEntity exposes the full developer profile as one variable; parse custom
    // attributes from it. Flattened variables are kept as fallbacks for the local emulator.
    var developerProfile = String(
      context.getVariable("AccessEntity.AE-LookupDeveloperByEmail") || ""
    );
    var customTier =
      readDeveloperAttribute(developerProfile, "product_tier") ||
      context.getVariable("AccessEntity.AE-LookupDeveloperByEmail.Developer.Attributes.Attribute.product_tier") ||
      context.getVariable("developer.product_tier") ||
      context.getVariable("auth.oidc_developer_tier") ||
      "coding-agent-standard";
    productTier = customTier;

    if (productTier === "coding-agent-frontier") {
      allowedModelTiers = "tier1-fast,tier2-daily,tier3-frontier,tier4-open";
    } else {
      allowedModelTiers = "tier1-fast,tier2-daily,tier4-open";
    }
    // Optional per-developer override (developer attribute daily_token_quota), else tier default.
    var devQuotaOverride =
      readDeveloperAttribute(developerProfile, "daily_token_quota") ||
      context.getVariable("developer.daily_token_quota") ||
      "";
    dailyQuota = /^[0-9]+$/.test(String(devQuotaOverride))
      ? String(devQuotaOverride)
      : tierDefaultQuota(productTier);
  }

  var regTeam =
    context.getVariable("verifyapikey.VA-VerifyApiKey.developer.team") ||
    context.getVariable("verifyapikey.VA-VerifyApiKey.developer.team_name") ||
    context.getVariable("verifyapikey.VA-VerifyApiKey.developer.attribute.team") ||
    context.getVariable("verifyapikey.VA-VerifyApiKey.app.team") ||
    context.getVariable("verifyapikey.VA-VerifyApiKey.app.attribute.team") ||
    readDeveloperAttribute(developerProfile, "team") ||
    context.getVariable("developer.team") ||
    "";
  var regCostCenter =
    context.getVariable("verifyapikey.VA-VerifyApiKey.developer.cost_center") ||
    context.getVariable("verifyapikey.VA-VerifyApiKey.developer.attribute.cost_center") ||
    context.getVariable("verifyapikey.VA-VerifyApiKey.app.cost_center") ||
    context.getVariable("verifyapikey.VA-VerifyApiKey.app.attribute.cost_center") ||
    readDeveloperAttribute(developerProfile, "cost_center") ||
    context.getVariable("developer.cost_center") ||
    "";

  // Gate C3: Take team and cost center strictly from registry for personal keys; only service_key may delegate via client headers
  var teamName =
    regTeam ||
    (credentialType === "service_key" ? context.getVariable("request.header.x-team-name") : "") ||
    "platform-engineering";
  var costCenter =
    regCostCenter ||
    (credentialType === "service_key" ? context.getVariable("request.header.x-cost-center") : "") ||
    "CC-1000";

  var basePath = String(context.getVariable("proxy.basepath") || "");
  var rawPath =
    context.getVariable("proxy.pathsuffix") ||
    context.getVariable("request.uri") ||
    "";
  var cleanPath = String(rawPath).split("?")[0];
  var fullPath = basePath ? (basePath + cleanPath) : cleanPath;
  // Gate C5: Telemetry is ONLY valid on the Antigravity gateway (basepath /v1beta or /v1beta1).
  // Never allow paths on Claude (/v1/messages) or Codex (/v1/responses) to bypass auth,
  // and require authentication on every path (never bypass auth when authMode === "none").
  var isTelem = false;
  if (/^\/v1beta(1)?/.test(basePath) || /^\/v1(beta|internal):/.test(fullPath)) {
    isTelem = /^(\/v1)?\/v1(beta|internal):[a-zA-Z0-9_]+$/.test(cleanPath) || /^\/v1(beta|internal):[a-zA-Z0-9_]+$/.test(fullPath);
  }
  if (isTelem) {
    context.setVariable("ai.isAntigravityTelemetry", "true");
    context.setVariable("is_telemetry_request", "true");
  } else {
    context.setVariable("ai.isAntigravityTelemetry", "false");
  }

  // Flat variables for JS callouts
  context.setVariable("auth_mode", authMode);
  context.setVariable("developer_email", developerEmail);
  context.setVariable("quota_owner_id", quotaOwnerId || developerEmail);
  context.setVariable("gateway_key_name", gatewayKeyName);
  context.setVariable("credential_type", credentialType);
  context.setVariable("user_id_source", userIdSource);
  context.setVariable("product_tier", productTier);
  context.setVariable("allowed_model_tiers", allowedModelTiers);
  context.setVariable("developer_daily_token_quota", dailyQuota);

  // Scoped variables for features/ai-auth-dual.yaml, <LLMTokenQuota>, and FinOps DataCapture
  context.setVariable("auth.mode", yamlAuthMode);
  context.setVariable("private.extracted_key", extractedKey);
  context.setVariable("private.extracted_jwt", extractedJwt);
  context.setVariable("auth.extracted_key", extractedKey);
  context.setVariable("auth.extracted_jwt", extractedJwt);
  if (verified === "true") {
    context.setVariable("auth.verified", "true");
  }
  context.setVariable("auth.developer_email", quotaOwnerId || developerEmail);
  context.setVariable("auth.developer_id", quotaOwnerId || developerEmail);
  context.setVariable("auth.tier", productTier);
  context.setVariable("auth.product_tier", productTier);
  context.setVariable("auth.allowed_models", allowedModelTiers);
  context.setVariable("auth.llm_quota", dailyQuota);
  context.setVariable("ai.developerEmail", developerEmail);
  context.setVariable("ai.gatewayKeyName", gatewayKeyName);
  context.setVariable("ai.credentialType", credentialType);
  context.setVariable("ai.userIdSource", userIdSource);
  context.setVariable("ai.teamName", teamName);
  context.setVariable("ai.costCenter", costCenter);
  context.setVariable("developer_team", teamName);
  context.setVariable("developer_cost_center", costCenter);
}

if (typeof context !== "undefined") {
  runExtractAuthMode(context);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    isJwtFormat: isJwtFormat,
    isServiceKeyApp: isServiceKeyApp,
    sanitizeAttributionId: sanitizeAttributionId,
    mapAllowedModelsToTiers: mapAllowedModelsToTiers,
    readDeveloperAttribute: readDeveloperAttribute,
    tierDefaultQuota: tierDefaultQuota,
    runExtractAuthMode: runExtractAuthMode
  };
}
