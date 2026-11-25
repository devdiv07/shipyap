// SYNTHETIC toy review route.
function ruleCheck(text: string) {
  return { score: text.includes('refund') ? 40 : 10 };
}

export async function review(text: string, callModel?: (t: string) => Promise<string>) {
  if (!callModel) {
    return { score: ruleCheck(text).score, source: 'rules' };
  }
  let result;
  try {
    result = JSON.parse(await callModel(text));
  } catch {
    result = ruleCheck(text);
  }
  return { score: result.score, source: 'model' };
}
