'use client';

import { useState } from 'react';
import { useSender, useReceipt } from './sender';

export function RefundDesk({ amount }: { amount: number }) {
  const { send, data: ticket } = useSender();
  const { isSuccess: isDone } = useReceipt({ ticket });
  const [noticeSent, setNoticeSent] = useState(false);
  const [review, setReview] = useState(null);

  const requestRefund = () => {
    send({ action: 'refund', amount });
  };

  const sendNotice = () => {
    send({ action: 'notice' });
    setNoticeSent(true);
  };

  const load = async () => {
    const reply = await fetch('/api/review').then((r) => r.json());
    setReview({ score: reply.score, source: reply.source });
  };

  return (
    <div>
      <button onClick={requestRefund}>Request refund</button>
      {isDone && <button onClick={sendNotice}>Send notice</button>}
      {noticeSent && <p>Notice sent</p>}
      {review && (
        <span>{review.source === 'model' ? 'Model Review' : 'Rule Check'}</span>
      )}
      <button onClick={load}>Load review</button>
    </div>
  );
}
