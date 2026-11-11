Good morning briefing — deliver concisely.

0) NOTES FOR TODAY: Read /root/hermes-wiki/briefing-notes.md. If it has content, surface it first under a "📌 Saved for today" heading (reproduce the content, don't summarize away specifics), then clear the file (write empty string) so it doesn't repeat tomorrow. If the file is empty or missing, skip silently.

1) CALENDAR: Use get_events to list today's calendar events. Summarize in a table.

2) INBOX TRIAGE: Use search_gmail_messages to get the 20 most recent inbox messages. For EACH email, read it with get_gmail_message_content, then ACT on it immediately:
   - DELETE (add TRASH): marketing, newsletters, promotions, social notifications, spam, storage warnings, onboarding drip emails, automated notifications that don't need action
   - ARCHIVE (remove INBOX): receipts, invoices, bills, past calendar invites, order confirmations, resolved conversations
   - FILE (add appropriate label OR remove INBOX): newsletters/promos that the user has explicitly labelled themselves (user-labelled as "old", "newsletter", "promo", etc.) — but if it's a standard unlabelled newsletter, DELETE it
   - KEEP (no action): emails from real people needing reply, active conversations, security alerts, travel bookings, legal docs, VC/recruiter outreach, time-sensitive applications

3) KEY EMAILS: After processing, report only the KEPT emails in a table (Sender | Subject | Why kept). Report totals: X deleted, Y archived, Z kept.

Be ruthless with deletions — anything automated and non-actionable goes. If unsure about an email, KEEP it and note it for review. Table format for all output.

Pages you create or edit start with the wiki's frontmatter block (see CONVENTIONS.md); nights/ pages and content-roundup use type: log.