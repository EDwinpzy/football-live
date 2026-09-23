"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { URL } = require("node:url");
const { createBridgeServer } = require("./bridge");

const DEFAULT_SELFHOSTED_ORIGIN = "https://103.236.97.137:35208";
const CLOUDBASE_API_ORIGIN = "https://appletv-d5ge1bth794873f76.api.tcloudbasegateway.com";

function selfhostedOrigin() {
  const url = new URL(process.env.OTV_SELFHOSTED_BASE_URL || DEFAULT_SELFHOSTED_ORIGIN);
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new Error("OTV_SELFHOSTED_BASE_URL must be an HTTPS origin");
  }
  if (url.hostname.endsWith(".tcloudbase.com") || url.hostname.endsWith(".tcloudbasegateway.com")) {
    throw new Error("CloudBase cannot be used as its own bridge upstream");
  }
  return url.origin;
}

const origin = selfhostedOrigin();
const ca = fs.readFileSync(path.join(__dirname, "mytv_migration_ca.pem"));
const server = createBridgeServer({
  functionName: "hotupdate",
  baseUrl: origin,
  ca,
  legacyDownloadBase: `${CLOUDBASE_API_ORIGIN}/v1/storages/object/hotupdate/`,
});
const port = Number(process.env.PORT || 9000);

server.listen(port, "0.0.0.0", () => console.log(`[hotupdate-bridge] listening on ${port}`));
