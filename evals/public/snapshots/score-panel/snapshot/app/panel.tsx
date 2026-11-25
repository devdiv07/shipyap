'use client';

import { useState } from 'react';

export function ScorePanel() {
  const [result, setResult] = useState(null);

  const load = async () => {
    const reply = await fetch('/api/score').then((r) => r.json());
    setResult({ score: reply.score, provider: reply.provider });
  };

  return (
    <div>
      {result && (
        <span>{result.provider.includes('Acme') ? 'AI Review' : 'Rule Check'}</span>
      )}
      <button onClick={load}>Load score</button>
    </div>
  );
}
