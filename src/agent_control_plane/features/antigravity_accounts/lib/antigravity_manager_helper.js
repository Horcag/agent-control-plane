"use strict";

const crypto = require("crypto");
const fs = require("fs");
const { createRequire } = require("module");
const path = require("path");
const { app, safeStorage } = require("electron");

const ENCRYPTION_PREFIX = "agm_enc_v1:";
const OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token";
const DEFAULT_OAUTH_CLIENT_KEY = "antigravity_enterprise";
const OAUTH_CLIENT_ID_ENV = "AGENT_CONTROL_PLANE_OAUTH_CLIENT_ID";
const OAUTH_CLIENT_SECRET_ENV = "AGENT_CONTROL_PLANE_OAUTH_CLIENT_SECRET";
const OAUTH_CLIENT_KEY_ENV = "AGENT_CONTROL_PLANE_OAUTH_CLIENT_KEY";

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

async function readInput() {
  const payloadPath = process.argv[2];
  if (payloadPath) {
    return fs.readFileSync(payloadPath, "utf8");
  }
  return readStdin();
}

function writeJson(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function fail(error) {
  const message = error && error.stack ? error.stack : String(error);
  writeJson({ ok: false, error: message });
  app.quit();
}

function decryptMasterKey(managerUserData) {
  const keyPath = path.join(managerUserData, ".mk");
  const encrypted = fs.readFileSync(keyPath);
  const keyHex = safeStorage.decryptString(encrypted);
  if (!/^[a-f0-9]{64}$/i.test(keyHex)) {
    throw new Error("Antigravity Manager master key has invalid format");
  }
  return Buffer.from(keyHex, "hex");
}

function decryptPayload(masterKey, payload) {
  if (payload.startsWith("{") || payload.startsWith("[")) {
    return payload;
  }
  const body = payload.startsWith(ENCRYPTION_PREFIX)
    ? payload.slice(ENCRYPTION_PREFIX.length)
    : payload;
  const parts = body.split(":");
  if (parts.length !== 3) {
    throw new Error("Invalid encrypted Manager payload");
  }
  const [ivHex, tagHex, cipherHex] = parts;
  const decipher = crypto.createDecipheriv("aes-256-gcm", masterKey, Buffer.from(ivHex, "hex"));
  decipher.setAuthTag(Buffer.from(tagHex, "hex"));
  let plain = decipher.update(cipherHex, "hex", "utf8");
  plain += decipher.final("utf8");
  return plain;
}

function encryptPayload(masterKey, plain) {
  const iv = crypto.randomBytes(16);
  const cipher = crypto.createCipheriv("aes-256-gcm", masterKey, iv);
  let encrypted = cipher.update(plain, "utf8", "hex");
  encrypted += cipher.final("hex");
  const tag = cipher.getAuthTag();
  return `${ENCRYPTION_PREFIX}${iv.toString("hex")}:${tag.toString("hex")}:${encrypted}`;
}

function readOAuthClient() {
  const clientId = process.env[OAUTH_CLIENT_ID_ENV];
  const clientSecret = process.env[OAUTH_CLIENT_SECRET_ENV];
  if (!clientId || !clientSecret) {
    throw new Error(
      `Google OAuth client credentials are required for token refresh. Set ${OAUTH_CLIENT_ID_ENV} and ${OAUTH_CLIENT_SECRET_ENV}.`
    );
  }
  return {
    key: process.env[OAUTH_CLIENT_KEY_ENV] || DEFAULT_OAUTH_CLIENT_KEY,
    client_id: clientId,
    client_secret: clientSecret,
  };
}

async function refreshTokenIfNeeded(token, proxyUrl, enabled) {
  if (!enabled) {
    return { token, refreshed: false };
  }
  if (!token.refresh_token || String(token.refresh_token).trim() === "") {
    return { token, refreshed: false };
  }
  const now = Math.floor(Date.now() / 1000);
  const expiresAt = Number(token.expiry_timestamp || 0);
  if (expiresAt > now + 600) {
    return { token, refreshed: false };
  }
  if (proxyUrl) {
    throw new Error("Proxy-backed Manager accounts are not supported by this helper yet");
  }
  const oauthClient = readOAuthClient();
  const body = new URLSearchParams({
    client_id: oauthClient.client_id,
    client_secret: oauthClient.client_secret,
    refresh_token: token.refresh_token,
    grant_type: "refresh_token",
  });
  const response = await fetch(OAUTH_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`Google token refresh failed: ${response.status} ${detail}`);
  }
  const refreshed = await response.json();
  return {
    token: {
      ...token,
      access_token: refreshed.access_token,
      refresh_token: refreshed.refresh_token || token.refresh_token,
      expires_in: refreshed.expires_in,
      expiry_timestamp: now + Number(refreshed.expires_in || 0),
      token_type: refreshed.token_type || token.token_type || "Bearer",
      id_token: refreshed.id_token || token.id_token,
      oauth_client_key: refreshed.oauth_client_key || token.oauth_client_key || oauthClient.key,
    },
    refreshed: true,
  };
}

function findLatestAppDir(managerInstallRoot) {
  const entries = fs.readdirSync(managerInstallRoot, { withFileTypes: true });
  const candidates = entries
    .filter((entry) => entry.isDirectory() && /^app-\d+\.\d+\.\d+$/.test(entry.name))
    .map((entry) => path.join(managerInstallRoot, entry.name))
    .filter((dir) => fs.existsSync(path.join(dir, "resources", "app.asar")));
  if (candidates.length === 0) {
    throw new Error(`No Antigravity Manager app-* directory found in ${managerInstallRoot}`);
  }
  candidates.sort((left, right) => right.localeCompare(left, undefined, { numeric: true }));
  return candidates[0];
}

function loadKeyring(managerInstallRoot) {
  const appDir = findLatestAppDir(managerInstallRoot);
  const appRequire = createRequire(path.join(appDir, "resources", "app.asar", "package.json"));
  return appRequire("@napi-rs/keyring");
}

function formatAgyCredential(token) {
  const expiry = new Date(Number(token.expiry_timestamp) * 1000)
    .toISOString()
    .replace(/\.(\d{3})Z$/, ".$1000Z");
  return JSON.stringify({
    token: {
      access_token: token.access_token,
      token_type: token.token_type || "Bearer",
      refresh_token: token.refresh_token,
      expiry,
    },
    auth_method: "consumer",
  });
}

function writeAgyCredential(keyring, token) {
  const entry = keyring.Entry.withTarget("gemini:antigravity", "gemini", "antigravity");
  try {
    entry.deleteCredential();
  } catch {
    // Missing credential is fine; Manager uses the same delete-then-set pattern.
  }
  entry.setSecret(Buffer.from(formatAgyCredential(token), "utf8"));
}

async function handleWriteAgyToken(input) {
  const masterKey = decryptMasterKey(input.managerUserData);
  const plainToken = decryptPayload(masterKey, input.account.tokenJson);
  const token = JSON.parse(plainToken);
  const refreshResult = await refreshTokenIfNeeded(
    token,
    input.account.proxyUrl || null,
    Boolean(input.refresh)
  );
  const encryptedTokenJson = encryptPayload(masterKey, JSON.stringify(refreshResult.token));
  if (!input.dryRun) {
    const keyring = loadKeyring(input.managerInstallRoot);
    writeAgyCredential(keyring, refreshResult.token);
  }
  return {
    ok: true,
    accountId: input.account.id,
    refreshed: refreshResult.refreshed,
    credentialWritten: !input.dryRun,
    dryRun: Boolean(input.dryRun),
    expiresAt: refreshResult.token.expiry_timestamp || null,
    encryptedTokenJson,
  };
}

app.setName("Antigravity Manager");
if (process.platform === "win32") {
  app.setAppUserModelId("com.draculabo.antigravity-manager");
}

readInput()
  .then((raw) => {
    const input = JSON.parse(raw);
    app.setPath("userData", input.managerUserData);
    return app.whenReady().then(async () => {
      if (input.action !== "write-agy-token") {
        throw new Error(`Unsupported helper action: ${input.action}`);
      }
      const output = await handleWriteAgyToken(input);
      writeJson(output);
      app.quit();
    });
  })
  .catch(fail);
