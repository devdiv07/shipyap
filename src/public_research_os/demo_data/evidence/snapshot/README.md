# SYNTHETIC retry exercise

The toy queue recovered 3/10 jobs in the supplied exercise. Its log records the outcome for each job separately, so a reader can distinguish a completed action from a response that merely reports that action as complete.

Retries are capped at 2 per job in this exercise. A worker consults the saved journal before sending another request, and the journal entry records the attempted action as well as the response that was actually observed by the worker.

A worker writes an intent before contacting the toy service. When a response is lost, the saved intent remains available to the next worker; the later worker reads that record instead of treating silence as a completed response.

The caller reuses the job token when retrying an unchanged action. A different action requires a different token, and the exercise keeps the original journal entry available so that a reviewer can compare the attempted actions directly.

Scope: this is synthetic evidence only and does not measure production reliability.

The recorded adapter label is 1234abcd.

Synthetic note 1: no production result is asserted.
Synthetic note 2: no production result is asserted.
Synthetic note 3: no production result is asserted.
Synthetic note 4: no production result is asserted.
Synthetic note 5: no production result is asserted.
Synthetic note 6: no production result is asserted.
Synthetic note 7: no production result is asserted.
Synthetic note 8: no production result is asserted.
Synthetic note 9: no production result is asserted.
Synthetic note 10: no production result is asserted.
Synthetic note 11: no production result is asserted.
