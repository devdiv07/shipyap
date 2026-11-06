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

## Existing workspaces: preview the publication ledger upgrade

Version 0.1.3 requires an explicit ledger upgrade. Starting review does not upgrade an old ledger.
Stop the review server and keep the workspace STOP file in place. First run this offline preview:

```powershell
shipyap publication migrate --ledger 'C:\work\my-posts\automation\review-ledger.sqlite' --drafts 'C:\work\my-posts\drafts'
```

The preview opens the original database read-only and upgrades a temporary copy. It lists every
earlier submission, identifies legacy duplicates and reports unresolved approvals. It preserves
the original approval, decision and intent rows. Inspect this result before changing the real ledger;
back up the workspace, then repeat with `--apply` only when you choose to migrate it. An unresolved
historical submission blocks all new sends rather than being ignored. No migration contacts Buffer.

## Earlier posts and intentional reposts

A story already submitted cannot be sent again by editing it, approving a new revision or choosing
a different date. Published and scheduled stories keep their dated status even after later review
decisions. The Published date is when the local ledger confirmed publication, which may be later
than Buffer actually published it. An accepted or uncertain submission also prevents a fresh send.
Deleting a post in Buffer does not unlock it locally.

To post it again intentionally, choose **Review repost**, inspect the exact text, destination, time
and matching earlier posts, then type the displayed `REPOST` phrase. This records your approval
and a permission for this one exact repost. Separately confirm **Send confirmed repost**. Changing
the post or adding new matching publication history invalidates that permission. The original
approved LinkedIn/X pair shares one publication cycle; its unsent X companion can finish without
resending LinkedIn.

The same guard blocks equal or lexically near-identical text across different stories, including
queued and uncertain sends. Unicode, case, spacing and punctuation are normalized; high token
similarity and substantial overlapping text also block. Matching text and the rule are shown for
review. This is a local lexical check, not semantic paraphrase detection, and covers only posts
recorded in this workspace's ledger. No model call is made for it.
