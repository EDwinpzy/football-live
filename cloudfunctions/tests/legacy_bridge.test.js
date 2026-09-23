const assert = require("node:assert/strict");
const http = require("node:http");
const { after, before, test } = require("node:test");
const { createBridgeServer, resolveTarget } = require("../activate/bridge");

let upstream;
let upstreamOrigin;
let lastUpstreamRequest;

before(async () => {
  upstream = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const body = Buffer.concat(chunks);
      lastUpstreamRequest = { method: req.method, url: req.url, headers: req.headers, body: body.toString("utf8") };
      if (req.url.startsWith("/hotupdate/check")) {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({
          ret: 0,
          hasUpdate: true,
          pkg: { objectKey: "versions/next.zip", url: "https://selfhosted.invalid/private.zip" },
        }));
      }
      if (req.url.startsWith("/iptv/latest")) {
        res.writeHead(200, { "Content-Type": "audio/x-mpegurl; charset=utf-8" });
        return res.end("#EXTM3U\nhttps://media.example/live.m3u8\n");
      }
      res.writeHead(206, {
        "Content-Type": "application/octet-stream",
        "Content-Range": "bytes 0-3/4",
        "Accept-Ranges": "bytes",
        "X-Not-Forwarded": "discard-me",
      });
      res.end(Buffer.from([0, 1, 254, 255]));
    });
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  upstreamOrigin = `http://127.0.0.1:${upstream.address().port}`;
});

after(async () => {
  await new Promise((resolve) => upstream.close(resolve));
});

test("legacy routes map only the documented My TV endpoints", () => {
  assert.equal(resolveTarget("activate", "POST", "/", ""), "/activate");
  assert.equal(resolveTarget("hotupdate", "POST", "/check", "?v=2"), "/hotupdate/check?v=2");
  assert.equal(resolveTarget("hotupdate", "GET", "/announce", ""), "/hotupdate/announce");
  assert.equal(resolveTarget("hotupdate", "POST", "/vod-sources/check", ""), "/hotupdate/vod-sources/check");
  assert.equal(resolveTarget("iptv", "GET", "/latest", ""), "/iptv/latest");
  assert.equal(resolveTarget("activate", "GET", "/admin", ""), null);
  assert.equal(resolveTarget("hotupdate", "DELETE", "/check", ""), null);
  assert.equal(resolveTarget("iptv", "POST", "/refresh", ""), null);
});

test("activate bridge preserves method, query, selected headers, body, status and bytes", async () => {
  const bridge = createBridgeServer({ functionName: "activate", baseUrl: upstreamOrigin });
  await new Promise((resolve) => bridge.listen(0, "127.0.0.1", resolve));
  try {
    const response = await fetch(`http://127.0.0.1:${bridge.address().port}/?trace=42`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Forwarded-For": "spoofed" },
      body: "{\"protocol\":2,\"action\":\"renew\",\"code\":\"OTV-ABCDE-23456\",\"currentCode\":\"OTV-ZYXWV-98765\",\"deviceId\":\"device-v2\"}",
    });
    assert.equal(response.status, 206);
    assert.equal(response.headers.get("content-range"), "bytes 0-3/4");
    assert.equal(response.headers.get("accept-ranges"), "bytes");
    assert.equal(response.headers.get("x-not-forwarded"), null);
    assert.deepEqual([...new Uint8Array(await response.arrayBuffer())], [0, 1, 254, 255]);
    assert.equal(lastUpstreamRequest.method, "POST");
    assert.equal(lastUpstreamRequest.url, "/activate?trace=42");
    assert.equal(lastUpstreamRequest.headers["content-type"], "application/json");
    assert.equal(lastUpstreamRequest.headers["x-forwarded-for"], undefined);
    assert.equal(lastUpstreamRequest.body,
      "{\"protocol\":2,\"action\":\"renew\",\"code\":\"OTV-ABCDE-23456\",\"currentCode\":\"OTV-ZYXWV-98765\",\"deviceId\":\"device-v2\"}");
  } finally {
    await new Promise((resolve) => bridge.close(resolve));
  }
});

test("hotupdate bridge rewrites package URLs to the retained CloudBase file mirror", async () => {
  const bridge = createBridgeServer({
    functionName: "hotupdate",
    baseUrl: upstreamOrigin,
    legacyDownloadBase: "https://legacy.example/v1/storages/object/hotupdate/",
  });
  await new Promise((resolve) => bridge.listen(0, "127.0.0.1", resolve));
  try {
    const response = await fetch(`http://127.0.0.1:${bridge.address().port}/check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const result = await response.json();
    assert.equal(result.ret, 0);
    assert.equal(result.pkg.url, "https://legacy.example/v1/storages/object/hotupdate/versions/next.zip");
  } finally {
    await new Promise((resolve) => bridge.close(resolve));
  }
});

test("IPTV bridge returns the upstream playlist and rejects unrelated paths", async () => {
  const bridge = createBridgeServer({ functionName: "iptv", baseUrl: upstreamOrigin });
  await new Promise((resolve) => bridge.listen(0, "127.0.0.1", resolve));
  try {
    const root = `http://127.0.0.1:${bridge.address().port}`;
    const response = await fetch(`${root}/latest`);
    assert.equal(response.status, 200);
    assert.match(response.headers.get("content-type"), /mpegurl/);
    assert.match(await response.text(), /^#EXTM3U/);
    const denied = await fetch(`${root}/refresh`, { method: "POST" });
    assert.equal(denied.status, 404);
  } finally {
    await new Promise((resolve) => bridge.close(resolve));
  }
});
