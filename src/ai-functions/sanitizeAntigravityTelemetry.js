/**
 * sanitizeAntigravityTelemetry.js
 * Enforces Google Antigravity Enterprise Gateway telemetry & header rules:
 *   1. Detects telemetry endpoints (`POST /v1beta:*` such as `/v1beta:recordClientEvent`,
 *      as well as legacy `/v1internal:*`) so they can be acknowledged with HTTP 200 `{}`
 *      without triggering Vertex AI inference.
 *   2. Preserves incoming `User-Agent`, `X-Goog-Api-Client`, and custom headers (`X-*`)
 *      unchanged without fabricating attribution headers when missing.
 *   3. Strips optional non-Vertex client telemetry envelopes from inference payloads if present.
 */
function isTelemetryPath(pathOrUri) {
  if (!pathOrUri || typeof pathOrUri !== "string") return false;
  var cleanPath = pathOrUri.split("?")[0];
  return /^(\/v1)?\/v1(beta|internal):[a-zA-Z0-9_]+$/.test(cleanPath);
}

function runSanitizeAntigravityTelemetry(context) {
  // Never fabricate User-Agent or X-Goog-Api-Client when missing; forward only what the client sent.
  var userAgent =
    context.getVariable("request.header.user-agent") ||
    context.getVariable("request.header.User-Agent") ||
    "";
  var googApiClient =
    context.getVariable("request.header.x-goog-api-client") ||
    context.getVariable("request.header.X-Goog-Api-Client") ||
    "";

  if (userAgent) {
    context.setVariable("antigravity_user_agent", userAgent);
  }
  if (googApiClient) {
    context.setVariable("antigravity_goog_api_client", googApiClient);
  }

  var basePath = String(context.getVariable("proxy.basepath") || "");
  var pathSuffix =
    context.getVariable("proxy.pathsuffix") ||
    context.getVariable("request.uri") ||
    "";
  var cleanSuffix = String(pathSuffix).split("?")[0];
  var normalizedBase = basePath === "/" ? "" : basePath;
  var fullPath = normalizedBase ? (normalizedBase + cleanSuffix) : cleanSuffix;
  var isTelem = false;
  if (
    basePath === "/" ||
    /^\/v1beta(1)?/.test(basePath) ||
    /^\/v1(beta|internal):/.test(fullPath)
  ) {
    isTelem = isTelemetryPath(cleanSuffix) || isTelemetryPath(fullPath);
  }
  if (isTelem) {
    context.setVariable("is_telemetry_request", "true");
    context.setVariable("antigravity.is_telemetry", "true");
    context.setVariable("ai.isAntigravityTelemetry", "true");
    return;
  }
  context.setVariable("is_telemetry_request", "false");
  context.setVariable("antigravity.is_telemetry", "false");
  context.setVariable("ai.isAntigravityTelemetry", "false");

  var rawBody = context.getVariable("request.content");
  if (!rawBody || rawBody.length === 0) {
    return;
  }

  try {
    var parsed = JSON.parse(rawBody);
    // Strip optional non-Vertex client telemetry envelopes if attached by IDE plugins
    if (parsed && typeof parsed === "object") {
      var modified = false;
      if ("clientTelemetry" in parsed) {
        delete parsed.clientTelemetry;
        modified = true;
      }
      if ("internalTraceMetadata" in parsed) {
        delete parsed.internalTraceMetadata;
        modified = true;
      }
      if ("telemetryEvents" in parsed) {
        delete parsed.telemetryEvents;
        modified = true;
      }
      if (!parsed.model) {
        var modelMatch = fullPath.match(/\/models\/([^/:?]+)/);
        if (modelMatch && modelMatch[1]) {
          parsed.model = modelMatch[1];
          modified = true;
        }
      }
      if (modified) {
        context.setVariable("request.content", JSON.stringify(parsed));
      }
    }
  } catch (e) {
    // Leave payload untouched if not valid JSON; downstream validation handles malformed JSON
  }
}

if (typeof context !== "undefined") {
  runSanitizeAntigravityTelemetry(context);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    isTelemetryPath: isTelemetryPath,
    runSanitizeAntigravityTelemetry: runSanitizeAntigravityTelemetry
  };
}
