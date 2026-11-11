Scan the user's Gmail inbox for emails containing events, deadlines, travel bookings, meetings, reservations, or any time-sensitive commitments from the last 7 days. Use search_gmail_messages with include_headers set to true.

**SKIP EMAILS ALREADY READ**: The "Script Output" section above lists the message IDs this job already read in full on earlier nights; their events were handled then. Do not read those messages again. Read only the messages whose IDs are not in that list. If there is no Script Output section, or it reports a script error, read every message from the last 7 days as before, and say so in one line in your report.

For each new email that contains one, extract: title, date, time, location, and any relevant details.

**CRITICAL — DEDUPLICATION**: Before creating ANY calendar event, you MUST first call get_events to retrieve the existing events on the candidate events' dates: one call from the earliest to the latest candidate date. If there are no candidate events, do not call get_events. Compare each candidate event against existing ones by title + date. If a matching event already exists (same title and date), SKIP it — do NOT create a duplicate. Only add genuinely new events that don't already appear on the calendar.

When creating events, use manage_event with appropriate start/end times. If the email doesn't specify an exact time, use 9:00-10:00 as default. Set reminders for events with specific times (30 min before).

After processing, report: how many emails the search returned, how many were skipped as already read, how many were read, how many candidate events were found, how many were new (created), and how many were skipped as duplicates.