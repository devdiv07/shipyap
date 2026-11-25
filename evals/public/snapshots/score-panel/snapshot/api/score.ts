// SYNTHETIC toy scoring route.
function ruleScore(text: string) {
  return text.includes('refund') ? 40 : 10;
}

export async function score(text: string, callAcme: (t: string) => Promise<Response>) {
  const reply = await callAcme(text);
  if (!reply.ok) {
    return { score: ruleScore(text), provider: 'Fallback (Acme unavailable)' };
  }
  try {
    const parsed = JSON.parse(await reply.text());
    return { score: parsed.score, provider: 'Acme Model' };
  } catch {
    return { score: ruleScore(text), provider: 'Fallback (parse error)' };
  }
}
