const stubUrl = new URL("./app_stub.mjs", import.meta.url).href;

export async function resolve(specifier, context, nextResolve)
{
  if (specifier.endsWith("/scripts/app.js"))
    return { url: stubUrl, shortCircuit: true };
  return nextResolve(specifier, context);
}
