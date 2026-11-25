import { app } from "../../scripts/app.js";

const NODE_TYPE = "SaveImagePlus";
const FILE_TYPE_WIDGET = "file_type";
const FILE_TYPE_JPEG = "JPEG";
const JPEG_WIDGETS = ["jpeg_quality", "jpeg_subsampling", "jpeg_optimize", "jpeg_progressive"];

// Shows the JPEG-only widgets only when JPEG is selected. Hidden widgets keep
// their values and are still serialized, so switching formats, duplicating the
// node or reloading a workflow preserves them. When file_type is driven by a
// link its runtime value is unknown here, so the JPEG widgets stay visible.
// Frontends without widget.hidden support simply keep showing all widgets.
export function updateJpegWidgets(node, resize)
{
  const fileType = node.widgets?.find(w => w.name === FILE_TYPE_WIDGET);
  if (!fileType)
    return;

  const fileTypeInput = node.inputs?.find(i => i.widget?.name === FILE_TYPE_WIDGET);
  const hidden = fileType.value !== FILE_TYPE_JPEG && fileTypeInput?.link == null;

  const heightBefore = node.computeSize()[1];
  for (const widget of node.widgets)
  {
    if (JPEG_WIDGETS.includes(widget.name))
      widget.hidden = hidden;
  }
  const minSize = node.computeSize();

  // Grow or shrink by the height of the toggled widgets, keeping any extra
  // space the node has (e.g. for image previews), but never below its minimum.
  const height = resize ? node.size[1] + minSize[1] - heightBefore : node.size[1];
  if (height !== node.size[1] || height < minSize[1])
    node.setSize([node.size[0], Math.max(height, minSize[1])]);
  node.setDirtyCanvas?.(true, true);
}

app.registerExtension({
  name: "SaveImagePlus.JpegSettings",
  async beforeRegisterNodeDef(nodeType, nodeData)
  {
    if (nodeData.name !== NODE_TYPE)
      return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function(...args)
    {
      const result = onNodeCreated?.apply(this, args);
      const fileType = this.widgets?.find(w => w.name === FILE_TYPE_WIDGET);
      if (fileType)
      {
        const node = this;
        const callback = fileType.callback;
        fileType.callback = function(...callbackArgs)
        {
          const callbackResult = callback?.apply(this, callbackArgs);
          updateJpegWidgets(node, true);
          return callbackResult;
        };
      }
      updateJpegWidgets(this, true);
      return result;
    };

    // Widget values are restored before onConfigure runs (workflow load,
    // paste, clone, undo), so visibility follows the restored file_type.
    // The saved node size is kept unless it is too small.
    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function(...args)
    {
      const result = onConfigure?.apply(this, args);
      updateJpegWidgets(this, false);
      return result;
    };

    const onConnectionsChange = nodeType.prototype.onConnectionsChange;
    nodeType.prototype.onConnectionsChange = function(...args)
    {
      const result = onConnectionsChange?.apply(this, args);
      // Also called from configure() before widget values are restored, so
      // only update visibility here and leave the node size to onConfigure.
      updateJpegWidgets(this, false);
      return result;
    };
  },
});
