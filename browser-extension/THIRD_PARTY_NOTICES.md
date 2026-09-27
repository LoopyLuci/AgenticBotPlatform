# Third-party notices

Only `dependencies` (never `devDependencies` — those build and test the extension but ship nothing) end up inside
`dist/`. The full, machine-generated transitive list (regenerate with `npm run sbom`) is `sbom.json`, 67 packages as
of this writing, all pulled in by the three runtime libraries below.

| Package | Version | License | Bundled into | Purpose |
|---|---|---|---|---|
| [@mlc-ai/web-llm](https://github.com/mlc-ai/web-llm) | 0.2.85 | Apache-2.0 | `offscreen.js` | Runs WebGPU-compiled chat models (Qwen, Llama, Phi) in the browser. |
| [@huggingface/transformers](https://github.com/huggingface/transformers.js) | 4.3.0 | Apache-2.0 | `offscreen.js` | Runs ONNX models (embeddings, speech-to-text, classification) on WebGPU or WASM. |
| [onnxruntime-web](https://github.com/microsoft/onnxruntime) | 1.30.0 | MIT | `offscreen.js`, `onnx/*.wasm` | The WASM/WebGPU inference engine transformers.js runs on. |

No dependency is modified from its published form; each keeps its own license text inside `node_modules/<name>/LICENSE`
during development (not shipped in `dist/`, which contains only bundled JS/WASM/static assets, per each library's
license terms for redistribution in compiled form).

## Regenerating

```bash
npm run sbom   # writes sbom.json (npm ls --omit=dev --all --json)
```

Re-run this, and update the table above, whenever a runtime dependency (not a dev dependency) is added, removed or
has its license changed upstream.
