# Shipyap security

Please use the repository's **Security → Report a vulnerability** feature for suspected security
issues. Do not post credentials, personal drafts or exploit details in a public issue. Include the
affected version, a minimal reproduction using synthetic data, and the impact you observed. Response
times are best effort.

Keep API keys in your own environment and set provider-side spending limits. Never commit a key or
paste one into a draft, issue, log or AI prompt. Rotate an exposed key with its provider.

The review server (`shipyap review`) is for a single owner on the local machine. Its link is a local session token,
not user authentication; do not share it or expose the server to a network. Approval binds the exact
text, destination and time, but does not prove claims are true. Scheduling is off by default, is a
separate confirmation, and requires an explicitly configured publisher. STOP pauses new actions; it
cannot cancel a post already submitted to Buffer. Check Buffer directly for an uncertain outcome.
