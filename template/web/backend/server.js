import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const backendDir = fileURLToPath(new URL(".", import.meta.url));
const frontendDir = join(backendDir, "..", "frontend", "dist");
const port = Number.parseInt(process.env.PORT || "3000", 10);
const host = process.env.HOST || "0.0.0.0";

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml"
};

function sendJson(response, status, payload) {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(payload));
}

function staticPath(urlPath) {
  const requested = normalize(decodeURIComponent(urlPath)).replace(/^(\.\.[/\\])+/, "");
  const candidate = join(frontendDir, requested === "/" ? "index.html" : requested);
  if (candidate.startsWith(frontendDir) && existsSync(candidate) && statSync(candidate).isFile()) {
    return candidate;
  }
  return join(frontendDir, "index.html");
}

const server = createServer((request, response) => {
  const url = new URL(request.url || "/", `http://${request.headers.host || "localhost"}`);
  if (url.pathname === "/api/health") {
    sendJson(response, 200, { status: "ok" });
    return;
  }
  if (url.pathname.startsWith("/api/")) {
    sendJson(response, 404, { error: "Not found" });
    return;
  }
  const path = staticPath(url.pathname);
  if (!existsSync(path)) {
    sendJson(response, 503, { error: "Frontend has not been built" });
    return;
  }
  response.writeHead(200, { "content-type": contentTypes[extname(path)] || "application/octet-stream" });
  createReadStream(path).pipe(response);
});

server.listen(port, host, () => {
  console.log(`ARC-Bench backend listening on http://${host}:${port}`);
});
