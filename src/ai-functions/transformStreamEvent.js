/**
 * transformStreamEvent.js
 * Executed inside Apigee EventFlow (or streaming proxy pipeline) on each Server-Sent Event (SSE) chunk:
 *   1. Extracts incremental token usage from the SSE data payload into `sse_event_token_count`
 *      and `ai.streamingTokenCount` for native <LLMTokenQuota> enforcement.
 *   2. Translates the SSE chunk into the calling harness's complete SSE wire state machine when
 *      cross-family routing occurs:
 *        - Claude Code (`anthropic_messages`): message_start -> content_block_start ->
 *          content_block_delta (text_delta / input_json_delta) -> content_block_stop ->
 *          message_delta -> message_stop
 *        - Codex `/v1/responses` (`openai_responses`): response.created ->
 *          response.output_item.added -> response.output_text.delta /
 *          response.function_call_arguments.delta -> response.output_item.done ->
 *          response.completed
 *        - OpenAI Chat `/v1/chat/completions` (`openai_chat`): chat.completion.chunk deltas
 *        - Antigravity (`vertex_gemini`): candidates[0].content.parts without premature finishReason
 */

var GLOBAL_THOUGHT_SIGNATURES =
  (typeof globalThis !== "undefined" && globalThis.__AGY_THOUGHT_SIGNATURES__) || {};
var GLOBAL_TOOL_ID_TO_NAME =
  (typeof globalThis !== "undefined" && globalThis.__AGY_TOOL_ID_TO_NAME__) || {};
if (typeof globalThis !== "undefined") {
  globalThis.__AGY_THOUGHT_SIGNATURES__ = GLOBAL_THOUGHT_SIGNATURES;
  globalThis.__AGY_TOOL_ID_TO_NAME__ = GLOBAL_TOOL_ID_TO_NAME;
}

var B64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
var MAX_REGISTRY_ENTRIES = 4000;

function utf8ToBytes(str) {
  var bytes = [];
  var s = String(str || "");
  for (var i = 0; i < s.length; i++) {
    var code = s.charCodeAt(i);
    if (code < 0x80) {
      bytes.push(code);
    } else if (code < 0x800) {
      bytes.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
    } else if (code >= 0xd800 && code <= 0xdbff && i + 1 < s.length) {
      var next = s.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        var cp = ((code - 0xd800) << 10) + (next - 0xdc00) + 0x10000;
        bytes.push(
          0xf0 | (cp >> 18),
          0x80 | ((cp >> 12) & 0x3f),
          0x80 | ((cp >> 6) & 0x3f),
          0x80 | (cp & 0x3f)
        );
        i++;
      } else {
        bytes.push(0xef, 0xbf, 0xbd);
      }
    } else {
      bytes.push(
        0xe0 | (code >> 12),
        0x80 | ((code >> 6) & 0x3f),
        0x80 | (code & 0x3f)
      );
    }
  }
  return bytes;
}

function encodeBase64Url(str) {
  if (!str) return "";
  var bytes = utf8ToBytes(str);
  var out = [];
  for (var i = 0; i < bytes.length; i += 3) {
    var b0 = bytes[i];
    var hasB1 = i + 1 < bytes.length;
    var hasB2 = i + 2 < bytes.length;
    var b1 = hasB1 ? bytes[i + 1] : 0;
    var b2 = hasB2 ? bytes[i + 2] : 0;
    out.push(B64_CHARS.charAt(b0 >> 2));
    out.push(B64_CHARS.charAt(((b0 & 3) << 4) | (b1 >> 4)));
    if (hasB1) {
      out.push(B64_CHARS.charAt(((b1 & 15) << 2) | (b2 >> 6)));
    }
    if (hasB2) {
      out.push(B64_CHARS.charAt(b2 & 63));
    }
  }
  return out.join("");
}

function trimMapSize(obj) {
  var keys = Object.keys(obj);
  if (keys.length > MAX_REGISTRY_ENTRIES) {
    var removeCount = keys.length - MAX_REGISTRY_ENTRIES + 500;
    for (var i = 0; i < removeCount && i < keys.length; i++) {
      delete obj[keys[i]];
    }
  }
}

function recordStreamToolMeta(callId, fnName, sig) {
  if (callId && fnName) {
    GLOBAL_TOOL_ID_TO_NAME[callId] = fnName;
    trimMapSize(GLOBAL_TOOL_ID_TO_NAME);
  }
  if (callId && sig) {
    GLOBAL_THOUGHT_SIGNATURES[callId] = sig;
    trimMapSize(GLOBAL_THOUGHT_SIGNATURES);
  }
}

function formatSseEvent(eventType, dataObj) {
  return "event: " + eventType + "\ndata: " + JSON.stringify(dataObj) + "\n\n";
}

function flushPendingToolsMap(pendingMap, toolCalls, specificIndex) {
  if (!pendingMap) return;
  var keys =
    typeof specificIndex === "number" && pendingMap[specificIndex]
      ? [String(specificIndex)]
      : Object.keys(pendingMap);
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i];
    var entry = pendingMap[k];
    if (!entry) continue;
    var parsedArgs = entry.initialInput || {};
    if (typeof entry.argsJson === "string" && entry.argsJson.trim().length > 0) {
      try {
        parsedArgs = JSON.parse(entry.argsJson);
      } catch (e) {
        parsedArgs = { raw: entry.argsJson };
      }
    }
    var rawId = entry.id || "toolu_stream_" + (i + 1);
    var entrySig = entry.thoughtSignature || null;
    var id =
      entrySig && rawId.indexOf("__sig_") === -1
        ? rawId + "__sig_" + encodeBase64Url(entrySig)
        : rawId;
    var name = entry.name || "tool";
    recordStreamToolMeta(id, name, entrySig);
    toolCalls.push({
      id: id,
      name: name,
      args: parsedArgs,
      thoughtSignature: entrySig
    });
    delete pendingMap[k];
  }
}

function transformSSEChunk(rawEvent, clientProtocol, targetFamily, modelId, streamState) {
  var state = streamState || {};
  state.started = Boolean(state.started);
  state.textBlockOpen = Boolean(state.textBlockOpen);
  state.completed = Boolean(state.completed);
  state.blockIndex = typeof state.blockIndex === "number" ? state.blockIndex : 0;
  state.sawToolCall = Boolean(state.sawToolCall);
  state.pendingAnthropicTools = state.pendingAnthropicTools || {};
  state.pendingMaaSTools = state.pendingMaaSTools || {};
  state.accumulatedText = state.accumulatedText || "";
  state.outputItems = Array.isArray(state.outputItems) ? state.outputItems : [];
  state.nextOutputIndex =
    typeof state.nextOutputIndex === "number" ? state.nextOutputIndex : 0;
  state.textItemIndex =
    typeof state.textItemIndex === "number" ? state.textItemIndex : 0;
  state.inputTokens = typeof state.inputTokens === "number" ? state.inputTokens : 0;
  state.outputTokens = typeof state.outputTokens === "number" ? state.outputTokens : 0;
  state.cachedInputTokens =
    typeof state.cachedInputTokens === "number" ? state.cachedInputTokens : 0;
  state.cacheCreationTokens =
    typeof state.cacheCreationTokens === "number" ? state.cacheCreationTokens : 0;
  state.thoughtsTokens =
    typeof state.thoughtsTokens === "number" ? state.thoughtsTokens : 0;
  state.requestStartMs =
    typeof state.requestStartMs === "number" ? state.requestStartMs : 0;
  state.ttftMs =
    typeof state.ttftMs === "number" ? state.ttftMs : null;
  state.toolCallNames = Array.isArray(state.toolCallNames)
    ? state.toolCallNames
    : [];

  var result = {
    transformedEvent: rawEvent,
    eventTokenCount: 0,
    streamState: state
  };
  if (!rawEvent) return result;

  // Native passthrough if protocols match
  var isNativeMatch =
    (clientProtocol === "vertex_gemini" && targetFamily === "gemini") ||
    (clientProtocol === "anthropic_messages" && targetFamily === "anthropic") ||
    (clientProtocol === "openai_chat" && targetFamily === "openapi_maas");

  var lines = rawEvent.split(/\r?\n/);
  var dataJsonStr = "";
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim();
    if (line.indexOf("data:") === 0) {
      dataJsonStr = line.substring(5).trim();
      break;
    }
  }

  if (!dataJsonStr) {
    return result;
  }

  var toolCalls = []; // [{ id, name, args, thoughtSignature }]

  if (dataJsonStr === "[DONE]") {
    flushPendingToolsMap(state.pendingAnthropicTools, toolCalls);
    flushPendingToolsMap(state.pendingMaaSTools, toolCalls);
    if (toolCalls.length > 0) {
      state.sawToolCall = true;
    }
    if (isNativeMatch) {
      state.completed = true;
      return result;
    }
    var doneOut = "";
    if (clientProtocol === "anthropic_messages" && !state.completed) {
      if (!state.started) {
        state.started = true;
        doneOut += formatSseEvent("message_start", {
          type: "message_start",
          message: {
            id: "msg_stream_" + Date.now(),
            type: "message",
            role: "assistant",
            model: modelId,
            content: [],
            stop_reason: null,
            stop_sequence: null,
            usage: { input_tokens: state.inputTokens, output_tokens: 0 }
          }
        });
      }
      if (state.textBlockOpen) {
        doneOut += formatSseEvent("content_block_stop", {
          type: "content_block_stop",
          index: state.blockIndex
        });
        state.textBlockOpen = false;
        state.blockIndex += 1;
      }
      for (var dTc = 0; dTc < toolCalls.length; dTc++) {
        var dt = toolCalls[dTc];
        doneOut += formatSseEvent("content_block_start", {
          type: "content_block_start",
          index: state.blockIndex,
          content_block: { type: "tool_use", id: dt.id, name: dt.name, input: {} }
        });
        doneOut += formatSseEvent("content_block_delta", {
          type: "content_block_delta",
          index: state.blockIndex,
          delta: {
            type: "input_json_delta",
            partial_json: JSON.stringify(dt.args || {})
          }
        });
        doneOut += formatSseEvent("content_block_stop", {
          type: "content_block_stop",
          index: state.blockIndex
        });
        state.blockIndex += 1;
      }
      state.completed = true;
      doneOut += formatSseEvent("message_delta", {
        type: "message_delta",
        delta: {
          stop_reason: state.sawToolCall ? "tool_use" : "end_turn",
          stop_sequence: null
        },
        usage: {
          input_tokens: state.inputTokens,
          output_tokens: state.outputTokens,
          cache_read_input_tokens: state.cachedInputTokens || 0
        }
      });
      doneOut += formatSseEvent("message_stop", { type: "message_stop" });
      result.transformedEvent = doneOut;
    } else if (clientProtocol === "openai_responses" && !state.completed) {
      if (state.textBlockOpen) {
        if (state.autoRouteBadge && !state.sawToolCall && !state.badgeAppended) {
          var doneBadgeDelta = "\n\n" + state.autoRouteBadge;
          state.accumulatedText += doneBadgeDelta;
          state.badgeAppended = true;
          doneOut += formatSseEvent("response.output_text.delta", {
            type: "response.output_text.delta",
            output_index: state.textItemIndex,
            content_index: 0,
            delta: doneBadgeDelta
          });
        }
        var doneMsgItem = {
          id: "msg_item_" + state.textItemIndex,
          type: "message",
          status: "completed",
          role: "assistant",
          content: [
            {
              type: "output_text",
              text: state.accumulatedText,
              annotations: []
            }
          ]
        };
        doneOut += formatSseEvent("response.output_item.done", {
          type: "response.output_item.done",
          output_index: state.textItemIndex,
          item: doneMsgItem
        });
        state.outputItems.push(doneMsgItem);
        state.textBlockOpen = false;
      }
      for (var drtc = 0; drtc < toolCalls.length; drtc++) {
        var drTool = toolCalls[drtc];
        var dArgsJson = JSON.stringify(drTool.args || {});
        var dItemIdx = state.nextOutputIndex++;
        var dFcItem = {
          id: "fc_" + (dItemIdx + 1),
          type: "function_call",
          call_id: drTool.id,
          name: drTool.name,
          arguments: dArgsJson,
          status: "completed"
        };
        doneOut += formatSseEvent("response.output_item.added", {
          type: "response.output_item.added",
          output_index: dItemIdx,
          item: {
            id: dFcItem.id,
            type: "function_call",
            call_id: drTool.id,
            name: drTool.name,
            arguments: "",
            status: "in_progress"
          }
        });
        doneOut += formatSseEvent("response.function_call_arguments.delta", {
          type: "response.function_call_arguments.delta",
          output_index: dItemIdx,
          call_id: drTool.id,
          delta: dArgsJson
        });
        doneOut += formatSseEvent("response.function_call_arguments.done", {
          type: "response.function_call_arguments.done",
          output_index: dItemIdx,
          call_id: drTool.id,
          arguments: dArgsJson
        });
        doneOut += formatSseEvent("response.output_item.done", {
          type: "response.output_item.done",
          output_index: dItemIdx,
          item: dFcItem
        });
        state.outputItems.push(dFcItem);
      }
      state.completed = true;
      doneOut += formatSseEvent("response.completed", {
        type: "response.completed",
        response: {
          id: "resp_stream_" + Date.now(),
          object: "response",
          status: "completed",
          model: modelId,
          output: state.outputItems,
          usage: {
            input_tokens: state.inputTokens,
            output_tokens: state.outputTokens,
            total_tokens: state.inputTokens + state.outputTokens
          }
        }
      });
      result.transformedEvent = doneOut;
    } else if (clientProtocol === "openai_chat") {
      state.completed = true;
      result.transformedEvent = "data: [DONE]\n\n";
    } else {
      state.completed = true;
      result.transformedEvent = "";
    }
    return result;
  }

  var parsed = null;
  try {
    parsed = JSON.parse(dataJsonStr);
  } catch (e) {
    return result;
  }

  var textDelta = "";
  var isFinalChunk = false;
  var finishReasonNorm = null; // "end_turn" | "tool_use" | "max_tokens"

  if (targetFamily === "gemini") {
    if (parsed.usageMetadata) {
      if (parsed.usageMetadata.promptTokenCount) {
        state.inputTokens = Number(parsed.usageMetadata.promptTokenCount);
      }
      if (parsed.usageMetadata.cachedContentTokenCount) {
        state.cachedInputTokens = Number(parsed.usageMetadata.cachedContentTokenCount);
      }
      var candToks = Number(parsed.usageMetadata.candidatesTokenCount || 0);
      var thoughtToks = Number(parsed.usageMetadata.thoughtsTokenCount || 0);
      if (thoughtToks > 0) {
        state.thoughtsTokens = thoughtToks;
      }
      if (candToks + thoughtToks > 0) {
        state.outputTokens = candToks + thoughtToks;
      }
    }
    var cand = parsed.candidates && parsed.candidates[0];
    if (cand) {
      var parts = (cand.content && cand.content.parts) || [];
      var latestSig = null;
      for (var pIdx = 0; pIdx < parts.length; pIdx++) {
        var pt = parts[pIdx];
        if (!pt) continue;
        if (pt.thoughtSignature) latestSig = pt.thoughtSignature;
        if (pt.text && !pt.thought) {
          textDelta += pt.text;
        }
        if (pt.functionCall) {
          var tcSig = pt.thoughtSignature || null;
          if (!tcSig && latestSig && toolCalls.length === 0) {
            tcSig = latestSig;
            latestSig = null;
          }
          var baseTcId =
            pt.functionCall.id ||
            "toolu_vrtx_stream_" + Date.now() + "_" + (pIdx + 1);
          var tcId = tcSig
            ? baseTcId + "__sig_" + encodeBase64Url(tcSig)
            : baseTcId;
          var tcName = pt.functionCall.name || "tool";
          recordStreamToolMeta(tcId, tcName, tcSig);
          toolCalls.push({
            id: tcId,
            name: tcName,
            args: pt.functionCall.args || {},
            thoughtSignature: tcSig
          });
        }
      }
      if (cand.finishReason) {
        isFinalChunk = true;
        result.eventTokenCount = state.inputTokens + state.outputTokens;
        if (toolCalls.length > 0 || state.sawToolCall) {
          finishReasonNorm = "tool_use";
        } else if (cand.finishReason === "MAX_TOKENS") {
          finishReasonNorm = "max_tokens";
        } else {
          finishReasonNorm = "end_turn";
        }
      }
    }
  } else if (targetFamily === "anthropic") {
    if (parsed.message && parsed.message.usage) {
      var mUncached = Number(parsed.message.usage.input_tokens || 0);
      if (parsed.message.usage.cache_read_input_tokens !== undefined) {
        state.cachedInputTokens = Number(parsed.message.usage.cache_read_input_tokens || 0);
      }
      if (parsed.message.usage.cache_creation_input_tokens !== undefined) {
        state.cacheCreationTokens = Number(parsed.message.usage.cache_creation_input_tokens || 0);
      }
      if (mUncached > 0 || state.cachedInputTokens > 0 || state.cacheCreationTokens > 0) {
        state.inputTokens = mUncached + state.cachedInputTokens + state.cacheCreationTokens;
      }
      if (parsed.message.usage.output_tokens) {
        state.outputTokens = Number(parsed.message.usage.output_tokens);
      }
    }
    if (parsed.usage) {
      var prevUncached = Math.max(
        0,
        state.inputTokens - state.cachedInputTokens - state.cacheCreationTokens
      );
      var dUncached =
        parsed.usage.input_tokens !== undefined
          ? Number(parsed.usage.input_tokens || 0)
          : prevUncached;
      if (parsed.usage.cache_read_input_tokens !== undefined) {
        state.cachedInputTokens = Number(parsed.usage.cache_read_input_tokens || 0);
      }
      if (parsed.usage.cache_creation_input_tokens !== undefined) {
        state.cacheCreationTokens = Number(parsed.usage.cache_creation_input_tokens || 0);
      }
      if (dUncached > 0 || state.cachedInputTokens > 0 || state.cacheCreationTokens > 0) {
        state.inputTokens = dUncached + state.cachedInputTokens + state.cacheCreationTokens;
      }
      if (parsed.usage.output_tokens) {
        state.outputTokens = Number(parsed.usage.output_tokens);
      }
    }
    if (parsed.type === "content_block_delta" && parsed.delta) {
      if (parsed.delta.type === "text_delta" && parsed.delta.text) {
        textDelta = parsed.delta.text;
      } else if (parsed.delta.type === "input_json_delta") {
        var dIdx = typeof parsed.index === "number" ? parsed.index : 0;
        var pendingEntry = state.pendingAnthropicTools[dIdx];
        if (!pendingEntry) {
          var pKeys = Object.keys(state.pendingAnthropicTools);
          if (pKeys.length > 0) {
            pendingEntry = state.pendingAnthropicTools[pKeys[pKeys.length - 1]];
          } else {
            pendingEntry = {
              id: "toolu_anth_stream_" + (dIdx + 1),
              name: "tool",
              initialInput: {},
              argsJson: ""
            };
            state.pendingAnthropicTools[dIdx] = pendingEntry;
          }
        }
        pendingEntry.argsJson += parsed.delta.partial_json || "";
      }
    } else if (
      parsed.type === "content_block_start" &&
      parsed.content_block &&
      parsed.content_block.type === "tool_use"
    ) {
      var bIdx = typeof parsed.index === "number" ? parsed.index : 0;
      state.pendingAnthropicTools[bIdx] = {
        id: parsed.content_block.id || "toolu_anth_stream_" + (bIdx + 1),
        name: parsed.content_block.name || "tool",
        initialInput: parsed.content_block.input || {},
        argsJson: ""
      };
      state.sawToolCall = true;
    } else if (parsed.type === "content_block_stop") {
      var sIdx = typeof parsed.index === "number" ? parsed.index : undefined;
      flushPendingToolsMap(state.pendingAnthropicTools, toolCalls, sIdx);
    } else if (parsed.type === "message_delta" && parsed.delta && parsed.delta.stop_reason) {
      flushPendingToolsMap(state.pendingAnthropicTools, toolCalls);
      isFinalChunk = true;
      finishReasonNorm = parsed.delta.stop_reason;
      result.eventTokenCount = state.inputTokens + state.outputTokens;
    } else if (parsed.type === "message_stop") {
      flushPendingToolsMap(state.pendingAnthropicTools, toolCalls);
      isFinalChunk = true;
      finishReasonNorm = state.sawToolCall ? "tool_use" : "end_turn";
    }
  } else {
    // openapi_maas
    if (parsed.usage) {
      if (parsed.usage.prompt_tokens) {
        state.inputTokens = Number(parsed.usage.prompt_tokens);
      }
      if (parsed.usage.completion_tokens) {
        state.outputTokens = Number(parsed.usage.completion_tokens);
      }
      if (parsed.usage.prompt_tokens_details && parsed.usage.prompt_tokens_details.cached_tokens) {
        state.cachedInputTokens = Number(parsed.usage.prompt_tokens_details.cached_tokens);
      }
      if (parsed.usage.completion_tokens_details && parsed.usage.completion_tokens_details.reasoning_tokens) {
        state.thoughtsTokens = Number(parsed.usage.completion_tokens_details.reasoning_tokens);
      }
      if (parsed.usage.total_tokens) {
        result.eventTokenCount = Number(parsed.usage.total_tokens);
      }
    }
    var ch = parsed.choices && parsed.choices[0];
    if (ch) {
      if (ch.delta) {
        if (ch.delta.content) {
          textDelta = ch.delta.content;
        }
        if (Array.isArray(ch.delta.tool_calls)) {
          for (var dtc = 0; dtc < ch.delta.tool_calls.length; dtc++) {
            var rawDtc = ch.delta.tool_calls[dtc];
            if (!rawDtc) continue;
            var tIdx = typeof rawDtc.index === "number" ? rawDtc.index : dtc;
            if (!state.pendingMaaSTools[tIdx]) {
              state.pendingMaaSTools[tIdx] = {
                id: rawDtc.id || "call_maas_stream_" + (tIdx + 1),
                name: "",
                initialInput: {},
                argsJson: "",
                thoughtSignature: null
              };
            }
            var mEntry = state.pendingMaaSTools[tIdx];
            if (rawDtc.id) mEntry.id = rawDtc.id;
            var dSig =
              (rawDtc.extra_content &&
                rawDtc.extra_content.google &&
                rawDtc.extra_content.google.thought_signature) ||
              rawDtc.thoughtSignature ||
              rawDtc.thought_signature ||
              null;
            if (dSig) mEntry.thoughtSignature = dSig;
            var dFn = rawDtc.function || {};
            if (dFn.name) mEntry.name = dFn.name;
            if (typeof dFn.arguments === "string") {
              mEntry.argsJson += dFn.arguments;
            }
            state.sawToolCall = true;
          }
        }
      }
      if (ch.finish_reason) {
        flushPendingToolsMap(state.pendingMaaSTools, toolCalls);
        isFinalChunk = true;
        if (!result.eventTokenCount) {
          result.eventTokenCount = state.inputTokens + state.outputTokens;
        }
        if (
          ch.finish_reason === "tool_calls" ||
          toolCalls.length > 0 ||
          state.sawToolCall
        ) {
          finishReasonNorm = "tool_use";
        } else if (ch.finish_reason === "length") {
          finishReasonNorm = "max_tokens";
        } else {
          finishReasonNorm = "end_turn";
        }
      }
    }
  }

  if (textDelta) {
    if (
      state.suppressTrailingBadge ||
      textDelta.indexOf("Vertex Smart Router") !== -1 ||
      (state.accumulatedText + textDelta).indexOf("Vertex Smart Router") !== -1
    ) {
      state.suppressTrailingBadge = true;
      textDelta = textDelta.replace(
        /\n*(?:---\s*\n+)?⚡?\s*\*{0,2}Vertex Smart Router[\s\S]*$/g,
        ""
      );
      if (state.accumulatedText.indexOf("Vertex Smart Router") !== -1 || /⚡\s*\*{0,2}$/.test(state.accumulatedText)) {
        state.accumulatedText = state.accumulatedText.replace(
          /\n*(?:---\s*\n+)?⚡\s*\*{0,2}(?:Vertex Smart Router[\s\S]*)?$/g,
          ""
        );
        textDelta = "";
      }
    }
    state.accumulatedText += textDelta;
  }

  if (toolCalls.length > 0) {
    for (var tcn = 0; tcn < toolCalls.length && state.toolCallNames.length < 10; tcn++) {
      if (
        toolCalls[tcn] &&
        toolCalls[tcn].name &&
        state.toolCallNames.indexOf(toolCalls[tcn].name) === -1
      ) {
        state.toolCallNames.push(String(toolCalls[tcn].name));
      }
    }
  }

  if (
    state.ttftMs === null &&
    state.requestStartMs > 0 &&
    (Boolean(textDelta) || toolCalls.length > 0 || state.sawToolCall)
  ) {
    state.ttftMs = Math.max(1, Date.now() - state.requestStartMs);
  }

  if (isNativeMatch) {
    if (isFinalChunk) {
      state.completed = true;
    }
    return result;
  }

  if (toolCalls.length > 0) {
    state.sawToolCall = true;
  }

  var outEvents = "";

  if (clientProtocol === "anthropic_messages") {
    var anthStreamUncachedIn = Math.max(
      0,
      state.inputTokens - (state.cachedInputTokens || 0) - (state.cacheCreationTokens || 0)
    );
    if (!state.started && (textDelta || toolCalls.length > 0 || isFinalChunk)) {
      state.started = true;
      outEvents += formatSseEvent("message_start", {
        type: "message_start",
        message: {
          id: "msg_stream_" + Date.now(),
          type: "message",
          role: "assistant",
          model: modelId,
          content: [],
          stop_reason: null,
          stop_sequence: null,
          usage: {
            input_tokens: anthStreamUncachedIn,
            output_tokens: 0,
            cache_read_input_tokens: state.cachedInputTokens || 0,
            cache_creation_input_tokens: state.cacheCreationTokens || 0
          }
        }
      });
    }
    if (textDelta) {
      if (!state.textBlockOpen) {
        state.textBlockOpen = true;
        outEvents += formatSseEvent("content_block_start", {
          type: "content_block_start",
          index: state.blockIndex,
          content_block: { type: "text", text: "" }
        });
      }
      outEvents += formatSseEvent("content_block_delta", {
        type: "content_block_delta",
        index: state.blockIndex,
        delta: { type: "text_delta", text: textDelta }
      });
    }
    for (var tcIdx = 0; tcIdx < toolCalls.length; tcIdx++) {
      if (state.textBlockOpen) {
        outEvents += formatSseEvent("content_block_stop", {
          type: "content_block_stop",
          index: state.blockIndex
        });
        state.textBlockOpen = false;
        state.blockIndex += 1;
      }
      var tc = toolCalls[tcIdx];
      var startBlock = {
        type: "tool_use",
        id: tc.id,
        name: tc.name,
        input: {}
      };
      if (tc.thoughtSignature) {
        startBlock.thoughtSignature = tc.thoughtSignature;
      }
      outEvents += formatSseEvent("content_block_start", {
        type: "content_block_start",
        index: state.blockIndex,
        content_block: startBlock
      });
      outEvents += formatSseEvent("content_block_delta", {
        type: "content_block_delta",
        index: state.blockIndex,
        delta: {
          type: "input_json_delta",
          partial_json: JSON.stringify(tc.args || {})
        }
      });
      outEvents += formatSseEvent("content_block_stop", {
        type: "content_block_stop",
        index: state.blockIndex
      });
      state.blockIndex += 1;
    }
    if (isFinalChunk && !state.completed) {
      state.completed = true;
      if (state.textBlockOpen) {
        outEvents += formatSseEvent("content_block_stop", {
          type: "content_block_stop",
          index: state.blockIndex
        });
        state.textBlockOpen = false;
      }
      outEvents += formatSseEvent("message_delta", {
        type: "message_delta",
        delta: {
          stop_reason:
            finishReasonNorm || (state.sawToolCall ? "tool_use" : "end_turn"),
          stop_sequence: null
        },
        usage: {
          input_tokens: anthStreamUncachedIn,
          output_tokens: state.outputTokens || result.eventTokenCount,
          cache_read_input_tokens: state.cachedInputTokens || 0,
          cache_creation_input_tokens: state.cacheCreationTokens || 0
        }
      });
      outEvents += formatSseEvent("message_stop", { type: "message_stop" });
    }
    result.transformedEvent = outEvents;
  } else if (clientProtocol === "openai_responses") {
    if (!state.started && (textDelta || toolCalls.length > 0 || isFinalChunk)) {
      state.started = true;
      outEvents += formatSseEvent("response.created", {
        type: "response.created",
        response: {
          id: "resp_stream_" + Date.now(),
          object: "response",
          status: "in_progress",
          model: modelId,
          output: []
        }
      });
    }
    if (textDelta) {
      if (!state.textBlockOpen) {
        state.textBlockOpen = true;
        state.textItemIndex = state.nextOutputIndex++;
        outEvents += formatSseEvent("response.output_item.added", {
          type: "response.output_item.added",
          output_index: state.textItemIndex,
          item: {
            id: "msg_item_" + state.textItemIndex,
            type: "message",
            status: "in_progress",
            role: "assistant",
            content: []
          }
        });
      }
      outEvents += formatSseEvent("response.output_text.delta", {
        type: "response.output_text.delta",
        output_index: state.textItemIndex,
        content_index: 0,
        delta: textDelta
      });
    }
    for (var rtc = 0; rtc < toolCalls.length; rtc++) {
      if (state.textBlockOpen) {
        var midMsgItem = {
          id: "msg_item_" + state.textItemIndex,
          type: "message",
          status: "completed",
          role: "assistant",
          content: [
            {
              type: "output_text",
              text: state.accumulatedText,
              annotations: []
            }
          ]
        };
        outEvents += formatSseEvent("response.output_item.done", {
          type: "response.output_item.done",
          output_index: state.textItemIndex,
          item: midMsgItem
        });
        state.outputItems.push(midMsgItem);
        state.textBlockOpen = false;
      }
      var rTool = toolCalls[rtc];
      var argsJson = JSON.stringify(rTool.args || {});
      var itemIdx = state.nextOutputIndex++;
      var fcDoneItem = {
        id: "fc_" + (itemIdx + 1),
        type: "function_call",
        call_id: rTool.id,
        name: rTool.name,
        arguments: argsJson,
        status: "completed"
      };
      if (rTool.thoughtSignature) {
        fcDoneItem.thoughtSignature = rTool.thoughtSignature;
      }
      outEvents += formatSseEvent("response.output_item.added", {
        type: "response.output_item.added",
        output_index: itemIdx,
        item: {
          id: fcDoneItem.id,
          type: "function_call",
          call_id: rTool.id,
          name: rTool.name,
          arguments: "",
          status: "in_progress"
        }
      });
      outEvents += formatSseEvent("response.function_call_arguments.delta", {
        type: "response.function_call_arguments.delta",
        output_index: itemIdx,
        call_id: rTool.id,
        delta: argsJson
      });
      outEvents += formatSseEvent("response.function_call_arguments.done", {
        type: "response.function_call_arguments.done",
        output_index: itemIdx,
        call_id: rTool.id,
        arguments: argsJson
      });
      outEvents += formatSseEvent("response.output_item.done", {
        type: "response.output_item.done",
        output_index: itemIdx,
        item: fcDoneItem
      });
      state.outputItems.push(fcDoneItem);
    }
    if (isFinalChunk && !state.completed) {
      state.completed = true;
      if (state.textBlockOpen) {
        if (state.autoRouteBadge && !state.sawToolCall && !state.badgeAppended) {
          var finalBadgeDelta = "\n\n" + state.autoRouteBadge;
          state.accumulatedText += finalBadgeDelta;
          state.badgeAppended = true;
          outEvents += formatSseEvent("response.output_text.delta", {
            type: "response.output_text.delta",
            output_index: state.textItemIndex,
            content_index: 0,
            delta: finalBadgeDelta
          });
        }
        var finalMsgItem = {
          id: "msg_item_" + state.textItemIndex,
          type: "message",
          status: "completed",
          role: "assistant",
          content: [
            {
              type: "output_text",
              text: state.accumulatedText,
              annotations: []
            }
          ]
        };
        outEvents += formatSseEvent("response.output_item.done", {
          type: "response.output_item.done",
          output_index: state.textItemIndex,
          item: finalMsgItem
        });
        state.outputItems.push(finalMsgItem);
        state.textBlockOpen = false;
      }
      var outToks = state.outputTokens || result.eventTokenCount;
      outEvents += formatSseEvent("response.completed", {
        type: "response.completed",
        response: {
          id: "resp_stream_" + Date.now(),
          object: "response",
          status: "completed",
          model: modelId,
          output: state.outputItems,
          usage: {
            input_tokens: state.inputTokens,
            output_tokens: outToks,
            total_tokens: state.inputTokens + outToks
          }
        }
      });
    }
    result.transformedEvent = outEvents;
  } else if (clientProtocol === "openai_chat") {
    if (!textDelta && toolCalls.length === 0 && !isFinalChunk) {
      result.transformedEvent = "";
      return result;
    }
    var deltaObj = {};
    if (!state.started) {
      state.started = true;
      deltaObj.role = "assistant";
    }
    if (textDelta) {
      deltaObj.content = textDelta;
    }
    if (toolCalls.length > 0) {
      deltaObj.tool_calls = [];
      for (var ctcIdx = 0; ctcIdx < toolCalls.length; ctcIdx++) {
        deltaObj.tool_calls.push({
          index: ctcIdx,
          id: toolCalls[ctcIdx].id,
          type: "function",
          function: {
            name: toolCalls[ctcIdx].name,
            arguments: JSON.stringify(toolCalls[ctcIdx].args || {})
          }
        });
      }
    }
    var oaiFinishReason = null;
    if (isFinalChunk) {
      if (finishReasonNorm === "tool_use" || state.sawToolCall) {
        oaiFinishReason = "tool_calls";
      } else if (finishReasonNorm === "max_tokens") {
        oaiFinishReason = "length";
      } else {
        oaiFinishReason = "stop";
      }
    }
    var chatChunk = {
      id: "chatcmpl_stream",
      object: "chat.completion.chunk",
      model: modelId,
      choices: [
        {
          index: 0,
          delta: deltaObj,
          finish_reason: oaiFinishReason
        }
      ]
    };
    if (isFinalChunk) {
      chatChunk.usage = {
        prompt_tokens: state.inputTokens,
        completion_tokens: state.outputTokens,
        total_tokens: state.inputTokens + state.outputTokens
      };
    }
    result.transformedEvent = "data: " + JSON.stringify(chatChunk) + "\n\n";
    if (isFinalChunk && !state.completed) {
      state.completed = true;
      result.transformedEvent += "data: [DONE]\n\n";
    }
  } else if (clientProtocol === "vertex_gemini") {
    var gemParts = [];
    if (textDelta) {
      gemParts.push({ text: textDelta });
    }
    for (var gtc = 0; gtc < toolCalls.length; gtc++) {
      var gPart = {
        functionCall: {
          name: toolCalls[gtc].name,
          args: toolCalls[gtc].args || {}
        }
      };
      if (toolCalls[gtc].thoughtSignature) {
        gPart.thoughtSignature = toolCalls[gtc].thoughtSignature;
      }
      gemParts.push(gPart);
    }
    if (gemParts.length === 0 && !isFinalChunk) {
      result.transformedEvent = "";
      return result;
    }
    var candidateObj = {
      content: {
        role: "model",
        parts: gemParts.length > 0 ? gemParts : [{ text: "" }]
      },
      index: 0
    };
    // Only set finishReason on the final chunk so Antigravity does not stop after chunk 1
    if (isFinalChunk) {
      state.completed = true;
      candidateObj.finishReason =
        finishReasonNorm === "max_tokens" ? "MAX_TOKENS" : "STOP";
    }
    var gemChunk = {
      candidates: [candidateObj],
      usageMetadata: {
        promptTokenCount: state.inputTokens,
        candidatesTokenCount: state.outputTokens,
        totalTokenCount: state.inputTokens + state.outputTokens
      },
      modelVersion: modelId
    };
    result.transformedEvent = "data: " + JSON.stringify(gemChunk) + "\n\n";
  }

  return result;
}

function runTransformStreamEvent(context) {
  var rawEvent = context.getVariable("response.event.current.content") || "";
  var clientProtocol = context.getVariable("client_protocol") || "anthropic_messages";
  var targetFamily =
    context.getVariable("target_model_family") ||
    context.getVariable("ai.targetFamily") ||
    "gemini";
  var targetModelId =
    context.getVariable("target_model_id") ||
    context.getVariable("ai.selectedModel") ||
    "gemini-3.1-pro-preview";

  var streamState = null;
  var savedStateJson = context.getVariable("sse_stream_state_json");
  if (savedStateJson) {
    try {
      streamState = JSON.parse(savedStateJson);
    } catch (e) {
      streamState = null;
    }
  }
  if (!streamState) {
    streamState = {
      started: context.getVariable("sse_stream_started") === "true",
      textBlockOpen: context.getVariable("sse_text_block_open") === "true",
      completed: context.getVariable("sse_stream_completed") === "true",
      blockIndex: parseInt(context.getVariable("sse_block_index") || "0", 10),
      sawToolCall: context.getVariable("sse_saw_tool_call") === "true"
    };
  }
  if (context.getVariable("ai.autoRouteBadge") === "true") {
    streamState.autoRouteBadge = context.getVariable("ai.smartRouterBadge") || "";
  }
  var reqStartMs = parseInt(context.getVariable("ai.requestStartMs") || "0", 10);
  if (reqStartMs > 0 && !streamState.requestStartMs) {
    streamState.requestStartMs = reqStartMs;
  }

  var out = transformSSEChunk(
    rawEvent,
    clientProtocol,
    targetFamily,
    targetModelId,
    streamState
  );

  context.setVariable("sse_stream_state_json", JSON.stringify(out.streamState));
  context.setVariable("sse_stream_started", out.streamState.started ? "true" : "false");
  context.setVariable(
    "sse_text_block_open",
    out.streamState.textBlockOpen ? "true" : "false"
  );
  context.setVariable(
    "sse_stream_completed",
    out.streamState.completed ? "true" : "false"
  );
  context.setVariable("sse_block_index", String(out.streamState.blockIndex));
  context.setVariable(
    "sse_saw_tool_call",
    out.streamState.sawToolCall ? "true" : "false"
  );

  context.setVariable("sse_event_token_count", String(out.eventTokenCount));
  context.setVariable("ai.streamingTokenCount", String(out.eventTokenCount));
  context.setVariable("ai.streamTokenCount", String(out.eventTokenCount));
  context.setVariable("response.event.current.content", out.transformedEvent);

  // Populate FinOps DataCapture flow variables (DC-CaptureFinOpsMetrics) on streaming turns
  var streamInToks = Number(out.streamState.inputTokens || 0);
  var streamOutToks = Number(out.streamState.outputTokens || 0);
  var streamCachedToks = Number(out.streamState.cachedInputTokens || 0);
  var streamCacheCreateToks = Number(out.streamState.cacheCreationTokens || 0);
  var streamThoughtsToks = Number(out.streamState.thoughtsTokens || 0);
  var streamTotalToks = streamInToks + streamOutToks;
  if (streamTotalToks > 0 || out.eventTokenCount > 0) {
    var effTotalToks = streamTotalToks > 0 ? streamTotalToks : Number(out.eventTokenCount || 0);
    var inputCostPer1M = parseFloat(context.getVariable("model_input_cost_1m") || "2.00");
    var outputCostPer1M = parseFloat(context.getVariable("model_output_cost_1m") || "12.00");
    var cacheReadDiscount = targetFamily === "anthropic" ? 0.10 : 0.25;
    var uncachedInToks = Math.max(0, streamInToks - streamCachedToks - streamCacheCreateToks);
    var streamCostUsd =
      (uncachedInToks / 1000000.0) * inputCostPer1M +
      (streamCachedToks / 1000000.0) * (inputCostPer1M * cacheReadDiscount) +
      (streamCacheCreateToks / 1000000.0) * (inputCostPer1M * 1.25) +
      (streamOutToks / 1000000.0) * outputCostPer1M;
    var streamFrontierCostUsd =
      (uncachedInToks / 1000000.0) * 15.0 +
      (streamCachedToks / 1000000.0) * (15.0 * 0.10) +
      (streamCacheCreateToks / 1000000.0) * (15.0 * 1.25) +
      (streamOutToks / 1000000.0) * 75.0;
    var streamSavingsUsd = Math.max(0.0, streamFrontierCostUsd - streamCostUsd);
    var fundingPool =
      targetFamily === "gemini"
        ? "ANTIGRAVITY_GEMINI_CREDITS"
        : "GCP_COMMIT_FSP_DISCOUNT";
    var fspDiscountPct = parseFloat(context.getVariable("org_fsp_discount_pct") || "15.0");
    if (isNaN(fspDiscountPct) || fspDiscountPct < 0) fspDiscountPct = 15.0;
    if (fspDiscountPct > 100.0) fspDiscountPct = 100.0;
    var netCashCostUsd =
      fundingPool === "ANTIGRAVITY_GEMINI_CREDITS"
        ? 0.0
        : streamCostUsd * Math.max(0.0, 1.0 - fspDiscountPct / 100.0);
    var totalNetSavingsUsd = Math.max(0.0, streamFrontierCostUsd - netCashCostUsd);
    var totalLatencyMs = reqStartMs > 0 ? Math.max(1, Date.now() - reqStartMs) : 1;
    var ttftMs =
      out.streamState.ttftMs !== null && out.streamState.ttftMs !== undefined
        ? Number(out.streamState.ttftMs)
        : totalLatencyMs;
    var vertexModelId =
      context.getVariable("vertex_model_id") ||
      context.getVariable("ai.vertexModelId") ||
      targetModelId;
    var devEmail =
      context.getVariable("developer_email") ||
      context.getVariable("ai.developerEmail") ||
      context.getVariable("developer.email") ||
      context.getVariable("verifyapikey.VA-VerifyApiKey.developer.email") ||
      context.getVariable("auth.oidc_email") ||
      "unknown@enterprise.internal";
    var teamName =
      context.getVariable("ai.teamName") ||
      context.getVariable("request.header.x-team-name") ||
      context.getVariable("verifyapikey.VA-VerifyApiKey.developer.team_name") ||
      context.getVariable("verifyapikey.VA-VerifyApiKey.developer.attribute.team") ||
      "platform-engineering";
    var costCenter =
      context.getVariable("ai.costCenter") ||
      context.getVariable("request.header.x-cost-center") ||
      context.getVariable("verifyapikey.VA-VerifyApiKey.developer.cost_center") ||
      context.getVariable("verifyapikey.VA-VerifyApiKey.developer.attribute.cost_center") ||
      "CC-1000";
    var codingHarness =
      context.getVariable("coding_harness") ||
      context.getVariable("ai.codingHarness") ||
      context.getVariable("coding_harness_name") ||
      clientProtocol;

    var cacheHitRatePct =
      streamInToks > 0 ? ((streamCachedToks / streamInToks) * 100.0).toFixed(1) : "0.0";
    var usedSmartJudge =
      context.getVariable("ai.useSmartJudge") === "true" ||
      context.getVariable("needs_smart_judge") === "true";
    var routerCostUsd = usedSmartJudge ? 0.000018 : 0.0;
    var routerTier =
      context.getVariable("target_model_tier") ||
      context.getVariable("ai.selectedTier") ||
      "tier2-daily";
    var routingMode =
      context.getVariable("routing_mode") ||
      context.getVariable("ai.routingMode") ||
      (usedSmartJudge
        ? "smart_auto"
        : routerTier === "tier3-frontier"
        ? "manual_frontier_pin"
        : "daily_direct");
    var recommendedTier =
      context.getVariable("recommended_tier") ||
      context.getVariable("ai.recommendedTier") ||
      routerTier;
    var recommendedModel =
      context.getVariable("recommended_model") ||
      context.getVariable("ai.recommendedModel") ||
      targetModelId;
    var recommendedInRate = parseFloat(context.getVariable("recommended_in_rate") || "1.25");
    var recommendedOutRate = parseFloat(context.getVariable("recommended_out_rate") || "10.0");
    var recommendedCostUsd =
      (uncachedInToks / 1000000.0) * recommendedInRate +
      (streamCachedToks / 1000000.0) * (recommendedInRate * 0.25) +
      (streamCacheCreateToks / 1000000.0) * (recommendedInRate * 1.25) +
      (streamOutToks / 1000000.0) * recommendedOutRate;
    var isFrontierOverkill =
      routingMode === "manual_frontier_pin" &&
      routerTier === "tier3-frontier" &&
      recommendedTier !== "tier3-frontier";
    var avoidableFrontierSpendUsd = isFrontierOverkill
      ? Math.max(0.0, streamCostUsd - recommendedCostUsd)
      : 0.0;
    var workloadCategory =
      context.getVariable("workload_category") ||
      context.getVariable("ai.workloadCategory") ||
      "Feature & Code Editing";
    var projectName =
      context.getVariable("project_name") ||
      context.getVariable("ai.projectName") ||
      "codingagent-modelrouter";
    var sessionId =
      context.getVariable("session_id") ||
      context.getVariable("ai.sessionId") ||
      "sess_default";
    var routerDecision =
      context.getVariable("smart_router_decision") ||
      context.getVariable("ai.smartRouterDecision") ||
      context.getVariable("smart_judge_reason") ||
      context.getVariable("ai.smartJudgeReason") ||
      "explicit_model";
    var isRouterOverride =
      context.getVariable("ai.isRouterOverride") === "true" || Boolean(isFrontierOverkill);
    var budgetAction = context.getVariable("ai.budgetAction") || "within_budget";

    var gatewayKeyName =
      context.getVariable("ai.gatewayKeyName") ||
      context.getVariable("gateway_key_name") ||
      "default-gateway-key";
    var credentialType =
      context.getVariable("ai.credentialType") ||
      context.getVariable("credential_type") ||
      "user_key";
    var userIdSource =
      context.getVariable("ai.userIdSource") ||
      context.getVariable("user_id_source") ||
      "api_key_owner";
    var ingestionMode = context.getVariable("ai.finopsIngestionMode") || "USAGE";
    var envName = context.getVariable("ai.environment") || "production";
    var tagsCsv = context.getVariable("ai.tags") || "";
    var traceId = context.getVariable("ai.traceId") || "";
    var spanId = context.getVariable("ai.spanId") || "";
    var gatewayReqId =
      context.getVariable("ai.gatewayRequestId") ||
      "req_" + Date.now().toString(36);
    var gcpProjectId =
      context.getVariable("gcp_project_id") ||
      context.getVariable("organization.name") ||
      "";

    context.setVariable("ai.inputTokens", String(streamInToks));
    context.setVariable("ai.promptTokenCount", String(streamInToks));
    context.setVariable("ai.outputTokens", String(streamOutToks));
    context.setVariable("ai.completionTokenCount", String(streamOutToks));
    context.setVariable("ai.cachedTokens", String(streamCachedToks));
    context.setVariable("ai.cacheCreationTokens", String(streamCacheCreateToks));
    context.setVariable("ai.thoughtsTokens", String(streamThoughtsToks));
    context.setVariable("ai.cacheHitRatePct", cacheHitRatePct);
    context.setVariable("ai.totalTokenCount", String(effTotalToks));
    context.setVariable("ai.estimatedCostUsd", streamCostUsd.toFixed(6));
    context.setVariable("ai.netCashCostUsd", netCashCostUsd.toFixed(6));
    context.setVariable("ai.totalNetSavingsUsd", totalNetSavingsUsd.toFixed(6));
    context.setVariable("net_cash_cost_usd", netCashCostUsd.toFixed(6));
    context.setVariable("total_latency_ms", String(totalLatencyMs));
    context.setVariable("ttft_ms", String(ttftMs));
    context.setVariable("ai.totalLatencyMs", String(totalLatencyMs));
    context.setVariable("ai.ttftMs", String(ttftMs));
    context.setVariable("ai.routerCostUsd", routerCostUsd.toFixed(6));
    context.setVariable("ai.frontierBaselineCostUsd", streamFrontierCostUsd.toFixed(6));
    context.setVariable("ai.routingSavingsUsd", streamSavingsUsd.toFixed(6));
    context.setVariable("ai.avoidableFrontierSpendUsd", avoidableFrontierSpendUsd.toFixed(6));
    context.setVariable("ai.isFrontierOverkill", isFrontierOverkill ? "true" : "false");
    context.setVariable("ai.fundingPool", fundingPool);
    context.setVariable("ai.routingMode", routingMode);
    context.setVariable("ai.recommendedTier", recommendedTier);
    context.setVariable("ai.recommendedModel", recommendedModel);
    context.setVariable("ai.workloadCategory", workloadCategory);
    context.setVariable("ai.projectName", projectName);
    context.setVariable("ai.sessionId", sessionId);
    context.setVariable("ai.developerEmail", devEmail);
    context.setVariable("ai.gatewayKeyName", gatewayKeyName);
    context.setVariable("ai.credentialType", credentialType);
    context.setVariable("ai.userIdSource", userIdSource);
    context.setVariable("ai.finopsIngestionMode", ingestionMode);
    var physicalModel =
      context.getVariable("ai.physical_model") ||
      vertexModelId;
    context.setVariable("ai.teamName", teamName);
    context.setVariable("ai.costCenter", costCenter);
    context.setVariable("ai.codingHarness", codingHarness);
    context.setVariable("ai.targetVertexModel", vertexModelId);
    context.setVariable("ai.physicalModel", physicalModel);
    context.setVariable("ai.physical_model", physicalModel);
    context.setVariable("ai.routedModel", String(targetModelId));
    context.setVariable("ai.routingTier", String(routerTier));

    if (ingestionMode === "NONE") {
      context.setVariable("ai.finopsLogPayload", "");
      context.setVariable("ai.finopsStreamReady", "false");
    } else {
      // null (not 0) when no JS-PrepareSmartJudge step timed the classifier callout.
      var routerClassifierMsRaw = context.getVariable("ai.routerClassifierMs");
      var routerClassifierMsLog =
        routerClassifierMsRaw === null ||
        routerClassifierMsRaw === undefined ||
        String(routerClassifierMsRaw) === ""
          ? null
          : Number(routerClassifierMsRaw);

      var finopsLogEntry = {
        event_type: "ai_router_finops_turn",
        timestamp: new Date().toISOString(),
        gateway_request_id: String(gatewayReqId),
        trace_id: String(traceId),
        span_id: String(spanId),
        developer_email: String(devEmail),
        quota_owner_id: String(
          context.getVariable("quota_owner_id") ||
            context.getVariable("auth.developer_email") ||
            devEmail
        ),
        gateway_key_name: String(gatewayKeyName),
        credential_type: String(credentialType),
        user_id_source: String(userIdSource),
        ingestion_mode: String(ingestionMode),
        environment: String(envName),
        tags: tagsCsv ? String(tagsCsv).split(",") : [],
        team_name: String(teamName),
        cost_center: String(costCenter),
        coding_harness: String(codingHarness),
        client_protocol: String(clientProtocol),
        project_name: String(projectName),
        session_id: String(sessionId),
        workload_category: String(workloadCategory),
        routing_mode: String(routingMode),
        requested_model: String(
          context.getVariable("ai.clientRequestedModel") ||
            context.getVariable("requested_model") ||
            targetModelId
        ),
        routed_model: String(targetModelId),
        vertex_model_id: String(vertexModelId),
        physical_model_id: String(physicalModel),
        model_family: String(targetFamily),
        model_tier: String(routerTier),
        actual_tier: String(routerTier),
        recommended_tier: String(recommendedTier),
        recommended_model: String(recommendedModel),
        is_frontier_overkill: Boolean(isFrontierOverkill),
        is_router_override: Boolean(isRouterOverride),
        is_subagent: context.getVariable("ai.isSubagent") === "true",
        fallback_triggered: context.getVariable("vertex_fallback_triggered") === "true",
        budget_action: String(budgetAction),
        smart_router_decision: String(routerDecision),
        router_classifier: String(context.getVariable("ai.routerClassifier") || ""),
        router_classifier_ms: routerClassifierMsLog,
        funding_pool: String(fundingPool),
        ttft_ms: Number(ttftMs),
        total_latency_ms: Number(totalLatencyMs),
        input_tokens: Number(streamInToks),
        output_tokens: Number(streamOutToks),
        cached_tokens: Number(streamCachedToks),
        cache_creation_tokens: Number(streamCacheCreateToks),
        thoughts_tokens: Number(streamThoughtsToks),
        total_tokens: Number(effTotalToks),
        cache_hit_rate_pct: parseFloat(cacheHitRatePct),
        actual_cost_usd: parseFloat(streamCostUsd.toFixed(6)),
        net_cash_cost_usd: parseFloat(netCashCostUsd.toFixed(6)),
        total_net_savings_usd: parseFloat(totalNetSavingsUsd.toFixed(6)),
        router_cost_usd: parseFloat(routerCostUsd.toFixed(6)),
        frontier_baseline_cost_usd: parseFloat(streamFrontierCostUsd.toFixed(6)),
        routing_savings_usd: parseFloat(streamSavingsUsd.toFixed(6)),
        avoidable_frontier_spend_usd: parseFloat(avoidableFrontierSpendUsd.toFixed(6))
      };
      if (gcpProjectId && traceId) {
        finopsLogEntry["logging.googleapis.com/trace"] =
          "projects/" + String(gcpProjectId) + "/traces/" + String(traceId);
        if (spanId) {
          finopsLogEntry["logging.googleapis.com/spanId"] = String(spanId);
        }
        finopsLogEntry["logging.googleapis.com/trace_sampled"] = true;
      }
      if (ingestionMode === "FULL") {
        finopsLogEntry.trace_payload = {
          prompt_preview: String(
            context.getVariable("ai.latestUserPrompt") ||
              context.getVariable("prompt_text_for_armor") ||
              ""
          ).slice(0, 2000),
          response_preview: String(out.streamState.accumulatedText || "").slice(0, 2000),
          tool_call_names: out.streamState.toolCallNames || []
        };
      }
      context.setVariable("ai.finopsLogPayload", JSON.stringify(finopsLogEntry));
      if (
        out.streamState.completed &&
        context.getVariable("ai.finopsStreamLogged") !== "true"
      ) {
        context.setVariable("ai.finopsStreamReady", "true");
        context.setVariable("ai.finopsStreamLogged", "true");
      } else {
        context.setVariable("ai.finopsStreamReady", "false");
      }
    }
  } else {
    context.setVariable("ai.finopsStreamReady", "false");
  }
}

if (typeof context !== "undefined") {
  runTransformStreamEvent(context);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    transformSSEChunk: transformSSEChunk,
    runTransformStreamEvent: runTransformStreamEvent
  };
}
