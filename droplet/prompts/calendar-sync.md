Scan the user's Gmail inbox for emails containing events, deadlines, travel bookings, meetings, reservations, or any time-sensitive commitments from the last 7 days. For each one found, extract: title, date, time, location, and any relevant details.

**CRITICAL — DEDUPLICATION**: Before creating ANY calendar event, you MUST first call get_events to retrieve all existing events in the relevant date range. Compare each candidate event against existing ones by title + date. If a matching event already exists (same title and date), SKIP it — do NOT create a duplicate. Only add genuinely new events that don't already appear on the calendar.

When creating events, use manage_event with appropriate start/end times. If the email doesn't specify an exact time, use 9:00-10:00 as default. Set reminders for events with specific times (30 min before).

After processing, report: how many emails were scanned, how many candidate events were found, how many were new (created), and how many were skipped as duplicates.