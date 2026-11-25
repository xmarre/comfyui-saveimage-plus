import assert from "node:assert/strict";
import { test } from "node:test";

import { app } from "./app_stub.mjs";
import "../../web/jpeg_settings.js";

const JPEG_WIDGETS = ["jpeg_quality", "jpeg_subsampling", "jpeg_optimize", "jpeg_progressive"];
const WIDGET_HEIGHT = 24;
const BASE_HEIGHT = 30;

const extension = app.extensions.find(e => e.name === "SaveImagePlus.JpegSettings");

// Minimal model of a LiteGraph node: hidden widgets take no height.
function makeNodeType(prototype = {})
{
  function NodeType()
  {
    this.widgets = [
      { name: "filename_prefix", value: "ComfyUI" },
      { name: "file_type", value: "PNG", callback: null },
      { name: "remove_metadata", value: false },
      { name: "jpeg_quality", value: 90 },
      { name: "jpeg_subsampling", value: "4:4:4" },
      { name: "jpeg_optimize", value: true },
      { name: "jpeg_progressive", value: false },
    ];
    this.inputs = [
      { name: "images", link: null },
      { name: "file_type", widget: { name: "file_type" }, link: null },
    ];
    this.size = this.computeSize();
    this.dirty = 0;
  }
  Object.assign(NodeType.prototype, prototype);
  NodeType.prototype.computeSize = function()
  {
    const visible = this.widgets.filter(w => !w.hidden).length;
    return [300, BASE_HEIGHT + visible * WIDGET_HEIGHT];
  };
  NodeType.prototype.setSize = function(size)
  {
    this.size = [...size];
  };
  NodeType.prototype.setDirtyCanvas = function()
  {
    this.dirty++;
  };
  return NodeType;
}

async function register(prototype, name = "SaveImagePlus")
{
  const NodeType = makeNodeType(prototype);
  await extension.beforeRegisterNodeDef(NodeType, { name });
  return NodeType;
}

function create(NodeType)
{
  const node = new NodeType();
  node.onNodeCreated?.();
  return node;
}

const widget = (node, name) => node.widgets.find(w => w.name === name);
const jpegHidden = node => JPEG_WIDGETS.map(name => widget(node, name).hidden);

function selectFileType(node, value)
{
  const fileType = widget(node, "file_type");
  fileType.value = value;
  fileType.callback?.(value);
}

test("extension is registered", () => {
  assert.ok(extension);
});

test("other node types are not modified", async () => {
  const NodeType = await register({}, "SaveImage");
  assert.equal(NodeType.prototype.onNodeCreated, undefined);
  assert.equal(NodeType.prototype.onConfigure, undefined);
  assert.equal(NodeType.prototype.onConnectionsChange, undefined);
});

test("new node hides JPEG widgets for the default PNG format and shrinks", async () => {
  const node = create(await register());
  assert.deepEqual(jpegHidden(node), [true, true, true, true]);
  assert.equal(node.size[1], BASE_HEIGHT + 3 * WIDGET_HEIGHT);
});

test("selecting JPEG shows the widgets and grows the node, other formats hide them", async () => {
  const node = create(await register());
  node.setSize([350, node.size[1] + 200]); // extra space, e.g. for image previews
  selectFileType(node, "JPEG");
  assert.deepEqual(jpegHidden(node), [false, false, false, false]);
  assert.deepEqual(node.size, [350, BASE_HEIGHT + 7 * WIDGET_HEIGHT + 200]);
  for (const format of ["PNG", "WEBP (lossless)", "WEBP (lossy)"])
  {
    selectFileType(node, format);
    assert.deepEqual(jpegHidden(node), [true, true, true, true], format);
    assert.deepEqual(node.size, [350, BASE_HEIGHT + 3 * WIDGET_HEIGHT + 200], format);
    selectFileType(node, "JPEG");
  }
});

test("widget values are preserved while hidden", async () => {
  const node = create(await register());
  selectFileType(node, "JPEG");
  widget(node, "jpeg_quality").value = 77;
  widget(node, "jpeg_subsampling").value = "4:2:0";
  widget(node, "jpeg_optimize").value = false;
  widget(node, "jpeg_progressive").value = true;
  selectFileType(node, "PNG");
  selectFileType(node, "JPEG");
  assert.deepEqual(JPEG_WIDGETS.map(name => widget(node, name).value), [77, "4:2:0", false, true]);
});

test("existing callbacks and hooks are chained", async () => {
  const calls = [];
  const NodeType = await register({
    onNodeCreated() { calls.push("created"); this.widgets[1].callback = v => calls.push(`callback:${v}`); return "created"; },
    onConfigure(info) { calls.push(`configure:${info}`); return "configured"; },
    onConnectionsChange(...args) { calls.push(`connections:${args.length}`); return "connections"; },
  });
  const node = new NodeType();
  assert.equal(node.onNodeCreated(), "created");
  selectFileType(node, "JPEG");
  assert.equal(node.onConfigure("info"), "configured");
  assert.equal(node.onConnectionsChange(1, 1, true, null, node.inputs[1]), "connections");
  assert.deepEqual(calls, ["created", "callback:JPEG", "configure:info", "connections:5"]);
  assert.deepEqual(jpegHidden(node), [false, false, false, false]);
});

test("configure follows the restored format and keeps a large enough saved size", async () => {
  const NodeType = await register();
  const node = create(NodeType);
  // Restored state of a saved JPEG workflow, including extra preview space.
  widget(node, "file_type").value = "JPEG";
  node.size = [400, 600];
  node.onConfigure({});
  assert.deepEqual(jpegHidden(node), [false, false, false, false]);
  assert.deepEqual(node.size, [400, 600]);

  const png = create(NodeType);
  png.size = [400, 600];
  png.onConfigure({});
  assert.deepEqual(jpegHidden(png), [true, true, true, true]);
  assert.deepEqual(png.size, [400, 600]);
});

test("configure grows a node saved before the JPEG widgets existed", async () => {
  const node = create(await register());
  widget(node, "file_type").value = "JPEG";
  node.size = [315, BASE_HEIGHT + 3 * WIDGET_HEIGHT];
  node.onConfigure({});
  assert.deepEqual(node.size, [315, BASE_HEIGHT + 7 * WIDGET_HEIGHT]);
});

test("JPEG widgets stay visible while file_type is driven by a link", async () => {
  const node = create(await register());
  node.inputs[1].link = 5;
  node.onConnectionsChange(1, 1, true, { id: 5 }, node.inputs[1]);
  assert.deepEqual(jpegHidden(node), [false, false, false, false]);
  assert.ok(node.size[1] >= BASE_HEIGHT + 7 * WIDGET_HEIGHT);
  node.inputs[1].link = null;
  node.onConnectionsChange(1, 1, false, null, node.inputs[1]);
  assert.deepEqual(jpegHidden(node), [true, true, true, true]);
});

test("connection changes never shrink the node", async () => {
  const node = create(await register());
  selectFileType(node, "JPEG");
  const size = [...node.size];
  node.onConnectionsChange(1, 0, true, { id: 1 }, node.inputs[0]);
  selectFileType(node, "PNG");
  const pngSize = [...node.size];
  node.onConnectionsChange(1, 0, false, null, node.inputs[0]);
  assert.deepEqual(node.size, pngSize);
  assert.ok(pngSize[1] < size[1]);
});

test("nodes without a file_type widget are ignored", async () => {
  const NodeType = await register();
  const node = new NodeType();
  node.widgets = node.widgets.filter(w => w.name !== "file_type");
  const size = [...node.size];
  node.onNodeCreated();
  assert.deepEqual(node.size, size);
  assert.ok(JPEG_WIDGETS.every(name => widget(node, name).hidden === undefined));
});
