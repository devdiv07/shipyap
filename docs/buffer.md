# Optional Buffer setup for Shipyap

You can draft and review with Shipyap without Buffer. Use these steps only if you want to schedule posts to
your own LinkedIn channel and, optionally, X. Return to the [quickstart](../README.md#use-your-own-repo)
for drafting setup.

## Connect your channels and create a key

Connect your social accounts to Buffer, then create a key in **Settings → API** using
[Buffer's API-key instructions](https://developers.buffer.com/guides/authentication.html).
Buffer's [Free plan currently supports up to three connected channels](https://buffer.com/pricing);
a LinkedIn channel and an X channel use two of those slots. Plan limits can change.

In Windows **Edit environment variables for your account**, add `PROS_BUFFER_API_KEY` under
**User variables**, with your own Buffer API key as its value. Keep keys out of pros.toml,
repository files and chat.

## Find the channel IDs

Use [Buffer's API Explorer](https://developers.buffer.com/explorer.html) with your own key.
The explorer stores the key in your browser; use its **Clear Token** button when finished.
Run these read-only queries, not a create-post mutation:

1. Get the organization ID from the account query:

   ```graphql
   query {
     account { organizations { id name } }
   }
   ```

2. Replace `YOUR_ORGANIZATION_ID` below with that ID and list its channels:

   ```graphql
   query {
     channels(input: { organizationId: "YOUR_ORGANIZATION_ID" }) { id name service }
   }
   ```

Copy the `id` of your LinkedIn channel (`service: linkedin`). For optional X publishing, copy
the X channel's `id` (`service: twitter`). Use Buffer's channel IDs, not your social profile URL
or organization ID. See the official [organization lookup](https://developers.buffer.com/examples/get-organizations.html)
and [channel lookup](https://developers.buffer.com/examples/get-channels.html) examples.

## Configure and enable scheduling

Set these **top-level** entries in pros.toml, above any `[author]` table. Replace the synthetic
IDs with the channel IDs you just copied; omit `x_channel_id` if you only want LinkedIn:

```toml
buffer_channel_id = '0123456789abcdef01234567'
x_channel_id = '0123456789abcdef01234568'
```

Keep or edit the `posting_slots` and `utc_offset` created by `shipyap init`. The offset is fixed,
so update it if your local offset changes. X uses the post's opening text and scope line and
may be omitted if they do not fit its limit.

When ready, add a Windows **User variable** named `PUBLISHING_DISABLED` with the exact lowercase
value `false`. Open a new PowerShell terminal and restart the review page (restart VS Code first
if using its terminal):

```powershell
.\scripts\review.ps1 -Config 'C:\work\my-posts\pros.toml'
```

Read and approve the exact post, then separately confirm scheduling. Approval alone sends nothing.
Unsetting the flag, or using any value other than `false`, disables new scheduling after review
restarts. A workspace `STOP` file pauses new actions; it cannot cancel posts already sent to Buffer.
