import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const legacyRoot = process.env.CLICKUP_LEGACY_REPO
  ? path.resolve(process.env.CLICKUP_LEGACY_REPO)
  : path.resolve(__dirname, "..", "..", "clickup-mcp");

// Keep non-protocol logs off stdout so stdio remains MCP-safe.
console.log = (...args) => console.error(...args);
console.info = (...args) => console.error(...args);
console.warn = (...args) => console.error(...args);

const requireFromLegacy = createRequire(path.join(legacyRoot, "package.json"));
const stdioModulePath = requireFromLegacy.resolve("@modelcontextprotocol/sdk/server/stdio.js");

const { StdioServerTransport } = await import(pathToFileURL(stdioModulePath).href);
const { createApplicationConfig } = await import(
  pathToFileURL(path.join(legacyRoot, "dist", "application", "config", "applicationConfig.js")).href
);
const { SessionCache } = await import(
  pathToFileURL(path.join(legacyRoot, "dist", "application", "services", "SessionCache.js")).href
);
const { createServer } = await import(pathToFileURL(path.join(legacyRoot, "dist", "server", "factory.js")).href);
const { resolveTeamIdFromApiKey } = await import(
  pathToFileURL(path.join(legacyRoot, "dist", "server", "teamResolution.js")).href
);

function resolveApiKey() {
  return (
    process.env.CLICKUP_API_TOKEN ||
    process.env.clickupApiToken ||
    process.env.apiKey ||
    process.env.API_KEY ||
    ""
  );
}

let config;
try {
  config = createApplicationConfig({});
} catch (error) {
  const apiKey = resolveApiKey();
  if (apiKey && error instanceof Error && error.message.includes("teamId")) {
    const teamId = await resolveTeamIdFromApiKey(apiKey);
    process.env.TEAM_ID = teamId;
    config = createApplicationConfig({});
  } else {
    throw error;
  }
}

const sessionCache = new SessionCache(config.hierarchyCacheTtlMs, config.spaceConfigCacheTtlMs);
const server = createServer(config, sessionCache);
const transport = new StdioServerTransport();

await server.connect(transport);
