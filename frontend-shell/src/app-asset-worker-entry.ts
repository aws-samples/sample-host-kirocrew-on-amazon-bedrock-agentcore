// Bundled to /app-asset-worker.js and imported by the upstream /sw.js.
import { installAppAssetWorker, type WorkerScope } from "./app-asset-worker.js";

installAppAssetWorker(self as unknown as WorkerScope);
