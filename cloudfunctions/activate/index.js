"use strict";

const fs = require("node:fs");
const path = require("node:path");
const https = require("node:https");
const { URL } = require("node:url");
const { createBridgeServer } = require("./bridge");

const DEFAULT_SELFHOSTED_ORIGIN = "https://103.236.97.137:35208";
const LEGACY_CLOUDBASE_HOST = "appletv-d5ge1bth794873f76.api.tcloudbasegateway.com";

function selfhostedOrigin() {
  const url = new URL(process.env.OTV_SELFHOSTED_BASE_URL || DEFAULT_SELFHOSTED_ORIGIN);
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new Error("OTV_SELFHOSTED_BASE_URL must be an HTTPS origin");
  }
  if (url.hostname === LEGACY_CLOUDBASE_HOST || url.hostname.endsWith(".tcloudbase.com") || url.hostname.endsWith(".tcloudbasegateway.com")) {
    throw new Error("CloudBase cannot be used as its own bridge upstream");
  }
  return url.origin;
}

const origin = selfhostedOrigin();
const ca = fs.readFileSync(path.join(__dirname, "mytv_migration_ca.pem"));
const server = createBridgeServer({ functionName: "activate", baseUrl: origin, ca });
const port = Number(process.env.PORT || 9000);

https.globalAgent.keepAlive = true;
server.listen(port, "0.0.0.0", () => console.log(`[activate-bridge] listening on ${port}`));
