"use strict";

const http = require("node:http");
const https = require("node:https");
const { URL } = require("node:url");

const MAX_REQUEST_BYTES = 1024 * 1024;
const MAX_HOTUPDATE_RESPONSE_BYTES = 2 * 1024 * 1024;
const REQUEST_HEADERS = [
  "accept",
  "content-length",
  "content-type",
  "if-modified-since",
  "if-none-match",
  "if-range",
  "range",
  "user-agent",
];
const RESPONSE_HEADERS = [
  "accept-ranges",
  "cache-control",
  "content-length",
  "content-range",
  "content-type",
  "etag",
  "expires",
  "last-modified",
  "retry-after",
];

function normalizedPath(pathname) {
  return pathname || "/";
}

function resolveTarget(functionName, method, pathname, search = "") {
  const path = normalizedPath(pathname);
  const verb = String(method || "GET").toUpperCase();
  let targetPath = null;

  if (functionName === "activate") {
    if (path === "/" || path === "/activate") {
      if (verb === "POST") targetPath = "/activate";
      else if (verb === "OPTIONS") targetPath = "/activate";
    }
  } else if (functionName === "hotupdate") {
    const aliases = new Map([
      ["/", "/hotupdate/check"],
      ["/check", "/hotupdate/check"],
      ["/hotupdate", "/hotupdate/check"],
      ["/hotupdate/check", "/hotupdate/check"],
      ["/announce", "/hotupdate/announce"],
      ["/hotupdate/announce", "/hotupdate/announce"],
      ["/report", "/hotupdate/report"],
      ["/hotupdate/report", "/hotupdate/report"],
      ["/vod-sources/check", "/hotupdate/vod-sources/check"],
      ["/hotupdate/vod-sources/check", "/hotupdate/vod-sources/check"],
    ]);
    const mapped = aliases.get(path);
      const expectedMethod = mapped === "/hotupdate/announce" ? "GET" : "POST";
    if (mapped && (verb === expectedMethod || verb === "OPTIONS")) targetPath = mapped;
  } else if (functionName === "iptv") {
    if (path === "/" || path === "/latest" || path === "/iptv/latest") {
      if (verb === "GET" || verb === "OPTIONS") targetPath = "/iptv/latest";
    }
  }

  if (!targetPath) return null;
  return `${targetPath}${search || ""}`;
}

function corsHeaders(method) {
  const methods = method === "GET" ? "GET, OPTIONS" : method === "POST" ? "POST, OPTIONS" : "GET, POST, OPTIONS";
  return {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": methods,
    "access-control-allow-headers": "Content-Type, Range, If-Range, If-None-Match, If-Modified-Since",
    "access-control-max-age": "600",
  };
}

function allowedRequestHeaders(headers) {
  const out = {};
  for (const name of REQUEST_HEADERS) {
    if (headers[name] !== undefined) out[name] = headers[name];
  }
  return out;
}

function allowedResponseHeaders(headers) {
  const out = {};
  for (const name of RESPONSE_HEADERS) {
    if (headers[name] !== undefined) out[name] = headers[name];
  }
  return out;
}

function rewrittenHotupdateBody(body, legacyDownloadBase) {
  if (!legacyDownloadBase) return body;
  try {
    const payload = JSON.parse(body.toString("utf8"));
    const key = payload && payload.pkg && payload.pkg.objectKey;
    if (typeof key !== "string" || !key || key.startsWith("/") || key.split("/").some((part) => !part || part === "." || part === "..")) {
      return body;
    }
    const base = new URL(legacyDownloadBase);
    if (base.protocol !== "https:" || base.username || base.password || !base.pathname.endsWith("/")) return body;
    payload.pkg.url = `${base.origin}${base.pathname}${key.split("/").map(encodeURIComponent).join("/")}`;
    return Buffer.from(JSON.stringify(payload), "utf8");
  } catch (_error) {
    return body;
  }
}

function createBridgeServer({ functionName, baseUrl, ca, legacyDownloadBase }) {
  const origin = new URL(baseUrl);
  const transport = origin.protocol === "https:" ? https : http;
  const agent = origin.protocol === "https:" ? new https.Agent({ ca, keepAlive: true }) : undefined;

  return http.createServer((req, res) => {
    let incoming;
    try {
      incoming = new URL(req.url || "/", "http://cloudbase.invalid");
    } catch (_error) {
      res.writeHead(400, { "content-type": "text/plain; charset=utf-8", ...corsHeaders(req.method) });
      res.end("bad request");
      return;
    }
    const target = resolveTarget(functionName, req.method, incoming.pathname, incoming.search);
    if (!target) {
      res.writeHead(404, { "content-type": "text/plain; charset=utf-8", ...corsHeaders(req.method) });
      res.end("not found");
      return;
    }
    if (req.method === "OPTIONS") {
      res.writeHead(204, corsHeaders(functionName === "iptv" ? "GET" : "POST"));
      res.end();
      return;
    }

    const chunks = [];
    let requestBytes = 0;
    let requestTooLarge = false;
    req.on("data", (chunk) => {
      requestBytes += chunk.length;
      if (requestBytes > MAX_REQUEST_BYTES) {
        requestTooLarge = true;
        chunks.length = 0;
        return;
      }
      if (requestTooLarge) return;
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (res.writableEnded || req.destroyed) return;
      if (requestTooLarge) {
        res.writeHead(413, { "content-type": "text/plain; charset=utf-8", ...corsHeaders(req.method) });
        res.end("request too large");
        return;
      }
      const headers = allowedRequestHeaders(req.headers);
      headers.host = origin.host;
      const upstream = transport.request({
        protocol: origin.protocol,
        hostname: origin.hostname,
        port: origin.port || undefined,
        method: req.method,
        path: target,
        headers,
        agent,
        timeout: 30000,
      }, (upstreamRes) => {
        const responseHeaders = { ...allowedResponseHeaders(upstreamRes.headers), ...corsHeaders(req.method) };
        const rewrite = functionName === "hotupdate"
          && (target === "/hotupdate/check" || target.startsWith("/hotupdate/check?"))
          && String(upstreamRes.headers["content-type"] || "").toLowerCase().includes("application/json");
        if (!rewrite) {
          res.writeHead(upstreamRes.statusCode || 502, responseHeaders);
          upstreamRes.pipe(res);
          return;
        }

        const responseChunks = [];
        let responseBytes = 0;
        upstreamRes.on("data", (chunk) => {
          responseBytes += chunk.length;
          if (responseBytes > MAX_HOTUPDATE_RESPONSE_BYTES) {
            upstreamRes.destroy();
            if (!res.headersSent) {
              res.writeHead(502, { "content-type": "application/json; charset=utf-8", ...corsHeaders(req.method) });
              res.end('{"ret":502,"msg":"upstream_response_too_large"}');
            }
            return;
          }
          responseChunks.push(chunk);
        });
        upstreamRes.on("end", () => {
          const body = rewrittenHotupdateBody(Buffer.concat(responseChunks), legacyDownloadBase);
          delete responseHeaders["content-length"];
          responseHeaders["content-length"] = String(body.length);
          res.writeHead(upstreamRes.statusCode || 502, responseHeaders);
          res.end(body);
        });
      });
      upstream.on("timeout", () => upstream.destroy(new Error("upstream timeout")));
      upstream.on("error", () => {
        if (!res.headersSent) {
          res.writeHead(502, { "content-type": "application/json; charset=utf-8", ...corsHeaders(req.method) });
          res.end('{"ret":502,"msg":"upstream_unavailable"}');
        } else {
          res.destroy();
        }
      });
      const body = Buffer.concat(chunks);
      if (body.length) upstream.write(body);
      upstream.end();
    });
  });
}

module.exports = { createBridgeServer, resolveTarget };
