// Redirects the ComfyUI frontend import "../../scripts/app.js" to a stub so the
// extension modules in web/ can be loaded by Node's test runner.
import { register } from "node:module";

register("./app_stub_loader.mjs", import.meta.url);
