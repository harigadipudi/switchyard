/**
 * prepareVertexFallbackPayload.js
 * Stage 05 Resiliency Policy:
 * When the primary Vertex AI Model Garden endpoint returns HTTP 429 (Quota Exhausted)
 * or HTTP 500/502/503/504, this policy re-targets the request to the high-availability
 * fallback model (`gemini-3.1-pro-preview` on global endpoint `aiplatform.googleapis.com`)
 * and translates the full original client request (system instructions, multi-turn history,
 * tool calls, tool results, and tool schemas) into Vertex Gemini `generateContent` format.
 */

var transformMod = null;
if (typeof require === "function") {
  try {
    transformMod = require("./transformPayload");
  } catch (e) {
    transformMod = null;
  }
}

function normalizeFallbackSchema(schema) {
  if (!schema || typeof schema !== "object") {
    return { type: "object", properties: {} };
  }
  if (Array.isArray(schema)) {
    var arr = [];
    for (var i = 0; i < schema.length; i++) {
      arr.push(
        typeof schema[i] === "object" && schema[i] !== null
          ? normalizeFallbackSchema(schema[i])
          : schema[i]
      );
    }
    return arr;
  }
  var out = {};
  var keys = Object.keys(schema);
  for (var k = 0; k < keys.length; k++) {
    var key = keys[k];
    var val = schema[key];
    if (key === "type" && typeof val === "string") {
      out[key] = val.toLowerCase();
    } else if (key === "type" && Array.isArray(val)) {
      var tList = [];
      for (var ti = 0; ti < val.length; ti++) {
        tList.push(typeof val[ti] === "string" ? val[ti].toLowerCase() : val[ti]);
      }
      out[key] = tList;
    } else if (key === "required" || key === "enum") {
      out[key] = Array.isArray(val) ? val.slice() : val;
    } else if (key === "properties" && val && typeof val === "object" && !Array.isArray(val)) {
      var propOut = {};
      var propKeys = Object.keys(val);
      for (var p = 0; p < propKeys.length; p++) {
        var pName = propKeys[p];
        propOut[pName] =
          val[pName] && typeof val[pName] === "object"
            ? normalizeFallbackSchema(val[pName])
            : val[pName];
      }
      out[key] = propOut;
    } else if (val && typeof val === "object") {
      out[key] = normalizeFallbackSchema(val);
    } else {
      out[key] = val;
    }
  }
  return out;
}

function ensureGemini3FunctionCallSignatures(payload) {
  if (!payload || !Array.isArray(payload.contents)) return payload;
  for (var i = 0; i < payload.contents.length; i++) {
    var turn = payload.contents[i];
    if (!turn || !Array.isArray(turn.parts)) continue;
    var seenFcInTurn = false;
    for (var j = 0; j < turn.parts.length; j++) {
      var part = turn.parts[j];
      if (part && part.functionCall) {
        if (!seenFcInTurn) {
          seenFcInTurn = true;
          if (!part.thoughtSignature) {
            part.thoughtSignature = "skip_thought_signature_validator";
          }
        }
      }
    }
  }
  return payload;
}

function buildFallbackPayloadFromBody(body, clientProtocol, promptSnippet) {
  if (
    transformMod &&
    typeof transformMod.normalizeToCanonicalRequest === "function" &&
    typeof transformMod.buildVertexGeminiPayload === "function"
  ) {
    if (clientProtocol === "vertex_gemini" && body && Array.isArray(body.contents)) {
      return ensureGemini3FunctionCallSignatures(body);
    }
    var canonical = transformMod.normalizeToCanonicalRequest(body, clientProtocol);
    return ensureGemini3FunctionCallSignatures(
      transformMod.buildVertexGeminiPayload(canonical)
    );
  }

  // Self-contained translation for Apigee Rhino runtime
  if (body && Array.isArray(body.contents)) {
    return ensureGemini3FunctionCallSignatures(body);
  }

  var payload = {
    contents: [],
    generationConfig: {
      maxOutputTokens: Math.min(
        Math.max(body.max_tokens || body.max_output_tokens || 4096, 1024),
        65536
      ),
      temperature: typeof body.temperature === "number" ? body.temperature : 1.0
    }
  };

  var sysText = "";
  if (typeof body.system === "string") {
    sysText = body.system;
  } else if (Array.isArray(body.system)) {
    var sp = [];
    for (var s = 0; s < body.system.length; s++) {
      if (body.system[s] && body.system[s].text) sp.push(body.system[s].text);
    }
    sysText = sp.join("\n");
  } else if (typeof body.instructions === "string") {
    sysText = body.instructions;
  }

  var toolIdToName = {};
  var rawMsgs = body.messages || (Array.isArray(body.input) ? body.input : []);
  for (var i = 0; i < rawMsgs.length; i++) {
    var m = rawMsgs[i];
    if (!m) continue;
    if (m.role === "system" || m.role === "developer") {
      if (typeof m.content === "string") {
        sysText += (sysText ? "\n" : "") + m.content;
      }
      continue;
    }
    var parts = [];
    var seenFc = false;
    if (typeof m.content === "string" && m.content) {
      parts.push({ text: m.content });
    } else if (Array.isArray(m.content)) {
      for (var j = 0; j < m.content.length; j++) {
        var b = m.content[j];
        if (!b) continue;
        if (
          (b.type === "text" ||
            b.type === "input_text" ||
            b.type === "output_text") &&
          b.text
        ) {
          parts.push({ text: b.text });
        } else if (b.type === "tool_use") {
          toolIdToName[b.id] = b.name;
          var fcObj = {
            functionCall: { name: b.name, args: b.input || {} }
          };
          if (!seenFc) {
            seenFc = true;
            fcObj.thoughtSignature =
              b.thoughtSignature || "skip_thought_signature_validator";
          }
          parts.push(fcObj);
        } else if (b.type === "tool_result") {
          parts.push({
            functionResponse: {
              name: b.name || toolIdToName[b.tool_use_id] || b.tool_use_id || "tool",
              response: {
                result:
                  typeof b.content === "string"
                    ? b.content
                    : JSON.stringify(b.content || {})
              }
            }
          });
        }
      }
    }
    if (Array.isArray(m.tool_calls)) {
      for (var tc = 0; tc < m.tool_calls.length; tc++) {
        var tCall = m.tool_calls[tc];
        var fn = (tCall && tCall.function) || {};
        toolIdToName[tCall.id] = fn.name || "tool";
        var parsedArgs = {};
        try {
          parsedArgs =
            typeof fn.arguments === "string"
              ? JSON.parse(fn.arguments)
              : fn.arguments || {};
        } catch (e) {
          parsedArgs = {};
        }
        var oaiFc = {
          functionCall: { name: fn.name || "tool", args: parsedArgs }
        };
        if (!seenFc) {
          seenFc = true;
          oaiFc.thoughtSignature =
            tCall.thoughtSignature || "skip_thought_signature_validator";
        }
        parts.push(oaiFc);
      }
    }
    if (m.role === "tool" || m.type === "function_call_output") {
      var refId = m.tool_call_id || m.call_id || "tool";
      parts.push({
        functionResponse: {
          name: m.name || toolIdToName[refId] || refId,
          response: { result: m.content || m.output || "" }
        }
      });
    } else if (m.type === "function_call") {
      toolIdToName[m.call_id || m.id] = m.name;
      var fArgs = {};
      try {
        fArgs =
          typeof m.arguments === "string"
            ? JSON.parse(m.arguments)
            : m.arguments || {};
      } catch (e) {
        fArgs = {};
      }
      var respFc = {
        functionCall: { name: m.name || "tool", args: fArgs },
        thoughtSignature:
          m.thoughtSignature || "skip_thought_signature_validator"
      };
      parts.push(respFc);
    }
    if (parts.length > 0) {
      var targetRole =
        m.role === "assistant" || m.type === "function_call" ? "model" : "user";
      if (
        payload.contents.length > 0 &&
        payload.contents[payload.contents.length - 1].role === targetRole
      ) {
        var prevParts = payload.contents[payload.contents.length - 1].parts;
        for (var pp = 0; pp < parts.length; pp++) {
          prevParts.push(parts[pp]);
        }
      } else {
        payload.contents.push({
          role: targetRole,
          parts: parts
        });
      }
    }
  }

  if (sysText) {
    payload.systemInstruction = { parts: [{ text: sysText }] };
  }
  if (payload.contents.length === 0) {
    payload.contents.push({
      role: "user",
      parts: [{ text: promptSnippet || "Continue coding task" }]
    });
  }
  if (Array.isArray(body.tools) && body.tools.length > 0) {
    var decls = [];
    for (var t = 0; t < body.tools.length; t++) {
      var rawT = body.tools[t].function || body.tools[t];
      if (rawT && rawT.name) {
        decls.push({
          name: rawT.name,
          description: rawT.description || "",
          parametersJsonSchema: normalizeFallbackSchema(
            rawT.input_schema || rawT.parameters || { type: "object", properties: {} }
          )
        });
      }
    }
    if (decls.length > 0) {
      payload.tools = [{ functionDeclarations: decls }];
    }
  }
  return ensureGemini3FunctionCallSignatures(payload);
}

function runPrepareVertexFallbackPayload(context) {
  var origRaw =
    context.getVariable("original_client_request_body") ||
    context.getVariable("request.content") ||
    "{}";
  var clientProtocol =
    context.getVariable("client_protocol") || "anthropic_messages";
  var promptSnippet =
    context.getVariable("prompt_text_for_armor") || "Continue coding task";

  var parsed = {};
  try {
    parsed = JSON.parse(origRaw);
  } catch (e) {
    parsed = {};
  }

  var fallbackGeminiPayload = buildFallbackPayloadFromBody(
    parsed,
    clientProtocol,
    promptSnippet
  );
  var primaryFailed =
    context.getVariable("target_model_id") ||
    context.getVariable("ai.selectedModel") ||
    "unknown";

  context.setVariable("fallback_triggered", "true");
  context.setVariable("ai.fallbackTriggered", "true");
  context.setVariable("ai.fallbackUsed", "true");
  context.setVariable("primary_failed_model", primaryFailed);
  context.setVariable("ai.primaryFailedModel", primaryFailed);

  // Gate C8: Select fallback model within caller's allowed RBAC tiers
  var allowedTiers =
    context.getVariable("allowed_model_tiers") ||
    context.getVariable("auth.allowed_models") ||
    "tier1-fast,tier2-daily,tier4-open";
  var allowTier2 =
    allowedTiers.indexOf("tier2-daily") !== -1 ||
    allowedTiers.indexOf("gemini-3.1-pro-preview") !== -1 ||
    allowedTiers.indexOf("tier3-frontier") !== -1;
  var fbTargetId = allowTier2 ? "gemini-3.1-pro-preview" : "gemini-3.8-flash";
  var fbVertexId = allowTier2 ? "gemini-3.1-pro-preview" : "gemini-3-flash-preview";
  var fbInCost = allowTier2 ? "2.00" : "0.15";
  var fbOutCost = allowTier2 ? "12.00" : "0.60";

  context.setVariable("target_model_id", fbTargetId);
  context.setVariable("ai.selectedModel", fbTargetId);
  context.setVariable("vertex_model_id", fbVertexId);
  context.setVariable("ai.fallbackVertexModel", fbVertexId);
  context.setVariable("target_model_family", "gemini");
  context.setVariable("ai.targetFamily", "gemini");
  context.setVariable("vertex_region", "global");
  context.setVariable("ai.vertexRegion", "global");
  context.setVariable("vertex_host", "aiplatform.googleapis.com");
  context.setVariable("ai.vertexHost", "aiplatform.googleapis.com");
  context.setVariable("model_input_cost_1m", fbInCost);
  context.setVariable("model_output_cost_1m", fbOutCost);
  context.setVariable(
    "vertex_fallback_payload",
    JSON.stringify(fallbackGeminiPayload)
  );
}

if (typeof context !== "undefined") {
  runPrepareVertexFallbackPayload(context);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    buildFallbackPayloadFromBody: buildFallbackPayloadFromBody,
    runPrepareVertexFallbackPayload: runPrepareVertexFallbackPayload
  };
}
