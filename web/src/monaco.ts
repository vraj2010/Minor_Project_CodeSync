import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/esm/vs/editor/editor.api";
import "monaco-editor/esm/vs/basic-languages/python/python.contribution";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";

// Bundled here rather than fetched from a CDN, so the page works offline and
// nothing outside this site is loaded.
self.MonacoEnvironment = { getWorker: () => new EditorWorker() };
loader.config({ monaco });
